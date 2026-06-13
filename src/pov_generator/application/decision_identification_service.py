"""Выявление решений на уровне отдельной задачи (v3.6).

ИСТОРИЯ. Раньше назывался ``decision_planning_service`` и работал по
парадигме «предварительного планирования»: LLM спрашивали «какие решения ТЫ
примешь, чтобы написать этот артефакт». Парадигма оказалась
архитектурно неверной — она генерировала **меta-решения** (формат,
глубина разделов, оформление) и дубли между задачами. Полный анализ —
см. ``docs/decision_subsystem_design_v3.6.md``.

ЧТО ЗДЕСЬ СЕЙЧАС. Этот сервис — **первый из трёх источников** реестра
решений (см. также :class:`DecisionExtractionService` и
:class:`PhaseGapAnalysisService`):

    1. **Task-level identification** (этот сервис) — на запуске задачи
       выявляем **новые** *проектные* развилки, которые именно эта
       задача обнажает и которых ещё нет в реестре. Знает текущий
       реестр; не дублирует.

    2. *Post-artifact extraction* — после генерации артефакта вытаскиваем
       неявные допущения, которые LLM «прорастила» в финальный текст.
       Покрывает молчаливые тех-решения (выбор фреймворка, библиотеки и
       т.п.), которые никогда не обсуждаются явно.

    3. *Phase gap analysis* — на границе фазы проекта (понимание / дизайн /
       поставка) проверяем покрытие по обязательным категориям и
       выявляем критичные **пробелы** в реестре.

ОБЛАСТЬ ОТВЕТСТВЕННОСТИ ЭТОГО МОДУЛЯ.

* НЕ работаем со всеми задачами подряд — только с теми, у которых в
  YAML-шаблоне поле ``decision_identification`` не отключено явно. См.
  ``TaskTemplate.decision_identification_enabled`` и whitelist-проверку
  в ``ExecutionService``. Чистые transform-задачи (request_fact_extraction,
  glossary_drafting, merge-задачи, review-задачи) выключены — они
  ничего нового по проекту не решают.

* Знаем существующий реестр проекта и передаём список title'ов в промпт.
  LLM явно инструктируется не дублировать.

* Промпт сфокусирован на **content-decisions** (что мы строим, как,
  для кого) и явно отвергает мета (формат, глубина разделов, оформление).

* Требуем category (`scope` | `tech_stack` | `data` | `integration` |
  `acceptance` | `risk` | `stakeholder` | `budget` | `team`) — это
  ОБЯЗАТЕЛЬНОЕ поле каждого решения. Заставляет LLM думать «это про
  проект или про оформление», даёт structural-фильтр на нашей стороне.

Спецификация: ``specs/12_clarification_escalation.md`` раздел v3.6.
Критерии классификации уровня: ``docs/decision_level_criteria.md``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..common.errors import ConflictError, ProviderExhaustedError
from ..common.llm_modes import plain_json_scope
from ..common.serialization import utc_now_iso
from ..domain.decisions import (
    DECISION_CATEGORIES,
    SOURCE_IDENTIFICATION,
    Decision,
    light_decision_item_schema,
    strip_decision_category_prefix,
)
from ..domain.llm_settings import PURPOSE_DECISION_PLANNING
from ..infrastructure.llm import LLMProviderRegistry
from .decision_light_parsing import light_alternatives, resolve_recommended_option_id

logger = logging.getLogger(__name__)


# Сложность задачи для resolve_for_purpose. Выявление решений — это
# структурное перечисление, не глубокий анализ. Standard уровня достаточно;
# на Claude-провайдерах маппится на sonnet (см. claude_sdk_client).
_IDENTIFICATION_COMPLEXITY = "standard"


@dataclass(frozen=True)
class IdentificationResult:
    """Результат выявления решений для одной задачи.

    Содержит готовые к сохранению ``Decision`` объекты со статусом
    ``proposed``. Каждый имеет ``source = "identification"`` (выявление
    решений до сборки артефакта) и привязку к задаче через ``source_task_id``.

    Args:
        decisions: упорядоченный список выявленных решений.
        provider: имя LLM-провайдера, использованного для выявления.
        model: модель.
        raw_response: исходный JSON от LLM (для аудита и отладки).
        token_usage: usage от провайдера (input_tokens, output_tokens,
            total_tokens, ...). Пустой dict, если провайдер не сообщает.
    """

    decisions: tuple[Decision, ...]
    provider: str
    model: str
    raw_response: dict[str, Any]
    token_usage: dict[str, Any] = field(default_factory=dict)


# Обратная совместимость для подписчиков, ещё импортирующих старое имя.
# Удалить после миграции внешних модулей (несколько коммитов спустя).
PlanningResult = IdentificationResult


# ---------------------------------------------------------------------------
# Критерии классификации уровня (компактная встраиваемая версия)
# ---------------------------------------------------------------------------

_LEVEL_CRITERIA_PROMPT = """\
КРИТЕРИИ КЛАССИФИКАЦИИ РЕШЕНИЙ ПО УРОВНЮ ВОВЛЕЧЕНИЯ.

