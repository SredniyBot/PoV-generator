from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AdmissionCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class CandidateEvaluation:
    task_id: str
    task_key: str
    title: str
    template_ref: str
    admissible: bool
    score: int
    checks: tuple[AdmissionCheck, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PlanningDecision:
    decision_id: str
    project_id: str
    objective_ref: str
    mode: str
    outcome: str
    selected_task_id: str | None
    selected_task_key: str | None
    selected_template_ref: str | None
    admitted_task_ids: tuple[str, ...]
    blocked_task_summaries: tuple[dict[str, object], ...]
    ranking_strategy: str
    candidates: tuple[CandidateEvaluation, ...] = field(default_factory=tuple)
    reasons: tuple[str, ...] = field(default_factory=tuple)
    created_at: str = ""
