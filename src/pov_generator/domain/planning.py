from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AdmissionCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class CandidateEvaluation:
    recipe_step_id: str
    step_title: str
    template_ref: str
    step_source_kind: str
    step_source_ref: str
    admissible: bool
    score: int
    duplicate: bool
    checks: tuple[AdmissionCheck, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PlanningDecision:
    project_id: str
    recipe_ref: str
    domain_pack_refs: tuple[str, ...]
    recipe_fragment_refs: tuple[str, ...]
    mode: str
    outcome: str
    selected_step_id: str | None
    selected_template_ref: str | None
    created_task_id: str | None
    candidates: tuple[CandidateEvaluation, ...] = field(default_factory=tuple)
    reasons: tuple[str, ...] = field(default_factory=tuple)
    created_at: str = ""
