"""Phase-boundary gap analysis (v3.6) — источник 3 в подсистеме реестра.

ИДЕЯ. У task-level identification (источник 1) есть слепая зона: оно
видит **только** одну задачу за раз, не знает «целиком, какие проектные
решения должны быть в этой фазе». Post-artifact extraction (источник 2)
видит только содержимое отдельного артефакта.

Phase gap analysis закрывает это: на **границе фазы** проекта (когда
все задачи фазы завершены) делается один большой LLM-вызов с двумя
входами:

  1. Полный реестр текущих решений проекта.
  2. Чек-лист «что обязательно должно быть закрыто на этой фазе»
     (категории и темы, специфичные фазе).

И вопрос: «какие **критичные** проектные решения этой фазы ещё НЕ
закрыты в реестре?» Найденные пробелы — это пропущенные тех/scope/
acceptance выборы, которые без явного решения «соберутся» имплицитно
в финальном артефакте и сюрпризнут пользователя.

ФАЗЫ (v3.6 — стартовая конфигурация; можно расширять без миграции):

  * **understanding** — «понять запрос». Активируется когда завершены
    задачи на template_ref'ах из ``_PHASE_TASK_TEMPLATES["understanding"]``.
    Закрывает категории: scope, stakeholder, acceptance (на верхнем уровне).
  * **design** — «спроектировать подход». Закрывает: tech_stack, data,
    integration, risk.
  * **delivery** — «спланировать поставку». Закрывает: acceptance
    (детальный), team, budget.

ПОЧЕМУ НА ГРАНИЦАХ ФАЗ, А НЕ НА КАЖДОЙ ЗАДАЧЕ. Per-task identification
видит локальный контекст и не может оценить полноту покрытия. Phase
gap analysis оперирует «глобальной картой» по фазе — это правильный
уровень для вопроса «всё ли важное обсудили». Кроме того, один большой
вызов на фазу заметно дешевле, чем 5-10 small per-task вызовов с
повторяющимся «глобальным» контекстом.

ИДЕМПОТЕНТНОСТЬ. Каждая фаза анализируется один раз. Маркер «фаза
проанализирована» хранится через source_task_id со специальным префиксом
``__phase_gap__:<phase_id>`` — это позволяет понять, что gap-analysis
уже отработала, не заводя отдельной таблицы.

Спецификация: ``specs/12_clarification_escalation.md`` раздел v3.6.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..common.errors import ConflictError
from ..common.serialization import utc_now_iso
from ..domain.decisions import Decision, DecisionAlternative
from ..domain.llm_settings import PURPOSE_DECISION_PLANNING
from ..infrastructure.llm import LLMProviderRegistry
from ..infrastructure.sqlite_runtime import SqliteRuntime
from .decision_identification_service import DECISION_CATEGORIES

logger = logging.getLogger(__name__)


_PHASE_GAP_COMPLEXITY = "standard"


# Маркер «фаза проанализирована». Сохраняется как source_task_id у
# Decision-записей, добавленных gap-анализом. Конкретное значение
# не используется в UI — только для idempotency-проверки.
_PHASE_MARKER_PREFIX = "__phase_gap__:"


# Конфиг фаз. Намеренно declarative: при добавлении новой задачи в
# проект нужно лишь обновить этот dict. Никаких подграфов/runtime-логики.
#
# Принцип группировки: какие task-templates вместе закрывают одну
# смысловую фазу проекта. Фаза «понять запрос» — про базовые входные
# данные; «спроектировать» — про техническую структуру; «спланировать
# поставку» — про acceptance/риски/команду.
_PHASE_TASK_TEMPLATES: dict[str, frozenset[str]] = {
    "understanding": frozenset(
        {
            "common.request_normalization",
            "common.request_fact_extraction",
            "common.ambiguity_gap_analysis",
            "common.goal_hypothesis",
            "common.constraint_inventory",
            "common.business_outcome_framing",
        }
    ),
    "design": frozenset(
        {
            "common.scope_boundary_definition",
            "common.stakeholder_mapping",
            "common.solution_option_inventory",
            "common.operating_model_outline",
            "common.deployment_topology",
            "common.solution_recommendation",
            "ml.predictive_problem_definition",
            "ml.data_landscape_assessment",
            "integration.operating_model",
            "security.constraints_assessment",
            "frontend.user_flow_analysis",
        }
    ),
    "delivery": frozenset(
        {
            "common.acceptance_model_definition",
            "common.delivery_scope_definition",
            "common.project_risk_register",
            "common.implementation_dependency_plan",
        }
    ),
}


# Что каждая фаза должна закрыть. Используется в промпте gap-анализа
# чтобы LLM искал именно эти категории. Стабильный список — общие
# проектные категории; не зависит от домена.
_PHASE_EXPECTED_CATEGORIES: dict[str, tuple[str, ...]] = {
    "understanding": ("scope", "stakeholder", "acceptance"),
    "design": ("tech_stack", "data", "integration", "risk"),
    "delivery": ("acceptance", "team", "budget"),
}


_PHASE_DESCRIPTIONS: dict[str, str] = {
    "understanding": (
        "ПОНИМАНИЕ ЗАПРОСА. К этой фазе должно быть зафиксировано: "
        "целевая аудитория, scope пилота, ключевые KPI и метод их замера, "
        "ключевые ограничения и допущения, состав основных стейкхолдеров."
    ),
    "design": (
        "ДИЗАЙН ПОДХОДА. К этой фазе должно быть зафиксировано: "
        "технологический стек (фреймворки, модели LLM/ML, СУБД), "
        "источники и обработка данных, состав интеграций, ключевые риски "
        "и митигации, целевая архитектура."
    ),
    "delivery": (
        "ПЛАНИРОВАНИЕ ПОСТАВКИ. К этой фазе должно быть зафиксировано: "
        "детальные критерии приёмки и Go/No-Go, состав команды и роли, "
        "оценка бюджета и сроков, фазировка работ."
    ),
}


_GAP_SYSTEM_PROMPT = """\
<role>
Ты — старший консультант, который ведёт реестр решений по PoV-проекту.
Сейчас закончилась одна из фаз проекта; ты смотришь на полный реестр
накопленных решений и ищешь **критичные пробелы** — проектные развилки,
которые на этой фазе должны были быть зафиксированы, но в реестре их
нет (или они есть, но в формулировке, далёкой от темы).
</role>