Уровень "business" — решение, которое:
- меняет ЧТО мы строим (цель, scope, аудитория, ценностное предложение);
- видимо и значимо для бизнес-заказчика или конечного пользователя;
- требует бизнес-суждения, не сводится к технической экспертизе;
- цена пересмотра — на уровне переговоров с заказчиком.
Примеры: целевая аудитория, формулировка бизнес-цели, граница пилота,
способ монетизации, включать ли мобильное приложение в scope.

Уровень "architecture" — решение, которое:
- определяет, КАК устроено решение в целом;
- затрагивает несколько компонентов или подсистем;
- цена пересмотра — значительная переделка нескольких компонентов;
- технический выбор с долгоиграющими последствиями.
Примеры: выбор СУБД, REST vs GraphQL, монолит vs микросервисы,
sync vs async, выбор облака/on-premise, стратегия аутентификации.

Уровень "detail" — решение, которое:
- касается одного компонента / одного параметра / одной строки контракта;
- цена пересмотра — небольшой рефакторинг, легко обратимо;
- чисто исполнительский выбор без значимых trade-off.
Примеры: naming convention для endpoints, конкретные значения thresholds,
включать ли stack trace в логи по умолчанию, выбор версии библиотеки.

ПРАВИЛА ТАЙ-БРЕЙКЕРА:
- Если выглядит технически, но изменение меняет бизнес-обещания заказчику — это "business".
- Если применяется один раз и зашивается во много мест — это "architecture", не "detail".
- Если решение практически необратимо (миграция данных, разрыв контрактов) — поднимай уровень минимум на одну ступень.
- При сомнении между двумя уровнями — выбирай ВЫШЕ. Пользователю безопаснее увидеть лишнее, чем пропустить важное.
"""


# Промпт — «что считать проектным решением». Сформулирован v3.6 как
# **переориентация вопроса**: не «что ТЫ будешь решать», а «какие
# *проектные* развилки этой задачи ещё нет в реестре». Это убирает
# meta-шум на уровне самого вопроса. Few-shot anti-examples фиксируют
# границу: meta-решения о формате/глубине/оформлении документа —
# не проектные, никогда не выноси.
_PROJECT_DECISION_DEFINITION = """\
ЧТО СЧИТАЕМ «ПРОЕКТНЫМ РЕШЕНИЕМ» (только такие выносим в реестр).

Проектное решение — это **развилка**, где система могла бы пойти иначе,
и это **изменило бы сам проект** (его scope, технический подход, данные,
интеграции, риски).

