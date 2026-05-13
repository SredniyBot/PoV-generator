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

from ..domain.clarifications import ClarificationCandidate, ClarificationOption
from ..domain.registry import MethodologyPackSpec
from .methodology_rule_eval import evaluate_rule


_VALID_VISIBILITY = {"principal", "architectural", "technical"}


def _resolve_options_from(
    spec: str,
    stage_outputs: dict[str, dict[str, Any]],
) -> list[Any]:
    """Разрешает выражение вида `stage.<stage_id>.<field>[.<sub>...]`
    и возвращает список значений по этому пути.

    Используется методологическим правилом, когда оно хочет показать
    пользователю РЕАЛЬНЫЕ альтернативы из reasoning'а LLM, а не общие
    плейсхолдеры.
    """
    if not isinstance(spec, str) or not spec.startswith("stage."):
        return []
    path = spec[len("stage."):].split(".")
    if not path:
        return []
    stage_id, *rest = path
    cursor: Any = stage_outputs.get(stage_id, {})
    for part in rest:
        # Подставляем `[*]` как «весь массив» — нам нужны items, а не
        # сама обёртка.
        if part.endswith("[*]"):
            field = part[:-3]
            if isinstance(cursor, dict):
                cursor = cursor.get(field)
            if isinstance(cursor, list):
                return cursor
            return []
        if isinstance(cursor, dict):
            cursor = cursor.get(part)
        elif isinstance(cursor, list):
            # обращение по ключу из списка не имеет смысла — упадёт в []
            return []
        else:
            return []
    if isinstance(cursor, list):
        return cursor
    return []


def _llm_options_to_clarification_options(
    raw_options: list[Any],
) -> tuple[ClarificationOption, ...]:
    """Конвертирует объекты `{label, rationale, tradeoffs, confidence}` из
    reasoning-стадии в `ClarificationOption`, которые увидит пользователь.

    rationale + tradeoffs склеиваются в description, чтобы менеджер
    увидел не только название варианта, но и его обоснование и
    компромиссы — это и есть осмысленный выбор, а не «принять / нет».
    """
    out: list[ClarificationOption] = []
    for idx, item in enumerate(raw_options):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("name") or f"Вариант {idx + 1}").strip()
        if not label:
            continue
        rationale = str(item.get("rationale") or "").strip()
        tradeoffs = str(item.get("tradeoffs") or "").strip()
        description_parts: list[str] = []
        if rationale:
            description_parts.append(rationale)
        if tradeoffs:
            description_parts.append(f"Компромисс: {tradeoffs}")
        description = "\n\n".join(description_parts)
        confidence_raw = item.get("confidence")
        confidence: float | None = None
        if isinstance(confidence_raw, (int, float)) and not isinstance(confidence_raw, bool):
            confidence = max(0.0, min(1.0, float(confidence_raw)))
        out.append(
            ClarificationOption(
                option_id=f"opt_{idx}",
                label=label,
                description=description,
                effect_preview="Этот вариант ляжет в основу финального артефакта задачи."
                if idx == 0
                else "Альтернативный вариант — будет переработан reasoning + результирующий артефакт.",
                confidence=confidence,
            )
        )
    return tuple(out)


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
            # Методологические правила — advisory: «система заметила
            # развилку в рассуждении». По умолчанию они НЕ блокируют
            # pipeline — пользователь увидит вопрос в инбоксе, но workflow
            # продолжается с default_assumption.
            #
            # Для настоящих gate-ов методолог-пак ЯВНО ставит
            # blocking_scope в emit_candidate.
            blocking_scope = str(emit.get("blocking_scope", "none"))
            if blocking_scope not in {"none", "task", "subtree", "objective"}:
                blocking_scope = "none"

            # Visibility: по умолчанию `technical` — методологические
            # развилки шумные и неинтересны менеджеру в обычных режимах.
            # На balanced/autopilot они тихо принимаются через
            # default_assumption. Показываются только на control/expert.
            # Per-rule override через `emit.visibility` доступен для
            # методологий, где конкретный gate реально требует решения.
            visibility = str(emit.get("visibility", "technical"))
            if visibility not in _VALID_VISIBILITY:
                visibility = "technical"

            # `options_from` — путь к массиву реальных альтернатив из
            # reasoning'а LLM. Раньше игнорировался: пользователь видел
            # generic-плейсхолдеры «Принять / Использовать допущение»,
            # не имея понятия, какие именно варианты сравниваются.
            # Теперь альтернативы подаются содержательно: label +
            # rationale + tradeoffs + confidence каждого.
            options_from_spec = emit.get("options_from")
            llm_alternatives: list[Any] = []
            if isinstance(options_from_spec, str):
                llm_alternatives = _resolve_options_from(options_from_spec, stage_outputs)
            clarification_options = _llm_options_to_clarification_options(llm_alternatives)

            # Строим текст вопроса: если есть конкретные альтернативы —
            # упоминаем их в вопросе, чтобы пользователь сразу видел, о
            # чём идёт речь, без необходимости открывать описание.
            base_need = str(emit.get("need") or f"Сработало правило {rule.identifier}.").strip()
            if clarification_options:
                titles = " · ".join(opt.label[:60] for opt in clarification_options[:3])
                question_text = f"{base_need} Сравниваются: {titles}"
            else:
                question_text = base_need

            # Описание: если есть реальные альтернативы, строим внятный
            # абзац-introduction из stage-контекста. Заполняем заранее,
            # чтобы `_enrich_candidate` не дёргал LLM для подготовки
            # описания (description.empty → LLM call).
            stage_title = (stage.title or stage.identifier).strip()
            description_text = ""
            if clarification_options:
                description_text = (
                    f"На стадии «{stage_title}» при разборе задачи LLM нашёл "
                    f"{len(clarification_options)} сопоставимых по уверенности "
                    "альтернативы. Выберите ту, которую следует положить в основу "
                    "финального артефакта; альтернативные варианты записаны для "
                    "истории решения."
                )

            default_assumption = _safe_assumption_for_rule(rule.identifier, outputs, stage_outputs)

            answer_mode = "single" if clarification_options else "free_text"
            recommended_id: str | None = None
            if clarification_options:
                # Рекомендация = вариант с максимальной LLM-confidence.
                best_idx = 0
                best_conf = -1.0
                for i, opt in enumerate(clarification_options):
                    c = opt.confidence if opt.confidence is not None else 0.0
                    if c > best_conf:
                        best_conf = c
                        best_idx = i
                recommended_id = clarification_options[best_idx].option_id

            candidate = ClarificationCandidate(
                candidate_id=str(uuid.uuid4()),
                project_id=project_id,
                source_type="methodology_pack",
                source_id=f"{methodology.ref.as_string()}#{stage.identifier}.{rule.identifier}",
                need=base_need,
                question=question_text,
                description=description_text,
                rationale=f"Сработало правило {rule.identifier} стадии {stage.identifier}.",
                impact="Без решения этой развилки методология рекомендует не продолжать.",
                severity=severity,  # type: ignore[arg-type]
                confidence_without_user=0.4,
                visibility=visibility,  # type: ignore[arg-type]
                default_assumption=default_assumption,
                recommended_answer=recommended_id,
                answer_mode=answer_mode,  # type: ignore[arg-type]
                options=clarification_options,
                affected_task_ids=(task_id,),
                related_artifact_ids=(),
                blocking_scope=blocking_scope,  # type: ignore[arg-type]
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


