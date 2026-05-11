from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ClarificationStatus = Literal["open", "answered", "assumed", "deferred", "cancelled"]
ClarificationPriority = Literal["low", "medium", "high", "critical"]
ClarificationSourceType = Literal["task", "validation", "planning", "domain_pack", "methodology_pack", "quality_gate"]
ClarificationAnswerMode = Literal["single", "multiple", "free_text", "confirmation"]
ClarificationBlockingScope = Literal["none", "task", "subtree", "objective"]
ClarificationMode = Literal["autopilot", "balanced", "control", "expert"]
# Декомпозиция вопросов по «домену решения». Ось ортогональна
# `ClarificationMode` (частоте показа) и нужна, чтобы фильтровать вопросы по
# роли менеджера: бизнес-менеджер не должен получать архитектурные/технические
# развилки на autopilot/balanced; CE11 LLM-driver классифицирует кандидата
# при подготовке вопроса. Имена сознательно совпадают с
# `quality_gate.approver_role` из spec/02 для общей терминологии.
DecisionOwnerRole = Literal[
    "business",
    "client",
    "methodologist",
    "architect",
    "data_owner",
    "security",
]


@dataclass(frozen=True)
class ClarificationOption:
    option_id: str
    label: str
    description: str = ""
    effect_preview: str = ""
    confidence: float | None = None


@dataclass(frozen=True)
class ClarificationCandidate:
    candidate_id: str
    project_id: str
    source_type: ClarificationSourceType
    source_id: str
    need: str
    question: str
    description: str
    rationale: str
    impact: str
    severity: ClarificationPriority
    confidence_without_user: float
    min_participation_mode: ClarificationMode
    default_assumption: str | None
    recommended_answer: str | None
    answer_mode: ClarificationAnswerMode
    options: tuple[ClarificationOption, ...] = field(default_factory=tuple)
    affected_task_ids: tuple[str, ...] = field(default_factory=tuple)
    related_artifact_ids: tuple[str, ...] = field(default_factory=tuple)
    blocking_scope: ClarificationBlockingScope = "task"
    decision_owner_role: DecisionOwnerRole = "business"
    created_at: str = ""


@dataclass(frozen=True)
class ClarificationRequest:
    request_id: str
    project_id: str
    status: ClarificationStatus
    priority: ClarificationPriority
    title: str
    question: str
    description: str
    reason: str
    impact: str
    answer_mode: ClarificationAnswerMode
    options: tuple[ClarificationOption, ...] = field(default_factory=tuple)
    recommended_option_id: str | None = None
    min_participation_mode: ClarificationMode = "balanced"
    default_assumption: str | None = None
    affected_task_ids: tuple[str, ...] = field(default_factory=tuple)
    related_artifact_ids: tuple[str, ...] = field(default_factory=tuple)
    blocking_scope: ClarificationBlockingScope = "task"
    decision_owner_role: DecisionOwnerRole = "business"
    source_type: ClarificationSourceType = "validation"
    source_id: str = ""
    created_from_candidate_ids: tuple[str, ...] = field(default_factory=tuple)
    selected_option_ids: tuple[str, ...] = field(default_factory=tuple)
    free_text: str | None = None
    resolution_summary: str | None = None
    created_at: str = ""
    updated_at: str = ""
