"""Pre-flight планирование решений перед генерацией артефакта (v3.0).

Это первая стадия трёх-стадийного pipeline:
    1. Pre-flight planning (этот сервис) — перечислить решения, которые
       LLM собирается принять при сборке артефакта.
    2. Checkpoint пользователю (CheckpointService) — отфильтровать по
       уровню режима, показать в UI, дождаться ответов.
    3. Locked-in генерация (ExecutionService) — собрать артефакт, имея
       решения как зафиксированные ограничения.

Этот сервис отвечает только за стадию 1. Возвращает список ``Decision``-
объектов со статусом ``proposed``. Не сохраняет их в реестр — это делает
вызывающий код после фильтрации по режиму (Decisions ниже уровня
пользователя сразу попадают в реестр как ``accepted_default``;
выше — попадают через checkpoint).

Спецификация: ``specs/12_clarification_escalation.md`` раздел v3.0.
Критерии классификации: ``docs/decision_level_criteria.md``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from ..common.errors import ConflictError
from ..common.serialization import utc_now_iso
from ..domain.decisions import Decision, DecisionAlternative
from ..domain.llm_settings import PURPOSE_DECISION_PLANNING
from ..infrastructure.llm import LLMProviderRegistry


# Сложность задачи для resolve_for_purpose. Planning — это структурное
# перечисление решений, не глубокий анализ. Standard уровня достаточно,
# а на Claude-провайдерах маппится на sonnet (см. claude_sdk_client).
_PLANNING_COMPLEXITY = "standard"


@dataclass(frozen=True)
class PlanningResult:
    """Результат pre-flight планирования для одной задачи.

    Содержит готовые к сохранению ``Decision`` объекты со статусом
    ``proposed``. Каждый имеет ``source = "pre_flight"`` и привязку к
    задаче через ``source_task_id``. Без ``project_id`` — выставляется
    выше по стеку при сохранении.

    Args:
        decisions: упорядоченный список планируемых решений.
        provider: имя LLM-провайдера, использованного для планирования.
        model: модель.
        raw_response: исходный JSON от LLM (для аудита и отладки).
    """

    decisions: tuple[Decision, ...]
    provider: str
    model: str
    raw_response: dict[str, Any]


# ---------------------------------------------------------------------------
# Критерии классификации (короткая встраиваемая версия)
# ---------------------------------------------------------------------------
#
# Полные критерии в docs/decision_level_criteria.md. Здесь — компактная
# выжимка, помещаемая в системный промпт LLM. Меняется редко и осознанно;
# любое изменение требует параллельной правки docs/ (там — для людей,
# здесь — для LLM, и они должны совпадать).

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


# ---------------------------------------------------------------------------
# Schema для structured output
# ---------------------------------------------------------------------------


def _build_planning_schema() -> dict[str, Any]:
    """JSON-schema для ответа LLM в pre-flight планировании.

    Возвращает массив decisions, каждое — с полным набором полей,
    необходимых для создания доменного Decision. Без лишних полей,
    чтобы LLM не плодила опциональщину.
    """
    return {
        "type": "object",
        "required": ["decisions"],
        "additionalProperties": False,
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "title",
                        "description",
                        "alternatives",
                        "proposed_option_id",
                        "rationale",
                        "level",
                        "level_rationale",
                        "confidence",
                    ],
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "alternatives": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "required": ["option_id", "label", "description"],
                                "additionalProperties": False,
                                "properties": {
                                    "option_id": {"type": "string"},
                                    "label": {"type": "string"},
                                    "description": {"type": "string"},
                                    "pros": {"type": "array", "items": {"type": "string"}},
                                    "cons": {"type": "array", "items": {"type": "string"}},
                                    "confidence": {"type": ["number", "null"]},
                                },
                            },
                        },
                        "proposed_option_id": {"type": "string"},
                        "rationale": {"type": "string"},
                        "level": {
                            "type": "string",
                            "enum": ["business", "architecture", "detail"],
                        },
                        "level_rationale": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                },
            }
        },
    }


# ---------------------------------------------------------------------------
# Сервис
# ---------------------------------------------------------------------------


class DecisionPlanningService:
    """Pre-flight планирование решений через LLM.

    Использует :class:`LLMProviderRegistry` для разрешения провайдера и
    модели через purpose ``PURPOSE_DECISION_PLANNING``. Конкретный
    провайдер настраивается через UI настроек (см. settings tab).
    """

    def __init__(self, *, llm_registry: LLMProviderRegistry | None = None) -> None:
        self._llm = llm_registry or LLMProviderRegistry()

    def plan_for_task(
        self,
        *,
        project_id: str,
        task_id: str,
        task_title: str,
        artifact_role: str,
        task_summary: str,
        context_text: str,
        provider: str | None = None,
        model: str | None = None,
    ) -> PlanningResult:
        """Спланировать решения для задачи через pre-flight LLM-вызов.

        Args:
            project_id: проект (для проставления в Decision).
            task_id: задача (для source_task_id в Decision).
            task_title: человекочитаемое название задачи (для UI checkpoint).
            artifact_role: какой артефакт будет сгенерирован.
            task_summary: краткое описание того, что должна сделать задача.
            context_text: уже подготовленный контекст задачи (бизнес-запрос,
                upstream-артефакты, decisions, факты). Передаётся как
                единый текстовый блок — без особых разбиений.
            provider: явный override провайдера (тесты / CLI). Если None —
                берётся из settings-store через purpose.
            model: явный override модели.

        Returns:
            PlanningResult с готовыми Decision-объектами (status="proposed",
            source="pre_flight"). Сохранение в реестр — ответственность
            вызывающего кода.

        Raises:
            ConflictError: если LLM-провайдер не настроен или вернул
                нечитаемый ответ.
        """
        if provider is not None:
            llm = self._llm.get(
                provider=provider,
                model=model,
                complexity=_PLANNING_COMPLEXITY,
            )
        else:
            llm = self._llm.resolve_for_purpose(
                PURPOSE_DECISION_PLANNING,
                complexity=_PLANNING_COMPLEXITY,
                override_model=model,
            )

        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(
            task_title=task_title,
            artifact_role=artifact_role,
            task_summary=task_summary,
            context_text=context_text,
        )
        schema = _build_planning_schema()

        try:
            response = llm.chat_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema=schema,
                tool_name="plan_decisions",
                tool_description=(
                    "Перечислить решения, которые исполнитель задачи "
                    "собирается принять при сборке артефакта."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            raise ConflictError(
                f"Ошибка pre-flight планирования через {llm.name}: {exc}"
            ) from exc

        decisions = self._build_decisions(
            response=response,
            project_id=project_id,
            task_id=task_id,
        )
        return PlanningResult(
            decisions=decisions,
            provider=llm.name,
            model=llm.model,
            raw_response=response,
        )

    # ---- prompt building --------------------------------------------------

    def _build_system_prompt(self) -> str:
        """Системный промпт для pre-flight планирования.

        Жёстко структурирован, чтобы LLM не уходила в свободный анализ.
        Главное требование: для каждого решения — обоснованный уровень
        по критериям.
        """
        return (
            "<role>\n"
            "Ты — pre-flight планировщик решений в системе генерации проектной "
            "документации. Твоя задача — перед сборкой артефакта перечислить "
            "решения, которые исполнитель будет принимать, и предложить по "
            "каждому осмысленный выбор по умолчанию.\n"
            "</role>\n\n"

            "<purpose>\n"
            "Ты НЕ собираешь артефакт сам. Ты только говоришь: 'для этой "
            "задачи нужно будет принять решения X, Y, Z; вот мои предложения; "
            "пользователь может оставить или поправить'. Дальше другой LLM-вызов "
            "соберёт артефакт с зафиксированными решениями.\n"
            "</purpose>\n\n"

            f"{_LEVEL_CRITERIA_PROMPT}\n\n"

            "<requirements>\n"
            "Для каждого решения возвращай:\n"
            "- title: короткое название (3-7 слов), отражающее суть выбора.\n"
            "- description: 1-3 предложения о том, что именно решается. Без воды.\n"
            "- alternatives: минимум 1, обычно 2-4 варианта. Каждый — с label, "
            "description (1-2 предложения), и опционально pros/cons.\n"
            "- proposed_option_id: id того варианта, который ты считаешь лучшим "
            "по умолчанию. Должен совпадать с option_id одного из alternatives.\n"
            "- rationale: почему именно этот вариант — дефолт. Конкретно, "
            "со ссылкой на контекст или принципы. Не 'потому что это хорошо'.\n"
            "- level: business / architecture / detail по критериям выше.\n"
            "- level_rationale: 1-2 предложения, почему именно этот уровень, "
            "а не соседний. Это аудит-trail для пользователя.\n"
            "- confidence: 0..1, насколько ты уверена в предложенном дефолте. "
            "Низкая уверенность (< 0.5) подсветит решение как рискованное.\n"
            "</requirements>\n\n"

            "<what_to_skip>\n"
            "НЕ включай в план:\n"
            "- решения, которые уже зафиксированы в контексте (явные решения "
            "пользователя, ранее принятые decisions, явные требования);\n"
            "- косметику и мелочи без trade-off (это шум, не решения);\n"
            "- 'решения' уровня 'надо подумать ещё' — это не решения.\n"
            "</what_to_skip>\n\n"

            "<output_contract>\n"
            "Верни ТОЛЬКО валидный JSON по приложенной схеме. Без markdown-"
            "обёрток, без префиксов, без комментариев. Количество решений — "
            "от 0 (нечего планировать сверх контекста) до ~10. Если решений "
            "много — это сигнал слишком крупной задачи; ограничься 7-8 самыми "
            "значимыми.\n"
            "</output_contract>"
        )

    def _build_user_prompt(
        self,
        *,
        task_title: str,
        artifact_role: str,
        task_summary: str,
        context_text: str,
    ) -> str:
        """User-промпт: контекст задачи + явный запрос на планирование."""
        return (
            f"### Задача\n"
            f"**Название:** {task_title}\n"
            f"**Тип артефакта:** {artifact_role}\n"
            f"**Что должна сделать:** {task_summary}\n\n"
            f"### Контекст\n"
            f"{context_text}\n\n"
            f"### Запрос\n"
            f"Перечисли решения, которые исполнитель этой задачи будет "
            f"принимать при сборке артефакта `{artifact_role}`. Для каждого "
            f"предложи дефолтный выбор с обоснованием и классифицируй по уровню."
        )

    # ---- response parsing -------------------------------------------------

    def _build_decisions(
        self,
        *,
        response: dict[str, Any],
        project_id: str,
        task_id: str,
    ) -> tuple[Decision, ...]:
        """Превратить JSON-ответ LLM в список доменных Decision.

        Защитное чтение: если LLM вернула невалидный список, поле или
        ссылку на несуществующий option_id — пропускаем элемент с
        логом-warning'ом, не валим весь pipeline.
        """
        raw_decisions = response.get("decisions") or []
        if not isinstance(raw_decisions, list):
            raise ConflictError(
                f"pre-flight: поле 'decisions' должно быть массивом, получено {type(raw_decisions).__name__}"
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
                # Один битый элемент не должен ломать остальные. Лог
                # ответственности вызывающего слоя (тот видит raw_response).
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
        # Альтернативы. proposed_option_id обязан быть среди них.
        raw_alts = raw.get("alternatives") or []
        alternatives = tuple(
            DecisionAlternative(
                option_id=str(alt["option_id"]),
                label=str(alt.get("label", "")),
                description=str(alt.get("description", "")),
                pros=tuple(alt.get("pros") or ()),
                cons=tuple(alt.get("cons") or ()),
                confidence=(
                    float(alt["confidence"])
                    if alt.get("confidence") is not None
                    else None
                ),
            )
            for alt in raw_alts
            if isinstance(alt, dict) and "option_id" in alt
        )
        if not alternatives:
            raise ValueError("decision без альтернатив")

        proposed = str(raw.get("proposed_option_id") or "")
        # Если LLM указала несуществующий option_id — fallback на первую
        # альтернативу. Сигнал в level_rationale не пишем, чтобы не
        # засорять; вызывающий код видит несоответствие через raw_response.
        if proposed not in {alt.option_id for alt in alternatives}:
            proposed = alternatives[0].option_id

        level = raw.get("level")
        if level not in ("business", "architecture", "detail"):
            # Невалидный уровень — fallback на architecture (наиболее
            # консервативно: вынесет вопрос в режимах control+expert,
            # скроет в balanced).
            level = "architecture"

        return Decision(
            decision_id=str(uuid.uuid4()),
            project_id=project_id,
            title=str(raw.get("title") or "Untitled decision"),
            description=str(raw.get("description") or ""),
            chosen_option_id=proposed,
            alternatives=alternatives,
            rationale=str(raw.get("rationale") or ""),
            level=level,  # type: ignore[arg-type]
            level_rationale=str(raw.get("level_rationale") or ""),
            confidence=float(raw.get("confidence") or 0.5),
            status="proposed",
            source="pre_flight",
            source_task_id=task_id,
            created_at=now,
            updated_at=now,
        )
