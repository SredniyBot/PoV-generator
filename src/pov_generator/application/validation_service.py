from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..common.errors import ValidationError
from ..common.serialization import utc_now_iso
from ..domain.decisions import DecisionAlternative, DecisionInput
from ..domain.registry import MethodologyPackSpec, RegistrySnapshot
from ..domain.validation import EscalationTicket, ValidationFinding, ValidationRun
from ..infrastructure.sqlite_runtime import SqliteRuntime
from .artifact_contracts import artifact_schema, validate_json_schema
from .checkpoint_service import CheckpointService
from .methodology_rules import evaluate_methodology_rules

if TYPE_CHECKING:
    from .execution_service import ExecutionBundle


# v3.1: visibility ClarificationCandidate → DecisionLevel.
# Legacy ось `visibility` (principal/architectural/technical) маппится 1:1
# на новые уровни вовлечения. См. docs/decision_level_criteria.md.
_VISIBILITY_TO_LEVEL: dict[str, str] = {
    "principal": "business",
    "architectural": "architecture",
    "technical": "detail",
}

# v3.1: source_type ClarificationCandidate → DecisionSource. Legacy типы
# `validation` и `quality_gate` оба означают реактивную регистрацию после
# того как валидация артефакта (или его gate) выявила пробел.
# `methodology_pack` приходит от правил методологии, которые срабатывают
# по ходу исполнения, — для реестра это `emergent`.
_SOURCE_TYPE_TO_DECISION_SOURCE: dict[str, str] = {
    "validation": "reactive_validation",
    "quality_gate": "reactive_validation",
    "methodology_pack": "emergent",
    "planning": "pre_flight",
    "task": "reactive_validation",
    "domain_pack": "emergent",
}


def _resolve_confidence(
    overall_confidence: float | None,
    payload: dict[str, Any],
) -> float | None:
    """Унифицированный доступ к уверенности артефакта.

    Уверенность — это метаданные артефакта
    (``ArtifactMetadata.overall_confidence``). При создании артефакта
    execution_service автоматически вытягивает её из ``payload['confidence']``
    и кладёт в метаданные. Эта функция инкапсулирует приоритеты:

    1. Если в метаданных есть конкретное число — берём его.
    2. Иначе — fallback на ``payload['confidence']`` для backward-compat
       (legacy-фикстуры, артефакты с прошлых запусков, тестовые мок-payload).
    3. Если ни там, ни там нет — ``None`` (валидация просто не сработает
       по правилу confidence, что эквивалентно «уверенности не задана»).
    """
    if isinstance(overall_confidence, (int, float)) and not isinstance(overall_confidence, bool):
        return float(overall_confidence)
    raw = payload.get("confidence")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    return None


def _build_decision_input(
    *,
    title: str,
    description: str,
    rationale: str,
    impact: str = "",
    visibility: str = "architectural",
    answer_mode: str = "single",
    confidence: float = 0.2,
    source_type: str = "validation",
    affected_task_ids: tuple[str, ...] = (),
    related_artifact_ids: tuple[str, ...] = (),
    options: tuple[tuple[str, str, str, float | None], ...] = (),
    recommended_option_id: str | None = None,
) -> DecisionInput:
    """Сконструировать DecisionInput из «сырого» вопроса (v3.1).

    Принимает упрощённый набор параметров, аналогичный
    ``ClarificationService.candidate_from_question`` в legacy-коде,
    и собирает payload для ``CheckpointService.register_decision_inputs``.

    options: tuple of (option_id, label, description, confidence) для каждой
    альтернативы. Если пусто — alternatives тоже пустой tuple (case free_text).
    recommended_option_id: если None и alternatives непуст, берётся option_id
    первой альтернативы.
    """
    # v3.5: per-alt confidence обязательная. Если эмиттер передал None —
    # подставляем 0.5 (нейтрально-неопределённо). is_low_confidence
    # учитывает это и засветит индикатор «система не уверена».
    alternatives = tuple(
        DecisionAlternative(
            option_id=opt[0],
            label=opt[1],
            description=opt[2],
            confidence=0.5 if opt[3] is None else opt[3],
        )
        for opt in options
    )
    # v3.3: если у эмиттера нет реальных альтернатив — НЕ изобретаем
    # фейковый «Принять рекомендацию системы». Это выглядело как заглушка.
    # Переключаемся на free_text — пользователь пишет ответ сам или
    # нажимает «Принять как есть» (accept_default) в UI.
    if not alternatives:
        answer_mode = "free_text"
        recommended = ""
    elif recommended_option_id is None:
        recommended = alternatives[0].option_id
    else:
        recommended = recommended_option_id
    # rationale + impact склейка: impact дополняет почему этот ответ важен.
    if impact:
        full_rationale = f"{rationale} {impact}".strip()
    else:
        full_rationale = rationale
    level = _VISIBILITY_TO_LEVEL.get(visibility, "architecture")
    source = _SOURCE_TYPE_TO_DECISION_SOURCE.get(source_type, "reactive_validation")
    source_task_id = affected_task_ids[0] if affected_task_ids else None
    return DecisionInput(
        title=title.strip(),
        description=description or title.strip(),
        alternatives=alternatives,
        recommended_option_id=recommended,
        rationale=full_rationale,
        level=level,  # type: ignore[arg-type]
        answer_mode=answer_mode,  # type: ignore[arg-type]
        confidence=float(confidence),
        source=source,  # type: ignore[arg-type]
        source_task_id=source_task_id,
        affected_artifact_ids=related_artifact_ids,
    )