ВЫНОСИ (это проектные):
- «Какой LLM-стек для on-prem (Llama-3 8B vs Qwen 7B vs ruT5)»
- «Какой OCR-движок для Актов Н-1 (Tesseract vs PaddleOCR vs Yandex Vision)»
- «Какие модули включить в PoV (только Модуль 1, только Модуль 2, оба)»
- «Стратегия обезличивания PII (псевдонимизация vs полная анонимизация)»
- «Состав интеграций на пилоте (только email / email+СОТ / полный контур)»
- «Архитектурный стиль обработки (синхронный vs асинхронный конвейер)»

НЕ ВЫНОСИ (это решают ВНЕ системы — коммерция / юридическое / организация):
- бюджет, стоимость, цены;
- сроки, даты, длительности, календарь;
- права на результаты / интеллектуальную собственность, лицензирование;
- гарантии сторон, обязательства, период поддержки;
- формальная приёмка и КТО подписывает (акты, подписанты, Go/No-Go-комитет);
- состав и роли команды подрядчика.
Система не обладает компетенцией по этим вопросам и не знает внешних
договорённостей — не выноси их в реестр ни под каким видом.

НЕ ВЫНОСИ (это meta/процесс/оформление):
- «Глубина детализации блока X в артефакте» → ни на что в проекте не влияет
- «Уровень детализации экранов в outline» → это формат документа
- «Включение раздела Y в этот артефакт» → формат документа
- «Состав ролей в actors-секции frontend_requirements» → формат документа
- «Глубина проверки кейс-специфичности» → процесс ревью, не проект
- «Как фиксировать числа с диапазонами» → стиль письма
- «Триггер rejection при провале OCR» → процесс валидации артефакта
- «Состав отложенных решений в артефакте» → мета о самом реестре
- «Подтвердить ли upstream-рекомендацию» → процессная механика, не проект

ПРОВЕРКА. Прежде чем выносить решение, спроси себя:
1. «Если переключить это решение, изменится ли что-то, что увидит
   ЗАКАЗЧИК в пилоте или в production?» — если нет, это НЕ решение
   по проекту, не выноси.
2. «Это про содержание проекта или про оформление документа?» — если
   про оформление, не выноси.
3. «Это коммерческий / юридический / организационный вопрос (бюджет,
   сроки, права на результаты, гарантии, подписанты, состав команды)?» —
   если да, не выноси: это решают вне системы.
