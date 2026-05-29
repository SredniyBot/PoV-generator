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

from dataclasses import dataclass, field
from typing import Any

from ..domain.decisions import DecisionAlternative, DecisionInput, DecisionLevel
from ..domain.registry import MethodologyPackSpec
from .methodology_rule_eval import evaluate_rule

_VALID_VISIBILITY = {"principal", "architectural", "technical"}

# v3.1: visibility (legacy emit YAML schema) → level (new domain)
_VISIBILITY_TO_LEVEL: dict[str, DecisionLevel] = {
    "principal": "business",
    "architectural": "architecture",
    "technical": "detail",
}


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


def _llm_options_to_decision_alternatives(
    raw_options: list[Any],
) -> tuple[DecisionAlternative, ...]:
    """Конвертирует объекты `{label, rationale, tradeoffs, confidence}` из
    reasoning-стадии в `DecisionAlternative`, которые увидит пользователь.

    rationale + tradeoffs склеиваются в description, чтобы менеджер
    увидел не только название варианта, но и его обоснование и
    компромиссы.
    """
    out: list[DecisionAlternative] = []
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
        # v3.5: confidence per-alt — обязательное поле. Если LLM не дала
        # явное значение, fallback на 0.5 (нейтрально-неопределённо).
        # Это ровно та полуточка, на которой Decision.is_low_confidence
        # засветит индикатор «система не уверена», — что и есть честный
        # сигнал, когда сама LLM воздержалась от оценки.
        confidence_raw = item.get("confidence")
        if isinstance(confidence_raw, (int, float)) and not isinstance(confidence_raw, bool):
            confidence: float = max(0.0, min(1.0, float(confidence_raw)))
        else:
            confidence = 0.5
        out.append(
            DecisionAlternative(
                option_id=f"opt_{idx}",
                label=label,
                description=description,
                confidence=confidence,
            )
        )
    return tuple(out)


@dataclass(frozen=True)
class RuleOutcome:
    """Один результат проверки правила одной стадии методологии.

    v3.1: убран candidate_id (legacy ClarificationCandidate id).
    Если нужна привязка решения к правилу для аудита — она доступна
    через Decision.source = "emergent" + наличие в выходе той же задачи.
    """

    stage_id: str
    rule_id: str
    fired: bool


@dataclass(frozen=True)
class MethodologyEvaluation:
    """Полный результат прогона правил методологии по reasoning.

    v3.1: вместо ClarificationCandidate выдаются готовые DecisionInput —
    эмиттер сразу формирует payload в новой архитектуре.
    """

    decision_inputs: tuple[DecisionInput, ...] = field(default_factory=tuple)
    rule_outcomes: tuple[RuleOutcome, ...] = field(default_factory=tuple)
    stage_outputs: dict[str, dict[str, Any]] = field(default_factory=dict)