<purpose>
Это финальная страховка против ситуации «закончили фазу проектирования,
а где зашит выбор LLM-модели — никто не помнит». Пользователь
переключится в expert-режим в любой момент и увидит твои находки как
proposed-решения — сможет либо подтвердить дефолт, либо переиграть.
</purpose>

ЧТО ТАКОЕ «КРИТИЧНЫЙ ПРОБЕЛ».

Это **проектная развилка**, которая:
1. Должна быть закрыта на этой фазе по чек-листу категорий (см. user-промпт).
2. НЕ покрыта ни одной из существующих записей в реестре (по смыслу,
   не по формулировке).
3. Без явного решения «соберётся» молча в финальном артефакте и
   рискует быть сюрпризом для заказчика.

НЕ ВЫНОСИ:
- Развилки, которые уже есть в реестре (даже под другой формулировкой).
- Мета-вопросы (формат документа, оформление, глубина раздела).
- Тривиальные / реверсивные параметры.
- Развилки следующей фазы (их LLM закроет позже).

КОЛИЧЕСТВО. От 0 до 5 пробелов. Лучше 0, чем шум.

ФОРМАТ. Каждый пробел оформляется как новое Decision со своим title,
category (из enum), 2-3 альтернативами (включая дефолт), confidence
для каждой, level (business/architecture/detail), и rationale.