class ValidationService:
    def __init__(self, runtime: SqliteRuntime, checkpoint_service: CheckpointService | None = None) -> None:
        self._runtime = runtime
        self._checkpoint_service = checkpoint_service or CheckpointService(runtime)

    def validate_execution(
        self,
        workspace: Path,
        snapshot: RegistrySnapshot,
        *,
        task_id: str,
        execution_bundle: ExecutionBundle,
    ) -> ValidationRun:
        manifest = self._runtime.load_manifest(workspace)
        process = self._runtime.load_process_state(workspace)
        task = self._runtime.get_task(workspace, task_id)
        template = snapshot.resolve_template(task.template_ref)
        findings: list[ValidationFinding] = []
        # v3.1: field остался `clarification_candidate_ids` для backward-compat
        # схемы ValidationRun, но реально хранит decision_id свежесозданных
        # Decision-записей (см. CheckpointService.register_decision_inputs).
        clarification_candidate_ids: list[str] = []
        active_domain_refs = tuple(sorted(process.active_domain_pack_records.keys()))

        if execution_bundle.result.status != "succeeded":
            findings.append(
                ValidationFinding(
                    finding_id=str(uuid.uuid4()),
                    finding_type="execution_failure",
                    severity="critical",
                    blocking=True,
                    message=execution_bundle.result.failure_message or "Исполнение задачи завершилось с ошибкой.",
                )
            )
        else:
            for output in execution_bundle.result.outputs:
                # Reasoning и methodology trace валидируются отдельным контрактом методологии,
                # не схемой основного артефакта. На этом этапе пропускаем.
                if getattr(output, "kind", "primary") != "primary":
                    continue
                artifact = self._runtime.load_artifact(workspace, output.artifact_id)
                try:
                    payload = json.loads(self._runtime.load_artifact_content(workspace, artifact.artifact_id))
                    validate_json_schema(payload, artifact_schema(output.artifact_role, active_domain_refs))
                except (json.JSONDecodeError, ValidationError) as exc:
                    findings.append(
                        ValidationFinding(
                            finding_id=str(uuid.uuid4()),
                            finding_type="schema_error",
                            severity="error",
                            blocking=True,
                            message=str(exc),
                            related_artifact_ids=(artifact.artifact_id,),
                        )
                    )
                    continue

                semantic_findings, decision_inputs = self._semantic_analysis(
                        artifact_role=output.artifact_role,
                        payload=payload,
                        template_ref=template.ref.as_string(),
                        active_domain_pack_refs=tuple(sorted(process.active_domain_pack_records.keys())),
                        artifact_id=artifact.artifact_id,
                        project_id=manifest.project_id,
                        task_id=task_id,
                        # Уверенность — это метаданные артефакта, не часть
                        # бизнес-payload. Достаём из metadata (execution_service
                        # уже её туда положил при создании артефакта).
                        overall_confidence=artifact.metadata.overall_confidence,
                    )
                # v3.1: register_decision_inputs создаёт Decision-записи и
                # отдельную CheckpointSession (mode="expert" — forcibly surface)
                # для каждой группы по task_id. Возвращает tuple[Decision, ...].
                created_decisions = self._checkpoint_service.register_decision_inputs(
                    workspace,
                    project_id=manifest.project_id,
                    decision_inputs=tuple(decision_inputs),
                )
                clarification_candidate_ids.extend(d.decision_id for d in created_decisions)
                # `needs_user_input`-finding для review_report со статусом
                # "needs_user_input" остаётся: он сообщает gating-уровень.
                # Если post-validation не породил ни одного решения для
                # пользователя — снимаем raw needs_user_input как избыточный.
                if not bool(created_decisions):
                    semantic_findings = tuple(
                        f for f in semantic_findings if f.finding_type != "needs_user_input"
                    )
                findings.extend(semantic_findings)

                if output.artifact_role == "review_report":
                    if payload.get("overall_status") != "passed":
                        findings.append(
                            ValidationFinding(
                                finding_id=str(uuid.uuid4()),
                                finding_type="quality_risk",
                                severity="error",
                                blocking=True,
                                message="Ревью не прошло: документ требует доработки.",
                                related_artifact_ids=(artifact.artifact_id,),
                            )
                        )
                    else:
                        gate_inputs = self._maybe_emit_gate_candidates(
                            workspace=workspace,
                            snapshot=snapshot,
                            project_id=manifest.project_id,
                            task_id=task_id,
                            artifact_role=output.artifact_role,
                            artifact_id=artifact.artifact_id,
                        )
                        if gate_inputs:
                            gate_decisions = self._checkpoint_service.register_decision_inputs(
                                workspace,
                                project_id=manifest.project_id,
                                decision_inputs=tuple(gate_inputs),
                            )
                            clarification_candidate_ids.extend(d.decision_id for d in gate_decisions)

                if (
                    output.artifact_role == "requirements_spec"
                    and template.ref.as_string() != "common.requirements_spec_generation@2.0.0"
                    and self._has_pack(tuple(sorted(process.active_domain_pack_records.keys())), "frontend.web_workspace", "frontend.web_app_requirements")
                    and not payload.get("frontend_requirements")
                ):
                    findings.append(
                        ValidationFinding(
                            finding_id=str(uuid.uuid4()),
                            finding_type="domain_pack_expectation",
                            severity="error",
                            blocking=True,
                            message="Для активного пакета интерфейса в ТЗ отсутствует раздел требований к интерфейсу.",
                            related_artifact_ids=(artifact.artifact_id,),
                        )
                    )

        status = "passed" if not any(item.blocking for item in findings) else "failed"
        # v3.1: methodology emits DecisionInput directly через
        # ExecutionResult.methodology_decisions. Регистрация через
        # CheckpointService — единый путь для всех источников.
        if (
            execution_bundle.result.status == "succeeded"
            and execution_bundle.result.methodology_decisions
            and self._checkpoint_service is not None
        ):
            try:
                meth_decisions = self._checkpoint_service.register_decision_inputs(
                    workspace,
                    project_id=manifest.project_id,
                    decision_inputs=execution_bundle.result.methodology_decisions,
                )
                clarification_candidate_ids.extend(d.decision_id for d in meth_decisions)
            except Exception:
                # Fail-safe: ошибка регистрации кандидата не должна валить
                # валидацию основного артефакта.
                pass

        validation_run = ValidationRun(
            validation_run_id=str(uuid.uuid4()),
            project_id=manifest.project_id,
            task_id=task_id,
            execution_run_id=execution_bundle.result.execution_run_id,
            status=status,
            findings=tuple(findings),
            clarification_candidate_ids=tuple(clarification_candidate_ids),
            created_at=utc_now_iso(),
        )
        self._runtime.record_validation_run(workspace, validation_run)

        if status != "passed" and any(finding.finding_type != "needs_user_input" for finding in findings):
            ticket = EscalationTicket(
                escalation_ticket_id=str(uuid.uuid4()),
                project_id=manifest.project_id,
                task_id=task_id,
                reason_code="validation_failed",
                severity="error",
                blocking=True,
                summary=f"Валидация задачи '{task.task_key}' завершилась с ошибками.",
                details={"findings": [finding.message for finding in findings]},
                created_at=utc_now_iso(),
            )
            self._runtime.record_escalation_ticket(workspace, ticket)

        return validation_run

    def _semantic_analysis(
        self,
        *,
        artifact_role: str,
        payload: dict[str, Any],
        template_ref: str,
        active_domain_pack_refs: tuple[str, ...],
        artifact_id: str,
        project_id: str,
        task_id: str,
        overall_confidence: float | None = None,
    ):
        findings: list[ValidationFinding] = []
        decision_inputs: list[DecisionInput] = []
        # Уверенность приоритетно берём из metadata (overall_confidence).
        # Fallback на payload['confidence'] — для backward-compat с
        # уже сохранёнными ранее артефактами и legacy-фикстурами.
        confidence = _resolve_confidence(overall_confidence, payload)
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and confidence < 0.45:
            findings.append(
                ValidationFinding(
                    finding_id=str(uuid.uuid4()),
                    finding_type="low_confidence",
                    severity="error",
                    blocking=True,
                    message=f"Артефакт '{artifact_role}' имеет слишком низкую уверенность ({confidence:.2f}).",
                    related_artifact_ids=(artifact_id,),
                )
            )

        if artifact_role == "requirements_spec" and template_ref == "common.requirements_spec_generation@2.0.0":
            findings.extend(self._validate_enterprise_spec(payload, active_domain_pack_refs, artifact_id))

        if artifact_role == "review_report" and template_ref == "common.requirements_spec_review@2.0.0":
            findings.extend(
                self._validate_review_report(payload, artifact_id, overall_confidence=overall_confidence)
            )

        return findings, decision_inputs

    def _validate_enterprise_spec(
        self,
        payload: dict[str, Any],
        active_domain_pack_refs: tuple[str, ...],
        artifact_id: str,
    ) -> list[ValidationFinding]:
        findings: list[ValidationFinding] = []
        required_sections = {
            "executive_summary": "В ТЗ отсутствует краткое резюме.",
            "business_context": "В ТЗ отсутствует бизнес-контекст.",
            "target_outcomes": "В ТЗ не зафиксированы целевые результаты.",
            "scope_in": "В ТЗ не зафиксировано, что входит в текущий этап.",
            "scope_out": "В ТЗ не зафиксировано, что не входит в текущий этап.",
            "stakeholders": "В ТЗ не описаны стейкхолдеры.",
            "operating_model": "В ТЗ не описана операционная модель.",
            "data_requirements": "Не описаны требования к данным.",
            "delivery_artifacts": "В ТЗ не описаны результаты текущего этапа.",
            "phased_plan": "В ТЗ не зафиксирован план этапов.",
        }
        for field_name, message in required_sections.items():
            value = payload.get(field_name)
            if value in (None, "") or (isinstance(value, list) and not value):
                findings.append(
                    ValidationFinding(
                        finding_id=str(uuid.uuid4()),
                        finding_type="spec_completeness",
                        severity="error",
                        blocking=True,
                        message=message,
                        related_artifact_ids=(artifact_id,),
                    )
                )

        if self._has_pack(active_domain_pack_refs, "frontend.web_workspace", "frontend.web_app_requirements"):
            frontend = payload.get("frontend_requirements")
            if not isinstance(frontend, dict) or not frontend.get("screens"):
                findings.append(
                    ValidationFinding(
                        finding_id=str(uuid.uuid4()),
                        finding_type="domain_pack_expectation",
                        severity="error",
                        blocking=True,
                        message="Для активного пакета интерфейса в ТЗ отсутствует или пуст раздел требований к интерфейсу.",
                        related_artifact_ids=(artifact_id,),
                    )
                )

        if self._has_pack(active_domain_pack_refs, "ml.predictive_analytics", "ml.predictive_analytics_pov_requirements"):
            ml_requirements = payload.get("ml_requirements")
            if not isinstance(ml_requirements, dict) or not ml_requirements.get("prediction_target"):
                findings.append(
                    ValidationFinding(
                        finding_id=str(uuid.uuid4()),
                        finding_type="domain_pack_expectation",
                        severity="error",
                        blocking=True,
                        message="Для активного пакета аналитики и ML в ТЗ отсутствует или неполон раздел требований к модели и данным.",
                        related_artifact_ids=(artifact_id,),
                    )
                )

        if self._has_pack(active_domain_pack_refs, "security.enterprise_compliance", "security.enterprise_compliance_requirements"):
            security_detail = payload.get("security_constraints_detail")
            if not isinstance(security_detail, dict) or not security_detail.get("mandatory_controls"):
                findings.append(
                    ValidationFinding(
                        finding_id=str(uuid.uuid4()),
                        finding_type="domain_pack_expectation",
                        severity="error",
                        blocking=True,
                        message="Для активного пакета безопасности в ТЗ отсутствует или неполон раздел ограничений ИБ и приватности.",
                        related_artifact_ids=(artifact_id,),
                    )
                )

        if self._has_pack(active_domain_pack_refs, "integration.enterprise_integration", "integration.enterprise_delivery_requirements"):
            integration_model = payload.get("integration_model")
            if not isinstance(integration_model, dict) or not integration_model.get("delivery_pattern"):
                findings.append(
                    ValidationFinding(
                        finding_id=str(uuid.uuid4()),
                        finding_type="domain_pack_expectation",
                        severity="error",
                        blocking=True,
                        message="Для активного пакета интеграций в ТЗ отсутствует или неполон раздел интеграционной модели.",
                        related_artifact_ids=(artifact_id,),
                    )
                )

        return findings

    def _has_pack(self, active_domain_pack_refs: tuple[str, ...], *pack_prefixes: str) -> bool:
        return any(
            any(pack_ref.startswith(f"{pack_prefix}@") for pack_prefix in pack_prefixes)
            for pack_ref in active_domain_pack_refs
        )

    def _validate_review_report(
        self,
        payload: dict[str, Any],
        artifact_id: str,
        *,
        overall_confidence: float | None = None,
    ) -> list[ValidationFinding]:
        findings: list[ValidationFinding] = []
        confidence = _resolve_confidence(overall_confidence, payload)
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and confidence < 0.55:
            findings.append(
                ValidationFinding(
                    finding_id=str(uuid.uuid4()),
                    finding_type="review_confidence",
                    severity="error",
                    blocking=True,
                    message=f"Ревью имеет недостаточную уверенность ({confidence:.2f}) и требует участия пользователя.",
                    related_artifact_ids=(artifact_id,),
                )
            )

        if payload.get("overall_status") == "needs_user_input":
            findings.append(
                ValidationFinding(
                    finding_id=str(uuid.uuid4()),
                    finding_type="needs_user_input",
                    severity="critical",
                    blocking=True,
                    message="Ревью показывает, что без дополнительного ввода пользователя продолжать нельзя.",
                    related_artifact_ids=(artifact_id,),
                )
            )
        return findings

    def _evaluate_methodology_rules(
        self,
        *,
        methodology: MethodologyPackSpec,
        complexity: str | None,
        reasoning: dict,
        project_id: str,
        task_id: str,
    ) -> list[DecisionInput]:
        # Тонкий делегат к pure-функции в `methodology_rules`. Сохранён для
        # обратной совместимости с тестами.
        evaluation = evaluate_methodology_rules(
            methodology=methodology,
            complexity=complexity,
            reasoning=reasoning,
            project_id=project_id,
            task_id=task_id,
        )
        return list(evaluation.decision_inputs)

    def _maybe_emit_gate_candidates(
        self,
        *,
        workspace: Path,
        snapshot: RegistrySnapshot,
        project_id: str,
        task_id: str,
        artifact_role: str,
        artifact_id: str,
    ) -> list[DecisionInput]:
        manifest = self._runtime.load_manifest(workspace)
        objective = snapshot.resolve_objective(manifest.objective_ref)
        inputs: list[DecisionInput] = []
        # v3.1: дедуп сигналов sign-off — раньше по (source_type="quality_gate",
        # source_id=gate_ref); теперь у Decision нет source_id, ищем по
        # подстроке gate.title внутри Decision.title (формат title задан
        # ниже как «Согласовать результат gate '<title>'?»). Менее точно,
        # но достаточно, чтобы не дублировать sign-off-запросы.
        existing_decisions = self._runtime.list_decisions(
            workspace,
            project_id=manifest.project_id,
            source="reactive_validation",
        )
        for gate_ref in objective.done_gate_refs:
            gate = snapshot.resolve_quality_gate(gate_ref)
            if gate.check_type != "human_approval":
                continue
            required_roles = set(gate.required_artifact_roles)
            if required_roles and artifact_role not in required_roles:
                continue
            already = any(
                gate.title in decision.title
                for decision in existing_decisions
            )
            if already:
                continue
            decision_modes = gate.decision_modes or ("approved", "approved_with_comments", "rejected")
            options_payload = tuple(
                (mode, mode, "", None) for mode in decision_modes
            )
            inputs.append(
                _build_decision_input(
                    title=f"Согласовать результат gate '{gate.title}'?",
                    description=f"Gate {gate.ref.as_string()} требует решения роли '{gate.approver_role or 'approver'}'.",
                    rationale="Gate настроен на human_approval — пока не получено решение, цель не считается завершённой.",
                    impact="Без согласования цель проекта не закрывается.",
                    # Этап 3.1: внешнее согласование (sign-off) — это
                    # principal-уровень: всплывает в любом engagement-режиме.
                    visibility="principal",
                    answer_mode="single",
                    confidence=0.0,
                    source_type="quality_gate",
                    affected_task_ids=(task_id,),
                    related_artifact_ids=(artifact_id,),
                    options=options_payload,
                )
            )
        return inputs