def evaluate_methodology_rules(
    *,
    methodology: MethodologyPackSpec,
    complexity: str | None,
    reasoning: dict[str, Any],
    project_id: str,
    task_id: str,
    methodology_mode: str = "full",
) -> MethodologyEvaluation:
    """Прогоняет правила активной методологии по reasoning_artifact.

    `reasoning` принимается в двух форматах:
    - `{"stages": [{"stage_id": ..., "outputs": {...}}, ...]}` (наш runtime
      формат);
    - `{"<stage_id>": {<fields>}, ...}` (raw, как в тестах).

    `methodology_mode` (Track 5): per-task фильтр стадий. См.
    `MethodologyPackSpec.stages_for`.
    """

    active_stages = methodology.stages_for(complexity, methodology_mode)
    stage_outputs = _stage_outputs_from_reasoning(reasoning)

    decision_inputs: list[DecisionInput] = []
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
            # v3.1: severity и blocking_scope больше не используются — режим
            # участия пользователя (см. ProcessState.clarification_mode)
            # решает, surfacing или silent_accept для решения этого уровня.

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
            decision_alts = _llm_options_to_decision_alternatives(llm_alternatives)

            # Строим текст вопроса: если есть конкретные альтернативы —
            # упоминаем их в вопросе, чтобы пользователь сразу видел, о
            # чём идёт речь, без необходимости открывать описание.
            base_need = str(emit.get("need") or f"Сработало правило {rule.identifier}.").strip()
            if decision_alts:
                titles = " · ".join(opt.label[:60] for opt in decision_alts[:3])
                question_text = f"{base_need} Сравниваются: {titles}"
            else:
                question_text = base_need

            # Описание: если есть реальные альтернативы, строим внятный
            # абзац-introduction из stage-контекста.
            stage_title = (stage.title or stage.identifier).strip()
            description_text = base_need
            if decision_alts:
                description_text = (
                    f"На стадии «{stage_title}» при разборе задачи LLM нашёл "
                    f"{len(decision_alts)} сопоставимых по уверенности "
                    "альтернативы. Выберите ту, которую следует положить в основу "
                    "финального артефакта; альтернативные варианты записаны для "
                    "истории решения."
                )

            # default_assumption (v2.2) → склейка в rationale (v3.1 не имеет
            # отдельного поля; пользователь видит дефолтный выбор + обоснование)
            default_assumption = _safe_assumption_for_rule(rule.identifier, outputs, stage_outputs)
            rationale_parts: list[str] = [
                f"Сработало правило {rule.identifier} стадии {stage.identifier}.",
            ]
            if default_assumption:
                rationale_parts.append(f"Безопасное допущение по умолчанию: {default_assumption}")
            rationale_text = " ".join(rationale_parts)

            # v3.4: каждое решение ДОЛЖНО иметь >=2 реальных альтернатив.
            # Случаи:
            # - LLM выдала альтернативы из reasoning + есть default_assumption →
            #   объединяем: default_assumption становится отдельной альтернативой
            #   «безопасное допущение», LLM-варианты — остальные. Гарантирован 2+.
            # - LLM выдала >=2 альтернатив → используем их (default_assumption,
            #   если есть, добавляем как ещё один безопасный fallback).
            # - LLM выдала 1 → дополняем default_assumption (если есть).
            # - LLM ничего не выдала и default_assumption нет → НЕ создаём
            #   decision вообще (правило просто факт, без выбора).
            if not decision_alts and not default_assumption:
                # Truly nothing to decide — skip emit, log rule outcome only
                outcomes.append(
                    RuleOutcome(
                        stage_id=stage.identifier,
                        rule_id=rule.identifier,
                        fired=True,
                    )
                )
                continue
            # Если есть default_assumption — добавим его как «безопасное
            # допущение» альтернативу. Гарантирует наличие 2+ опций даже
            # когда LLM дал только один вариант.
            if default_assumption:
                default_label = default_assumption.strip()
                if len(default_label) > 80:
                    default_label = default_label[:77] + "…"
                default_alt = DecisionAlternative(
                    option_id="opt_safe_default",
                    label=default_label,
                    description=default_assumption,
                    confidence=0.6,
                )
                # Положим в начало списка — это рекомендуемый дефолт
                decision_alts = (default_alt, *decision_alts)

            # Финальная проверка: должно быть >=1 альтернатива
            # (формально 2+ норма; если только 1 — оставляем как «выберите
            # или дайте свой ответ через escape hatch в UI»).
            answer_mode = "single"
            # Рекомендация = вариант с максимальной LLM-confidence
            best_idx = 0
            best_conf = -1.0
            for i, opt in enumerate(decision_alts):
                c = opt.confidence if opt.confidence is not None else 0.0
                if c > best_conf:
                    best_conf = c
                    best_idx = i
            recommended_id = decision_alts[best_idx].option_id

            # Visibility → Level mapping (v3.1)
            level = _VISIBILITY_TO_LEVEL.get(visibility, "detail")
            # severity / blocking_scope больше не передаются — v3.1 mode
            # определяет surface/auto. Игнорируем оба.

            decision_inputs.append(
                DecisionInput(
                    title=question_text,
                    description=description_text,
                    alternatives=decision_alts,
                    recommended_option_id=recommended_id,
                    rationale=rationale_text,
                    level=level,
                    answer_mode=answer_mode,  # type: ignore[arg-type]
                    confidence=0.4,
                    source="emergent",
                    source_task_id=task_id,
                    affected_artifact_ids=(),
                )
            )
            outcomes.append(
                RuleOutcome(
                    stage_id=stage.identifier,
                    rule_id=rule.identifier,
                    fired=True,
                )
            )

    return MethodologyEvaluation(
        decision_inputs=tuple(decision_inputs),
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


