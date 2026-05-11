from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..common.errors import ValidationError
from ..common.serialization import utc_now_iso
from ..domain.clarifications import ClarificationCandidate, ClarificationOption
from ..domain.registry import MethodologyPackSpec, RegistrySnapshot
from ..domain.validation import EscalationTicket, ValidationFinding, ValidationRun
from ..infrastructure.sqlite_runtime import SqliteRuntime
from .artifact_contracts import artifact_schema, validate_json_schema
from .clarification_service import ClarificationService
from .methodology_rules import evaluate_methodology_rules

if TYPE_CHECKING:
    from .execution_service import ExecutionBundle


_KNOWN_DECISION_OWNER_ROLES: frozenset[str] = frozenset(
    {"business", "client", "methodologist", "architect", "data_owner", "security"}
)


def _normalize_decision_owner_role(approver_role: str | None) -> str:
    """Маппинг `quality_gate.approver_role` (свободный формат, расширяемый
    словарь spec/02) на канонический `DecisionOwnerRole`. Имена осознанно
    совпадают, но gate может объявить, например, `dpo` — нормализуем
    к ближайшей роли (`security`). Неизвестные роли уходят в `client` для
    human_approval gate'ов (внешнее согласование) и `business` иначе."""
    if not approver_role:
        return "client"
    role = approver_role.strip().lower()
    if role in _KNOWN_DECISION_OWNER_ROLES:
        return role
    aliases = {
        "dpo": "security",
        "ciso": "security",
        "owner": "client",
        "stakeholder": "business",
        "bo": "business",
        "po": "business",
    }
    return aliases.get(role, "client")