Верни ТОЛЬКО валидный JSON по схеме. Без markdown.
"""


def _build_gap_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["gaps"],
        "additionalProperties": False,
        "properties": {
            "gaps": {
                "type": "array",
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "required": [
                        "title",
                        "description",
                        "category",
                        "alternatives",
                        "proposed_option_id",
                        "rationale",
                        "level",
                        "confidence",
                    ],
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "category": {
                            "type": "string",
                            "enum": list(DECISION_CATEGORIES),
                        },
                        "alternatives": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 4,
                            "items": {
                                "type": "object",
                                "required": ["option_id", "label", "description", "confidence"],
                                "additionalProperties": False,
                                "properties": {
                                    "option_id": {"type": "string"},
                                    "label": {"type": "string"},
                                    "description": {"type": "string"},
                                    "pros": {"type": "array", "items": {"type": "string"}},
                                    "cons": {"type": "array", "items": {"type": "string"}},
                                    "confidence": {
                                        "type": "number",
                                        "minimum": 0.0,
                                        "maximum": 1.0,
                                    },
                                },
                            },
                        },
                        "proposed_option_id": {"type": "string"},
                        "rationale": {"type": "string"},
                        "level": {
                            "type": "string",
                            "enum": ["business", "architecture", "detail"],
                        },
                        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    },
                },
            }
        },
    }


@dataclass(frozen=True)
class PhaseGapResult:
    """Результат gap-анализа одной фазы.

    Args:
        phase_id: идентификатор фазы (understanding / design / delivery).
        gaps: вытащенные пробелы как Decision-записи (status="proposed",
            source="phase_gap_analysis", чтобы UI мог отдельно подсветить).
        already_done: True если фаза уже была проанализирована (никаких
            новых LLM-вызовов не делалось).
        token_usage: usage LLM-вызова. Пустой dict если already_done.
    """

    phase_id: str
    gaps: tuple[Decision, ...]
    already_done: bool
    token_usage: dict[str, Any] = field(default_factory=dict)


class PhaseGapAnalysisService:
    """Phase-boundary gap analysis (v3.6) — источник 3 реестра."""

    def __init__(
        self,
        runtime: SqliteRuntime,
        *,
        llm_registry: LLMProviderRegistry | None = None,
    ) -> None:
        self._runtime = runtime
        self._llm = llm_registry or LLMProviderRegistry()

    # ---- public API ------------------------------------------------------

    def maybe_run_phase_analysis(
        self,
        workspace: Path,
        *,
        project_id: str,
        completed_template_ref: str,
    ) -> tuple[PhaseGapResult, ...]:
        """Проверить, не завершилась ли одна (или несколько) фаз, и
        запустить gap-анализ для каждой завершённой.

        Вызывается из ExecutionService после успешного завершения task'а.
        Может прогнать сразу несколько фаз, если одна задача оказалась
        последней в нескольких (теоретически возможно при пересечениях,
        хотя обычно одна).

        Returns:
            Tuple результатов по каждой завершённой фазе. Пустой tuple
            если ни одна фаза не закрылась этим вызовом.
        """
        # Какие template_ref'ы уже закрыты в проекте?
        completed_refs = self._completed_template_refs(workspace, project_id)
        completed_refs_no_version = {ref.split("@", 1)[0] for ref in completed_refs}

        # Какие фазы только что замкнулись этим завершением задачи?
        completed_ref_no_version = completed_template_ref.split("@", 1)[0]
        out: list[PhaseGapResult] = []
        for phase_id, required_refs in _PHASE_TASK_TEMPLATES.items():
            if completed_ref_no_version not in required_refs:
                # эта фаза не зависит от текущего task'а — пропускаем
                continue
            if not required_refs.issubset(completed_refs_no_version):
                # фаза ещё не полная
                continue
            if self._is_phase_already_analyzed(workspace, project_id, phase_id):
                # уже была — не повторяемся
                out.append(
                    PhaseGapResult(phase_id=phase_id, gaps=(), already_done=True)
                )
                continue
            # Phase boundary just crossed — run analysis.
            try:
                result = self._analyze_phase(
                    workspace=workspace,
                    project_id=project_id,
                    phase_id=phase_id,
                )
                out.append(result)
            except Exception as exc:  # noqa: BLE001
                # best-effort: ошибка не должна валить workflow.
                logger.warning(
                    "Phase gap analysis failed for phase %s (project %s): %s",
                    phase_id, project_id, exc,
                )
        return tuple(out)

    # ---- internals -------------------------------------------------------

    def _completed_template_refs(
        self, workspace: Path, project_id: str
    ) -> set[str]:
        """Какие template_ref'ы успешно закрыты в проекте."""
        # Workspace = один проект → list_artifacts уже scoped к нему.
        # Смотрим существующие primary-артефакты — их metadata знает
        # template_ref. Это самый прямой признак «задача закрыта».
        artifacts = self._runtime.list_artifacts(workspace)
        return {
            (a.metadata.template_ref or "")
            for a in artifacts
            if a.metadata and a.metadata.template_ref
            and a.artifact_kind == "primary"
        }

    def _is_phase_already_analyzed(
        self, workspace: Path, project_id: str, phase_id: str
    ) -> bool:
        marker = f"{_PHASE_MARKER_PREFIX}{phase_id}"
        decisions = self._runtime.list_decisions(workspace, project_id=project_id)
        return any(d.source_task_id == marker for d in decisions)

    def _analyze_phase(
        self,
        *,
        workspace: Path,
        project_id: str,
        phase_id: str,
    ) -> PhaseGapResult:
        existing_decisions = self._runtime.list_decisions(
            workspace, project_id=project_id
        )
        registry_titles = tuple(d.title for d in existing_decisions)

        llm = self._resolve_llm()
        user_prompt = self._build_user_prompt(
            phase_id=phase_id,
            registry_titles=registry_titles,
        )
        schema = _build_gap_schema()

        try:
            response = llm.chat_json(
                system_prompt=_GAP_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                schema=schema,
            )
        except Exception as exc:  # noqa: BLE001
            raise ConflictError(
                f"Phase gap analysis ({phase_id}) failed: {exc}"
            ) from exc

        usage = getattr(llm, "last_usage", None)
        usage_dict = usage.to_dict() if usage is not None else {}

        gaps = self._build_gap_decisions(
            response=response,
            project_id=project_id,
            phase_id=phase_id,
        )

        # Сохраняем. Все идут как proposed — это пробелы, требующие
        # внимания. В autopilot всё равно автоматически примутся как
        # accepted_default (через CheckpointService.process_planned_decisions).
        # Но статус proposed нам нужен на момент сохранения, чтобы UI
        # подсветил «вышло gap-analysis» если пользователь в higher mode.
        saved: list[Decision] = []
        marker = f"{_PHASE_MARKER_PREFIX}{phase_id}"
        # Маркер всегда: либо как реальная decision-запись (если есть
        # gaps), либо как «пустая отметка» (если нет — выносим заглушку
        # с источником phase_gap_analysis и автоматически принятым
        # дефолтом). Это нужно для идемпотентности.
        if not gaps:
            now = utc_now_iso()
            marker_decision = Decision(
                decision_id=str(uuid.uuid4()),
                project_id=project_id,
                title=f"[__phase_marker__] Фаза «{phase_id}» — пробелов не выявлено",
                description=(
                    f"Phase gap analysis по фазе «{phase_id}» отработала и не "
                    "нашла критичных пробелов. Это служебная запись для "
                    "идемпотентности (в UI скрыта)."
                ),
                chosen_option_id="ok",
                alternatives=(
                    DecisionAlternative(
                        option_id="ok",
                        label="OK — без пробелов",
                        description="Анализ завершён.",
                        confidence=1.0,
                    ),
                    DecisionAlternative(
                        option_id="reopen",
                        label="Переоткрыть",
                        description="Пользователь может вручную пометить как требующее ревизии.",
                        confidence=0.5,
                    ),
                ),
                rationale="Служебная отметка идемпотентности.",
                level="detail",
                level_rationale="Служебная запись, не проектное решение.",
                confidence=1.0,
                status="accepted_default",
                user_action="not_shown",
                source="phase_gap_analysis",
                source_task_id=marker,
                created_at=now,
                updated_at=now,
            )
            self._runtime.upsert_decision(workspace, marker_decision)
            return PhaseGapResult(
                phase_id=phase_id,
                gaps=(),
                already_done=False,
                token_usage=usage_dict,
            )

        for d in gaps:
            # Помечаем source_task_id маркером — чтобы _is_phase_already_analyzed
            # вернул True при следующем вызове и мы не пере-анализировали.
            # Это безопасно: source_task_id у gap-решений не несёт смысла
            # «привязка к task'у», только аудит-метка.
            marked = replace(d, source_task_id=marker)
            try:
                self._runtime.upsert_decision(workspace, marked)
                saved.append(self._runtime.get_decision(workspace, marked.decision_id))
            except Exception:  # noqa: BLE001
                continue

        return PhaseGapResult(
            phase_id=phase_id,
            gaps=tuple(saved),
            already_done=False,
            token_usage=usage_dict,
        )

    def _resolve_llm(self):
        try:
            return self._llm.resolve_for_purpose(
                PURPOSE_DECISION_PLANNING,
                complexity=_PHASE_GAP_COMPLEXITY,
            )
        except ConflictError:
            return self._llm.resolve_for_purpose(
                "execution",
                complexity="standard",
            )

    def _build_user_prompt(
        self,
        *,
        phase_id: str,
        registry_titles: tuple[str, ...],
    ) -> str:
        phase_desc = _PHASE_DESCRIPTIONS.get(phase_id, phase_id)
        expected = _PHASE_EXPECTED_CATEGORIES.get(phase_id, ())
        expected_bullets = "\n".join(f"- `{c}`" for c in expected)

        recent_titles = registry_titles[-80:]
        titles_block = (
            "\n".join(f"- {t}" for t in recent_titles)
            if recent_titles
            else "_(реестр пуст)_"
        )

        return (
            f"### Фаза\n"
            f"**ID:** {phase_id}\n"
            f"**Описание:** {phase_desc}\n\n"
            f"### Категории, которые должны быть закрыты на этой фазе\n"
            f"{expected_bullets}\n\n"
            f"### Текущий реестр решений ({len(registry_titles)} записей; "
            f"показаны последние {len(recent_titles)})\n"
            f"{titles_block}\n\n"
            f"### Запрос\n"
            f"Найди **критичные пробелы** — проектные развилки, которые на "
            f"этой фазе должны быть закрыты, но в реестре отсутствуют. От 0 "
            f"до 5. Лучше 0, чем шум."
        )

    def _build_gap_decisions(
        self,
        *,
        response: dict[str, Any],
        project_id: str,
        phase_id: str,
    ) -> tuple[Decision, ...]:
        raw_gaps = response.get("gaps") or []
        if not isinstance(raw_gaps, list):
            return ()
        now = utc_now_iso()
        out: list[Decision] = []
        for raw in raw_gaps:
            if not isinstance(raw, dict):
                continue
            try:
                d = self._build_single_gap(raw=raw, project_id=project_id, now=now)
            except (KeyError, TypeError, ValueError):
                continue
            out.append(d)
        return tuple(out)

    def _build_single_gap(
        self,
        *,
        raw: dict[str, Any],
        project_id: str,
        now: str,
    ) -> Decision:
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
        if len(alternatives) < 2:
            raise ValueError("gap decision: need >= 2 alternatives")
        if any(alt.confidence is None for alt in alternatives):
            raise ValueError("gap decision: confidence required")

        category = str(raw.get("category") or "").strip()
        if category not in DECISION_CATEGORIES:
            raise ValueError(f"gap decision: bad category {category!r}")

        proposed = str(raw.get("proposed_option_id") or "")
        if proposed not in {alt.option_id for alt in alternatives}:
            proposed = alternatives[0].option_id

        level = raw.get("level")
        if level not in ("business", "architecture", "detail"):
            level = "architecture"

        description = str(raw.get("description") or "")
        if not description.startswith("[") or "]" not in description[:30]:
            description = f"[{category}] {description}"

        return Decision(
            decision_id=str(uuid.uuid4()),
            project_id=project_id,
            title=str(raw.get("title") or "Untitled gap"),
            description=description,
            chosen_option_id=proposed,
            alternatives=alternatives,
            rationale=str(raw.get("rationale") or ""),
            level=level,  # type: ignore[arg-type]
            level_rationale="Выявлено phase gap analysis: пробел по чек-листу фазы.",
            confidence=float(raw.get("confidence") or 0.5),
            status="proposed",
            user_action="pending",
            source="phase_gap_analysis",
            source_task_id=None,  # выставится у вызывающего на marker
            created_at=now,
            updated_at=now,
        )
