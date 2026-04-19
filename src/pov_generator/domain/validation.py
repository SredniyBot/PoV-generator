from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


ValidationStatus = Literal["passed", "failed", "escalated"]
FindingSeverity = Literal["info", "warning", "error", "critical"]


@dataclass(frozen=True)
class ValidationFinding:
    finding_id: str
    finding_type: str
    severity: FindingSeverity
    blocking: bool
    message: str
    related_artifact_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ValidationRun:
    validation_run_id: str
    project_id: str
    task_id: str
    execution_run_id: str
    status: ValidationStatus
    findings: tuple[ValidationFinding, ...] = field(default_factory=tuple)
    created_at: str = ""


@dataclass(frozen=True)
class EscalationTicket:
    escalation_ticket_id: str
    project_id: str
    task_id: str | None
    reason_code: str
    severity: Literal["warning", "error", "critical"]
    blocking: bool
    summary: str
    details: dict[str, object]
    created_at: str
