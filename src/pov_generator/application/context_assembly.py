"""Сборка контекста задачи: источник истины против производного, приоритет по
авторитету, укладка в бюджет.

Принцип. То, что **авторитетно подтвердил человек** (прямой вход заказчика;
выбранные/подтверждённые решения и артефакты) и **структурно обязательное** — не
выкидывается молча. Производные предположения системы — по бюджету, в порядке
авторитета.

Разделение ответственностей (SRP):
* отбор кандидатов и их классификация — в ``ContextService`` (ему нужен доступ к
  runtime/состоянию проекта);
* модель авторитета — здесь (:class:`ContextAuthority`);
* укладка в бюджет — здесь (:func:`pack_context`): единственное место, где
  работает размер; выкинутое фиксируется для аудита (``excluded``).

Точки расширения (OCP): новый род элемента — это новая комбинация
(authority, pinned) у кандидата, без правки укладчика; иная стратегия сжатия
слишком большого источника подключается у вызывающего, не здесь.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from ..domain.artifacts import ContextItem

# Резерв на вывод модели (токены). Окно модели = вход + выход; оставляем место
# под ответ, иначе генерация упрётся в потолок окна.
OUTPUT_RESERVE_TOKENS = 8000


class ContextAuthority(IntEnum):
    """Авторитет элемента контекста.

    Ось — «подтверждено человеком против предположено системой». Чем выше
    значение, тем приоритетнее элемент при нехватке бюджета.
    """

    REFERENCE = 0        # справочный / вендорский материал
    DERIVED = 10         # извлечённые факты, неподтверждённые артефакты
    ASSUMED = 20         # авто-допущения системы (не показаны/не подтверждены)
    REQUIRED = 30        # структурно обязательный вход задачи
    CONFIRMED = 40       # подтверждённые человеком решение/артефакт
    CUSTOMER_INPUT = 50  # прямой вход заказчика (запрос / вложения / ответы)


@dataclass(frozen=True)
class ContextCandidate:
    """Кандидат на включение в контекст задачи.

    ``pinned`` — нельзя выкидывать (структурно обязательное ИЛИ подтверждённое
    человеком ИЛИ прямой вход заказчика). ``display_order`` — позиция в финальном
    промпте (логическое чтение), не путать с приоритетом укладки.
    """

    item: ContextItem
    authority: ContextAuthority
    pinned: bool
    display_order: int


@dataclass(frozen=True)
class PackResult:
    items: tuple[ContextItem, ...]
    """Включённые элементы в порядке чтения (``display_order``)."""
    excluded: tuple[str, ...]
    """Человекочитаемые записи о выкинутом — для аудита и UI."""
    used_tokens: int
    over_budget: bool
    """Закреплённые сами по себе превысили бюджет — вызывающий решает, что делать
    (сжать источник или упасть громко)."""


# Маркер усечённого источника — явный, чтобы потеря была видимой, а не молчаливой.
_TRUNCATION_MARKER = "\n\n[…источник усечён под бюджет задачи; полный текст — в исходном файле…]"


def _truncate_item(item: ContextItem, target_tokens: int) -> ContextItem:
    """Faithful head-truncation элемента до ~target_tokens с явной пометкой.

    Сохраняем начало текста (детерминированно) и добавляем видимый маркер. Это
    первый, неблокирующий шаг; верное LLM-резюме — будущая точка расширения.
    """
    if target_tokens <= 0 or item.token_estimate <= target_tokens:
        return item
    ratio = target_tokens / item.token_estimate
    keep = max(1, int(len(item.content) * ratio) - len(_TRUNCATION_MARKER))
    return ContextItem(
        item_id=item.item_id,
        item_type=item.item_type,
        source_ref=item.source_ref,
        title=item.title,
        content=item.content[:keep].rstrip() + _TRUNCATION_MARKER,
        token_estimate=max(1, target_tokens),
        required=item.required,
        priority=item.priority,
    )


def _fit_pinned(
    pinned: list[ContextCandidate], budget_tokens: int | None
) -> tuple[list[ContextCandidate], int, list[str]]:
    """Подогнать закреплённые под бюджет, усекая источники истины (а не падая).

    Усекаемы только источники заказчика (``CUSTOMER_INPUT``): их текст можно
    верно сократить с пометкой. Структурно обязательное (артефакты, инструкция)
    не трогаем — если ОНО само не влезает, это настоящий отказ (вернём как есть,
    вызывающий увидит ``over_budget``).
    """
    total = sum(c.item.token_estimate for c in pinned)
    if budget_tokens is None or total <= budget_tokens:
        return pinned, total, []
    fixed = sum(
        c.item.token_estimate for c in pinned if c.authority != ContextAuthority.CUSTOMER_INPUT
    )
    sources = [c for c in pinned if c.authority == ContextAuthority.CUSTOMER_INPUT]
    room = budget_tokens - fixed
    if room <= 0 or sum(c.item.token_estimate for c in sources) <= room:
        # Усечение источников не спасёт — переполнение из-за обязательного.
        return pinned, total, []
    # Water-filling: маленькие источники проходят целиком, остаток бюджета делят
    # большие. Так мелкий печатный запрос не режется ради большого файла.
    allocation: dict[int, int] = {}
    remaining_room = room
    remaining = len(sources)
    for candidate in sorted(sources, key=lambda c: c.item.token_estimate):
        fair = remaining_room // remaining if remaining else 0
        give = min(candidate.item.token_estimate, fair)
        allocation[id(candidate)] = give
        remaining_room -= give
        remaining -= 1
    notes: list[str] = []
    result: list[ContextCandidate] = []
    used = fixed
    for candidate in pinned:
        if candidate.authority != ContextAuthority.CUSTOMER_INPUT:
            result.append(candidate)
            continue
        give = allocation[id(candidate)]
        if candidate.item.token_estimate <= give:
            result.append(candidate)
            used += candidate.item.token_estimate
            continue
        trimmed = _truncate_item(candidate.item, give)
        result.append(
            ContextCandidate(trimmed, candidate.authority, pinned=True, display_order=candidate.display_order)
        )
        used += trimmed.token_estimate
        notes.append(
            f"{candidate.item.title}: усечён до ~{trimmed.token_estimate} ток. "
            f"(источник больше бюджета задачи)"
        )
    return result, used, notes


def pack_context(
    candidates: list[ContextCandidate], budget_tokens: int | None
) -> PackResult:
    """Уложить кандидатов в бюджет.

    Закреплённые входят всегда; их источники истины при нехватке места верно
    усекаются (видимо, с пометкой), а не теряются молча. Остальное (производное)
    добавляется по убыванию ``(authority, priority)``, пока есть место; что не
    вошло — в ``excluded``. Финальный порядок элементов — по ``display_order``.
    ``over_budget`` — даже после усечения источников обязательное не влезло
    (вызывающий решает: поднять окно/бюджет или упасть).
    """
    pinned, used, excluded = _fit_pinned(
        [c for c in candidates if c.pinned], budget_tokens
    )
    droppable = sorted(
        (c for c in candidates if not c.pinned),
        key=lambda c: (int(c.authority), c.item.priority),
        reverse=True,
    )
    chosen: list[ContextCandidate] = list(pinned)
    for candidate in droppable:
        if budget_tokens is None or used + candidate.item.token_estimate <= budget_tokens:
            chosen.append(candidate)
            used += candidate.item.token_estimate
        else:
            excluded.append(
                f"{candidate.item.title} "
                f"({candidate.item.token_estimate} ток., {candidate.authority.name}) "
                f"— не вошло в бюджет"
            )
    over_budget = budget_tokens is not None and used > budget_tokens
    chosen.sort(key=lambda c: c.display_order)
    return PackResult(
        items=tuple(candidate.item for candidate in chosen),
        excluded=tuple(excluded),
        used_tokens=used,
        over_budget=over_budget,
    )


def effective_input_budget(
    template_intent: int | None,
    model_window: int | None,
    hard_ceiling: int | None = None,
) -> int | None:
    """Рабочий бюджет входа: намерение шаблона, ограниченное окном модели.

    ``model_window`` — потолок (окно модели), из него вычитаем резерв на вывод.
    ``hard_ceiling`` — дополнительный жёсткий потолок входа (напр. для провайдера
    с лимитом окна, где объём токенов выжигает 5-часовое окно): срезает только
    ПРОИЗВОДНЫЙ контекст (обязательное/pinned укладчик не трогает — при нехватке
    он громко падает, не теряет молча). Возвращает ``None`` (без лимита) только
    если не задано НИЧЕГО. Для больших окон (≥128k) намерение шаблона почти
    всегда меньше потолка — поведение обычных задач не меняется; потолки защищают
    лишь от переполнения реально маленького окна / выжигания окна подписки.
    """
    limits = [v for v in (template_intent, hard_ceiling) if v is not None and v > 0]
    if model_window is not None and model_window > 0:
        limits.append(max(OUTPUT_RESERVE_TOKENS, model_window - OUTPUT_RESERVE_TOKENS))
    if not limits:
        return None
    return min(limits)