class ValidationService:
    def __init__(self, runtime: SqliteRuntime, clarification_service: ClarificationService | None = None) -> None:
        self._runtime = runtime
        self._clarification_service = clarification_service or ClarificationService(runtime)

    def validate_execution(
        self,
        workspace: Path,
        snapshot: RegistrySnapshot,
        *,
        task_id: str,
        execution_bundle: ExecutionBundle,
    ) -> ValidationRun:
        manifest = self._runtime.load_manifest(workspace)
        state = self._runtime.load_problem_state(workspace)
        task = self._runtime.get_task(workspace, task_id)
        template = snapshot.resolve_template(task.template_ref)
        findings: list[ValidationFinding] = []
        clarification_candidate_ids: list[str] = []

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
                    validate_json_schema(payload, artifact_schema(output.artifact_role, tuple(sorted(state.active_domain_pack_records.keys()))))
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

                semantic_findings, candidates = self._semantic_analysis(
                        artifact_role=output.artifact_role,
                        payload=payload,
                        template_ref=template.ref.as_string(),
                        active_domain_pack_refs=tuple(sorted(state.active_domain_pack_records.keys())),
                        artifact_id=artifact.artifact_id,
                        project_id=manifest.project_id,
                        task_id=task_id,
                    )
                findings.extend(semantic_findings)
                decisions = self._clarification_service.register_candidates(workspace, tuple(candidates))
                clarification_candidate_ids.extend(decision.candidate_id for decision in decisions)

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
                        gate_candidates = self._maybe_emit_gate_candidates(
                            workspace=workspace,
                            snapshot=snapshot,
                            project_id=manifest.project_id,
                            task_id=task_id,
                            artifact_role=output.artifact_role,
                            artifact_id=artifact.artifact_id,
                        )
                        if gate_candidates:
                            decisions = self._clarification_service.register_candidates(
                                workspace, tuple(gate_candidates)
                            )
                            clarification_candidate_ids.extend(d.candidate_id for d in decisions)

                if (
                    output.artifact_role == "requirements_spec"
                    and template.ref.as_string() != "common.requirements_spec_generation@2.0.0"
                    and self._has_pack(tuple(sorted(state.active_domain_pack_records.keys())), "frontend.web_workspace", "frontend.web_app_requirements")
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
        # Кандидаты от правил методологии теперь поступают из execution_service:
        # rules eval делается там же, где формируется reasoning, и попадает
        # в methodology_trace с реальными `fired`/`candidates_emitted`.
        # Здесь только регистрируем их через ClarificationService.
        if execution_bundle.result.status == "succeeded" and execution_bundle.result.methodology_candidates:
            try:
                decisions = self._clarification_service.register_candidates(
                    workspace, execution_bundle.result.methodology_candidates
                )
                clarification_candidate_ids.extend(decision.candidate_id for decision in decisions)
            except Exception:
                # Fail-safe: ошибка регистрации кандидата не должна валить
                # валидацию основного артефакта. Аналогично прежнему поведению.
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
    ):
        findings: list[ValidationFinding] = []
        candidates = []
        confidence = payload.get("confidence")
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
            if not payload.get("blocking_questions"):
                candidates.append(
                    self._clarification_service.candidate_from_question(
                        project_id=project_id,
                        source_type="validation",
                        source_id=f"{task_id}:{artifact_id}:low_confidence",
                        question="Какой ключевой бизнес-контекст нужно учесть, чтобы повысить уверенность результата?",
                        affected_task_ids=(task_id,),
                        related_artifact_ids=(artifact_id,),
                        severity="high",
                        confidence_without_user=float(confidence),
                        rationale="Результат задачи имеет низкую уверенность, а в контексте недостаточно данных для надежного вывода.",
                        impact="Ответ поможет перезапустить задачу с более точным пониманием требований.",
                    )
                )

        blocking_questions = payload.get("blocking_questions")
        if isinstance(blocking_questions, list) and blocking_questions:
            findings.append(
                ValidationFinding(
                    finding_id=str(uuid.uuid4()),
                    finding_type="needs_user_input",
                    severity="error",
                    blocking=True,
                    message="Для продолжения нужны уточнения пользователя: " + "; ".join(str(item) for item in blocking_questions),
                    related_artifact_ids=(artifact_id,),
                )
            )
            for index, question in enumerate(blocking_questions, start=1):
                if str(question).strip():
                    candidates.append(
                        self._clarification_service.candidate_from_question(
                            project_id=project_id,
                            source_type="validation",
                            source_id=f"{task_id}:{artifact_id}:question:{index}",
                            question=str(question),
                            affected_task_ids=(task_id,),
                            related_artifact_ids=(artifact_id,),
                            severity="high",
                            confidence_without_user=0.2,
                        )
                    )

        if artifact_role == "requirements_spec" and template_ref == "common.requirements_spec_generation@2.0.0":
            findings.extend(self._validate_enterprise_spec(payload, active_domain_pack_refs, artifact_id))

        if artifact_role == "review_report" and template_ref == "common.requirements_spec_review@2.0.0":
            findings.extend(self._validate_review_report(payload, artifact_id))

        return findings, candidates

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

    def _validate_review_report(self, payload: dict[str, Any], artifact_id: str) -> list[ValidationFinding]:
        findings: list[ValidationFinding] = []
        confidence = payload.get("confidence")
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
    ) -> list[ClarificationCandidate]:
        # Тонкий делегат к pure-функции в `methodology_rules`. Сохранён для
        # обратной совместимости с тестами, которые исторически вызывают этот
        # метод напрямую. В рантайме теперь правила прогоняет execution_service.
        evaluation = evaluate_methodology_rules(
            methodology=methodology,
            complexity=complexity,
            reasoning=reasoning,
            project_id=project_id,
            task_id=task_id,
        )
        return list(evaluation.candidates)

    def _maybe_emit_gate_candidates(
        self,
        *,
        workspace: Path,
        snapshot: RegistrySnapshot,
        project_id: str,
        task_id: str,
        artifact_role: str,
        artifact_id: str,
    ) -> list[ClarificationCandidate]:
        manifest = self._runtime.load_manifest(workspace)
        objective = snapshot.resolve_objective(manifest.objective_ref)
        candidates: list[ClarificationCandidate] = []
        existing_requests = self._runtime.list_clarification_requests(workspace)
        for gate_ref in objective.done_gate_refs:
            gate = snapshot.resolve_quality_gate(gate_ref)
            if gate.check_type != "human_approval":
                continue
            required_roles = set(gate.required_artifact_roles)
            if required_roles and artifact_role not in required_roles:
                continue
            already = any(
                req.source_type == "quality_gate" and req.source_id == gate.ref.as_string()
                for req in existing_requests
            )
            if already:
                continue
            decision_modes = gate.decision_modes or ("approved", "approved_with_comments", "rejected")
            options_typed = tuple(
                ClarificationOption(option_id=mode, label=mode, description="")
                for mode in decision_modes
            )
            candidates.append(
                ClarificationCandidate(
                    candidate_id=str(uuid.uuid4()),
                    project_id=project_id,
                    source_type="quality_gate",
                    source_id=gate.ref.as_string(),
                    need=f"Требуется внешнее согласование: {gate.title}.",
                    question=f"Согласовать результат gate '{gate.title}'?",
                    description=f"Gate {gate.ref.as_string()} требует решения роли '{gate.approver_role or 'approver'}'.",
                    rationale="Gate настроен на human_approval — пока не получено решение, цель не считается завершённой.",
                    impact="Без согласования цель проекта не закрывается.",
                    severity="high",
                    confidence_without_user=0.0,
                    min_participation_mode="balanced",
                    default_assumption=None,
                    recommended_answer=None,
                    answer_mode="single",
                    options=options_typed,
                    affected_task_ids=(task_id,),
                    related_artifact_ids=(artifact_id,),
                    blocking_scope="objective",
                    decision_owner_role=_normalize_decision_owner_role(gate.approver_role),
                    created_at="",
                )
            )
        return candidates
