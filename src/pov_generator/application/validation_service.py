from __future__ import annotations

from pathlib import Path
import json
import uuid
from typing import TYPE_CHECKING, Any

from ..common.errors import ValidationError
from ..common.serialization import utc_now_iso
from ..domain.registry import RegistrySnapshot
from ..domain.validation import EscalationTicket, ValidationFinding, ValidationRun
from ..infrastructure.sqlite_runtime import SqliteRuntime
from .artifact_contracts import artifact_schema, validate_json_schema

if TYPE_CHECKING:
    from .execution_service import ExecutionBundle


class ValidationService:
    def __init__(self, runtime: SqliteRuntime) -> None:
        self._runtime = runtime

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
        template = snapshot.resolve_template(f"{task.template_id}@{task.template_version}")
        findings: list[ValidationFinding] = []

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
                artifact = self._runtime.load_artifact(workspace, output.artifact_id)
                try:
                    payload = json.loads(self._runtime.load_artifact_content(workspace, artifact.artifact_id))
                    validate_json_schema(payload, artifact_schema(output.artifact_role, tuple(sorted(state.enabled_domain_packs.keys()))))
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

                findings.extend(
                    self._semantic_findings(
                        artifact_role=output.artifact_role,
                        payload=payload,
                        template_ref=template.ref.as_string(),
                        enabled_domain_packs=tuple(sorted(state.enabled_domain_packs.keys())),
                        artifact_id=artifact.artifact_id,
                    )
                )

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

                if (
                    output.artifact_role == "requirements_spec"
                    and template.ref.as_string() != "common.requirements_spec_generation@2.0.0"
                    and "frontend.web_app_requirements@1.0.0" in state.enabled_domain_packs
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
        validation_run = ValidationRun(
            validation_run_id=str(uuid.uuid4()),
            project_id=manifest.project_id,
            task_id=task_id,
            execution_run_id=execution_bundle.result.execution_run_id,
            status=status,
            findings=tuple(findings),
            created_at=utc_now_iso(),
        )
        self._runtime.record_validation_run(workspace, validation_run)

        if status != "passed":
            ticket = EscalationTicket(
                escalation_ticket_id=str(uuid.uuid4()),
                project_id=manifest.project_id,
                task_id=task_id,
                reason_code="validation_failed",
                severity="error",
                blocking=True,
                summary=f"Валидация задачи '{task.recipe_step_id}' завершилась с ошибками.",
                details={"findings": [finding.message for finding in findings]},
                created_at=utc_now_iso(),
            )
            self._runtime.record_escalation_ticket(workspace, ticket)

        return validation_run

    def _semantic_findings(
        self,
        *,
        artifact_role: str,
        payload: dict[str, Any],
        template_ref: str,
        enabled_domain_packs: tuple[str, ...],
        artifact_id: str,
    ) -> list[ValidationFinding]:
        findings: list[ValidationFinding] = []
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

        if artifact_role == "requirements_spec" and template_ref == "common.requirements_spec_generation@2.0.0":
            findings.extend(self._validate_enterprise_spec(payload, enabled_domain_packs, artifact_id))

        if artifact_role == "review_report" and template_ref == "common.requirements_spec_review@2.0.0":
            findings.extend(self._validate_review_report(payload, artifact_id))

        return findings

    def _validate_enterprise_spec(
        self,
        payload: dict[str, Any],
        enabled_domain_packs: tuple[str, ...],
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

        if any(ref.startswith("frontend.web_app_requirements@") for ref in enabled_domain_packs):
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

        if any(ref.startswith("ml.predictive_analytics_pov_requirements@") for ref in enabled_domain_packs):
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

        if any(ref.startswith("security.enterprise_compliance_requirements@") for ref in enabled_domain_packs):
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

        if any(ref.startswith("integration.enterprise_delivery_requirements@") for ref in enabled_domain_packs):
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
