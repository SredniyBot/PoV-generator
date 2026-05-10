"""Pure-функция эвалюации правил активной методологии по reasoning_artifact.

Раньше логика жила в `validation_service._evaluate_methodology_rules`. Теперь
вынесена сюда, чтобы:

1) `execution_service` мог запустить правила сразу после генерации reasoning
   и записать **реальные** `rules_evaluated` / `candidates_emitted` в
   `methodology_trace` (вместо placeholder'а с `fired: False`);
2) `validation_service` мог читать готовые candidates из `ExecutionBundle`
   и не дублировать расчёт.

Модуль не зависит от runtime — это чистая функция от данных.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from ..domain.clarifications import ClarificationCandidate
from ..domain.registry import MethodologyPackSpec
from .methodology_rule_eval import evaluate_rule


@dataclass(frozen=True)
class RuleOutcome:
    """Один результат проверки правила одной стадии методологии."""

    stage_id: str
    rule_id: str
    fired: bool
    candidate_id: str | None = None


@dataclass(frozen=True)
class MethodologyEvaluation:
    """Полный результат прогона правил методологии по reasoning."""

    candidates: tuple[ClarificationCandidate, ...] = field(default_factory=tuple)
    rule_outcomes: tuple[RuleOutcome, ...] = field(default_factory=tuple)
    stage_outputs: dict[str, dict[str, Any]] = field(default_factory=dict)


def evaluate_methodology_rules(
    *,
    methodology: MethodologyPackSpec,
    complexity: str | None,
    reasoning: dict[str, Any],
    project_id: str,
    task_id: str,
) -> MethodologyEvaluation:
    """Прогоняет правила активной методологии по reasoning_artifact.

    `reasoning` принимается в двух форматах:
    - `{"stages": [{"stage_id": ..., "outputs": {...}}, ...]}` (наш runtime
      формат);
    - `{"<stage_id>": {<fields>}, ...}` (raw, как в тестах).
    """

    active_stages = methodology.stages_for_complexity(complexity)
    stage_outputs = _stage_outputs_from_reasoning(reasoning)

    candidates: list[ClarificationCandidate] = []
    outcomes: list[RuleOutcome] = []

    for stage in active_stages:
        outputs = stage_outputs.get(stage.identifier, {})
        for rule in stage.rules:
            fired = evaluate_rule(
                rule.if_expression,
                current_stage_outputs=outputs,
                all_stage_outputs=stage_outputs,
            )
            if not fired:
                outcomes.append(
                    RuleOutcome(stage_id=stage.identifier, rule_id=rule.identifier, fired=False)
                )
                continue

            emit = rule.emit_candidate or {}
            severity = str(emit.get("severity", "medium"))
            if severity not in {"low", "medium", "high", "critical"}:
                severity = "medium"
            blocking_scope = str(emit.get("blocking_scope", "task"))
            if blocking_scope not in {"none", "task", "subtree", "objective"}:
                blocking_scope = "task"
            need_text = str(emit.get("need") or f"Сработало правило {rule.identifier}.")
            default_assumption = _safe_assumption_for_rule(rule.identifier, outputs, stage_outputs)

            candidate = ClarificationCandidate(
                candidate_id=str(uuid.uuid4()),
                project_id=project_id,
                source_type="methodology_pack",
                source_id=f"{methodology.ref.as_string()}#{stage.identifier}.{rule.identifier}",
                need=str(emit.get("need", need_text)),
                question=need_text,
                description="",
                rationale=f"Сработало правило {rule.identifier} стадии {stage.identifier}.",
                impact="Без решения этой развилки методология рекомендует не продолжать.",
                severity=severity,  # type: ignore[arg-type]
                confidence_without_user=0.4,
                min_participation_mode="balanced",
                # Безопасное допущение per-rule даёт ClarificationService
                # возможность тихо «принять» решение для менеджера на
                # autopilot/balanced, когда роль = methodologist.
                default_assumption=default_assumption,
                recommended_answer=None,
                answer_mode="free_text",
                options=(),
                affected_task_ids=(task_id,),
                related_artifact_ids=(),
                blocking_scope=blocking_scope,  # type: ignore[arg-type]
                # Правила методологии — это «как мы думаем», не бизнес-вопрос.
                # На autopilot/balanced менеджер их не должен видеть; роль
                # `methodologist` поднимает effective floor до `control`.
                decision_owner_role="methodologist",
                created_at="",
            )
            candidates.append(candidate)
            outcomes.append(
                RuleOutcome(
                    stage_id=stage.identifier,
                    rule_id=rule.identifier,
                    fired=True,
                    candidate_id=candidate.candidate_id,
                )
            )

    return MethodologyEvaluation(
        candidates=tuple(candidates),
        rule_outcomes=tuple(outcomes),
        stage_outputs=stage_outputs,
    )


def _stage_outputs_from_reasoning(reasoning: dict[str, Any]) -> dict[str, dict[str, Any]]:
    stage_outputs: dict[str, dict[str, Any]] = {}
    if isinstance(reasoning.get("stages"), list):
        for stage_block in reasoning["stages"]:
            if not isinstance(stage_block, dict):
                continue
            sid = stage_block.get("stage_id")
            if not sid:
                continue
            outputs = stage_block.get("outputs")
            stage_outputs[sid] = outputs if isinstance(outputs, dict) else {}
        return stage_outputs

    for sid, fields in reasoning.items():
        if isinstance(fields, dict):
            stage_outputs[sid] = fields
    return stage_outputs


def _safe_assumption_for_rule(
    rule_id: str,
    outputs: dict[str, Any],
    all_outputs: dict[str, dict[str, Any]],
) -> str | None:
    """Подбирает безопасное допущение для каждого известного правила.

    Используется ClarificationService на низких уровнях вовлечённости
    менеджера (autopilot/balanced + role=methodologist): вместо того чтобы
    бить тревогу, координатор тихо принимает это допущение. Гарантия:
    допущение должно быть детерминированным и ассоциированным с
    содержимым reasoning, а не пустой формулировкой.
    """
    if rule_id == "empty_goal":
        return "Считать целью задачи её рабочее название (declared_goal не зафиксирована)."
    if rule_id == "ambiguous_choice":
        options = outputs.get("options")
        if not isinstance(options, list):
            fallback = all_outputs.get("option_generation", {})
            options = fallback.get("options") if isinstance(fallback, dict) else None
        if isinstance(options, list):
            best = None
            best_conf = -1.0
            for item in options:
                if isinstance(item, dict) and isinstance(item.get("confidence"), (int, float)):
                    if float(item["confidence"]) > best_conf:
                        best_conf = float(item["confidence"])
                        best = item.get("label")
            if best:
                return f"Выбрать вариант с наивысшей уверенностью: {best}."
        return "Выбрать вариант с наивысшей уверенностью."
    if rule_id == "low_overall_confidence":
        return "Продолжить с текущим выбором, зафиксировав низкую общую уверенность как риск."
    return None


