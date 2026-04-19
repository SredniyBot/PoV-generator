from __future__ import annotations

from pathlib import Path
import json
import uuid
from typing import TYPE_CHECKING

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
                    and "frontend.web_app_requirements@1.0.0" in state.enabled_domain_packs
                    and not payload.get("frontend_requirements")
                ):
                    findings.append(
                        ValidationFinding(
                            finding_id=str(uuid.uuid4()),
                            finding_type="domain_pack_expectation",
                            severity="error",
                            blocking=True,
                            message="Для активного frontend domain pack в ТЗ отсутствует раздел frontend_requirements.",
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