"""


# ---------------------------------------------------------------------------
# Schema для structured output
# ---------------------------------------------------------------------------


def _build_identification_schema() -> dict[str, Any]:
    """JSON-schema ответа выявления решений — обёртка над ЕДИНОЙ облегчённой
    схемой решения (:func:`light_decision_item_schema`), общей с emergent-путём.

    ``maxItems: 5`` — кэп, чтобы модель не «добивала» список мелочью. Богатый
    domain заполняется маппингом (см. ``decision_light_parsing``)."""
    return {
        "type": "object",
        "required": ["decisions"],
        "additionalProperties": False,
        "properties": {
            "decisions": {
                "type": "array",
                "maxItems": 5,
                "description": "0-5 НОВЫХ проектных решений. Лучше 0, чем мета-шум.",
                "items": light_decision_item_schema(),
            }
        },
    }


# ---------------------------------------------------------------------------
# Сервис
# ---------------------------------------------------------------------------


class DecisionIdentificationService:
    """Выявление новых проектных решений на уровне одной задачи (v3.6).

    Использует :class:`LLMProviderRegistry` для разрешения провайдера через
    purpose ``PURPOSE_DECISION_PLANNING`` (имя purpose сохраняем — оно
    зашито в settings, а ребрендинг не функциональный).

    Ключевое отличие от старой реализации: знает существующий реестр
    проекта и инструктирует LLM не дублировать уже принятые решения.
    Это убирает дубли при кросс-task анализе одного проекта.
    """

    def __init__(self, *, llm_registry: LLMProviderRegistry | None = None) -> None:
        self._llm = llm_registry or LLMProviderRegistry()

    def identify_for_task(
        self,
        *,
        project_id: str,
        task_id: str,
        task_title: str,
        artifact_role: str,
        task_summary: str,
        context_text: str,
        existing_registry_titles: tuple[str, ...] = (),
        provider: str | None = None,
        model: str | None = None,
    ) -> IdentificationResult:
        """Выявить **новые проектные решения** для этой задачи.

        Args:
            project_id: проект (для проставления в Decision).
            task_id: задача (для source_task_id в Decision).
            task_title: человекочитаемое название задачи (для UI checkpoint).
            artifact_role: какой артефакт будет сгенерирован.
            task_summary: краткое описание того, что должна сделать задача.
            context_text: уже подготовленный контекст задачи (бизнес-запрос,
                upstream-артефакты, decisions, факты). Передаётся как
                единый текстовый блок — без особых разбиений.
            existing_registry_titles: title'ы уже существующих в реестре
                проекта решений. LLM получает их в промпте с инструкцией
                «не дублируй». v3.6 — главный механизм борьбы с
                кросс-task дублями.
            provider: явный override провайдера (тесты / CLI). Если None —
                берётся из settings-store через purpose.
            model: явный override модели.

        Returns:
            IdentificationResult с готовыми Decision-объектами
            (status="proposed", source="identification"). Сохранение в реестр —
            ответственность вызывающего кода.

        Raises:
            ConflictError: если LLM-провайдер не настроен или вернул
                нечитаемый ответ.
        """
        if provider is not None:
            llm = self._llm.get(
                provider=provider,
                model=model,
                complexity=_IDENTIFICATION_COMPLEXITY,
            )
        else:
            # Резолв: сначала пробуем purpose decision_planning (имя оставлено
            # из истории; функционально — purpose «выявление»). При его
            # отсутствии тихо падаем на execution.standard.
            try:
                llm = self._llm.resolve_for_purpose(
                    PURPOSE_DECISION_PLANNING,
                    complexity=_IDENTIFICATION_COMPLEXITY,
                    override_model=model,
                )
            except ConflictError as primary_exc:
                logger.info(
                    "PURPOSE_DECISION_PLANNING не настроен (%s); fallback на "
                    "execution.standard для выявления решений",
                    primary_exc,
                )
                llm = self._llm.resolve_for_purpose(
                    "execution",
                    complexity="standard",
                    override_model=model,
                )

        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(
            task_title=task_title,
            artifact_role=artifact_role,
            task_summary=task_summary,
            context_text=context_text,
            existing_registry_titles=existing_registry_titles,
        )
        schema = _build_identification_schema()

        # Plain-режим: один проход (schema-в-промпте, без strict multi-turn
        # coercion и без compositional-декомпозиции). Выявление решений —
        # best-effort на дешёвой модели; форму ответа добивают tolerant-разбор и
        # нормализация. Это убирает strict-штормы, бывшие главной статьёй времени.
        try:
            with plain_json_scope():
                result = llm.chat_json(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    schema=schema,
                )
        except ProviderExhaustedError:
            # Исчерпание квоты — фатально для прогона: пробрасываем как есть,
            # чтобы раннер остановил пайплайн (НЕ глушим best-effort skip'ом).
            raise
        except Exception as exc:  # noqa: BLE001
            raise ConflictError(
                f"Ошибка выявления решений через {llm.name}: {exc}"
            ) from exc
        response = result.payload

        decisions = self._build_decisions(
            response=response,
            project_id=project_id,
            task_id=task_id,
        )
        usage = result.usage
        usage_dict = (
            {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_tokens or 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": usage.reasoning_tokens or 0,
                "call_count": usage.call_count,
                "retry_count": usage.retry_count,
                "total_tokens": usage.total_tokens,
            }
            if usage is not None
            else {}
        )
        return IdentificationResult(
            decisions=decisions,
            provider=llm.name,
            model=llm.model,
            raw_response=response,
            token_usage=usage_dict,
        )

    # Обратная совместимость для подписчиков, ещё вызывающих старый метод.
    def plan_for_task(self, **kwargs: Any) -> IdentificationResult:
        """Deprecated alias — см. :meth:`identify_for_task`."""
        return self.identify_for_task(**kwargs)

    # ---- prompt building --------------------------------------------------

    def _build_system_prompt(self) -> str:
        """Системный промпт для выявления решений (v3.6).

        Жёстко переориентирован относительно v3.5: вопрос больше не «что
        ТЫ решишь, чтобы написать артефакт», а «какие *проектные*
        развилки этой задачи ещё не в реестре».
        """
        return (
            "<role>\n"
            "Ты — аналитик, который ведёт реестр решений по PoV-проекту. Твоя "
            "задача — на запуске очередной задачи проекта выявить **новые** "
            "проектные развилки, которые она обнажает и которые пользователь "
            "должен увидеть в реестре.\n"
            "</role>\n\n"

            "<purpose>\n"
            "Ты НЕ собираешь артефакт сам. Ты только говоришь: 'в ходе этой "
            "задачи возникают проектные решения X, Y, Z; их ещё нет в "
            "реестре; вот мои предложения дефолтов'. Дальше другой LLM-вызов "
            "соберёт артефакт с зафиксированными решениями.\n"
            "</purpose>\n\n"

            f"{_PROJECT_DECISION_DEFINITION}\n\n"

            f"{_LEVEL_CRITERIA_PROMPT}\n\n"

            "<requirements>\n"
            "Для каждого решения возвращай:\n"
            "- title: короткое название (3-7 слов), отражающее суть выбора.\n"
            "- description: 1-3 предложения о том, что именно решается. Без воды.\n"
            "- category: одна из категорий (см. enum). Если ни одна не "
            "подходит — это **сигнал, что решение НЕ проектное**, не выноси.\n"
            "- alternatives: ОБЯЗАТЕЛЬНО минимум 2 и обычно 2-4 варианта. "
            "Каждый — реальный, содержательный, осмысленно отличающийся, с "
            "label (короткое имя) и description (1-2 предложения). ЗАПРЕЩЕНО "
            "плодить заглушки вида «принять рекомендацию», «оставить как есть».\n"
            "- recommended: label того варианта, который ты считаешь лучшим по "
            "умолчанию. ТОЧНО совпадает с label одного из alternatives.\n"
            "- rationale: почему именно этот вариант — дефолт. Конкретно, со "
            "ссылкой на контекст или принципы.\n"
            "- level: business / architecture / detail по критериям выше.\n"
            "</requirements>\n\n"

            "<dedupe>\n"
            "В user-промпте даны title'ы уже существующих в реестре решений "
            "(блок «Уже в реестре»). НЕ выноси решения с похожим смыслом — "
            "даже если формулировка отличается. Если в реестре уже есть "
            "«Длительность пилота», не дублируй её под названием «Сколько "
            "недель идёт PoV».\n"
            "</dedupe>\n\n"

            "<output_contract>\n"
            "Верни ТОЛЬКО валидный JSON по приложенной схеме. Без markdown-"
            "обёрток, без префиксов, без комментариев. Количество решений — "
            "от 0 до 5. **Лучше вернуть 0 решений, чем мета-шум**.\n"
            "</output_contract>"
        )

    def _build_user_prompt(
        self,
        *,
        task_title: str,
        artifact_role: str,
        task_summary: str,
        context_text: str,
        existing_registry_titles: tuple[str, ...] = (),
    ) -> str:
        """User-промпт: контекст задачи + текущий реестр + запрос на выявление."""
        # v3.6: передаём текущий реестр прямо в промпт. Лимитируем 60 шт
        # чтобы prompt не разрастался; берём самые свежие (это, как правило,
        # самые релевантные для следующей задачи).
        registry_block = ""
        if existing_registry_titles:
            recent = existing_registry_titles[-60:]
            bullets = "\n".join(f"- {t}" for t in recent)
            registry_block = (
                f"### Уже в реестре ({len(existing_registry_titles)} решений; "
                f"показаны последние {len(recent)})\n"
                f"{bullets}\n\n"
                f"НЕ дублируй эти решения. Если по смыслу совпадает — "
                f"пропусти и не выноси.\n\n"
            )

        return (
            f"### Задача\n"
            f"**Название:** {task_title}\n"
            f"**Тип артефакта:** {artifact_role}\n"
            f"**Что должна сделать:** {task_summary}\n\n"
            f"{registry_block}"
            f"### Контекст\n"
            f"{context_text}\n\n"
            f"### Запрос\n"
            f"Перечисли **новые проектные** решения, которые возникают на "
            f"этой задаче и ещё НЕ присутствуют в реестре. Для каждого — "
            f"category из enum, дефолт с обоснованием, уровень. "
            f"Лучше 0 решений, чем шум."
        )

    # ---- response parsing -------------------------------------------------

    def _build_decisions(
        self,
        *,
        response: dict[str, Any],
        project_id: str,
        task_id: str,
    ) -> tuple[Decision, ...]:
        """Превратить JSON-ответ LLM в список доменных Decision."""
        raw_decisions = response.get("decisions") or []
        if not isinstance(raw_decisions, list):
            raise ConflictError(
                f"identification: поле 'decisions' должно быть массивом, "
                f"получено {type(raw_decisions).__name__}"
            )

        now = utc_now_iso()
        decisions: list[Decision] = []
        for raw in raw_decisions:
            if not isinstance(raw, dict):
                continue
            try:
                decision = self._build_single_decision(
                    raw=raw,
                    project_id=project_id,
                    task_id=task_id,
                    now=now,
                )
            except (KeyError, TypeError, ValueError):
                continue
            decisions.append(decision)
        return tuple(decisions)

    def _build_single_decision(
        self,
        *,
        raw: dict[str, Any],
        project_id: str,
        task_id: str,
        now: str,
    ) -> Decision:
        # Облегчённая схема (alternatives={label, description}) → богатый domain
        # через ОБЩИЙ маппинг (decision_light_parsing), единый с emergent-путём:
        # option_id=opt-N, pros/cons пустые, confidence=None.
        alternatives = light_alternatives(raw.get("alternatives"))
        if len(alternatives) < 2:
            raise ValueError("decision требует минимум 2 альтернативы")

        # category обязательна и из enum — структурный anti-meta фильтр. Значение
        # вне enum трактуем как сигнал «решение не проектное» → отбрасываем.
        category = str(raw.get("category") or "").strip()
        if category not in DECISION_CATEGORIES:
            raise ValueError(
                f"decision: category должен быть одной из {DECISION_CATEGORIES}, "
                f"получено {category!r}"
            )

        chosen = resolve_recommended_option_id(alternatives, raw.get("recommended"))

        level = raw.get("level")
        if level not in ("business", "architecture", "detail"):
            level = "architecture"

        description = strip_decision_category_prefix(str(raw.get("description") or ""))

        return Decision(
            decision_id=str(uuid.uuid4()),
            project_id=project_id,
            title=str(raw.get("title") or "Untitled decision"),
            description=description,
            category=category,
            chosen_option_id=chosen,
            alternatives=alternatives,
            rationale=str(raw.get("rationale") or ""),
            level=level,  # type: ignore[arg-type]
            level_rationale="",  # облегчённая схема не запрашивает — не критично для ценности
            confidence=0.5,  # дефолт: уверенность отдельным полем больше не запрашиваем
            status="proposed",
            source=SOURCE_IDENTIFICATION,
            source_task_id=task_id,
            created_at=now,
            updated_at=now,
        )


# Обратная совместимость для подписчиков, ещё импортирующих старое имя.
DecisionPlanningService = DecisionIdentificationService
