from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .positions import VisibilityLevel

ClarificationStatus = Literal["open", "answered", "assumed", "deferred", "cancelled"]
ClarificationPriority = Literal["low", "medium", "high", "critical"]
ClarificationSourceType = Literal["task", "validation", "planning", "domain_pack", "methodology_pack", "quality_gate"]
ClarificationAnswerMode = Literal["single", "multiple", "free_text", "confirmation"]
ClarificationBlockingScope = Literal["none", "task", "subtree", "objective"]
ClarificationMode = Literal["autopilot", "balanced", "control", "expert"]
# Декомпозиция вопросов по «владельцу решения». Эта ось — **информационная**:
# используется для группировки/фильтрации в UI и для CE11 LLM-driver при
# подборе формулировки вопроса. **Не** влияет на ask/assume/defer-решение —
# для этого служит `visibility` (Этап 3 roadmap).
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
    """Сырой кандидат на уточнение.

    Этап 3 roadmap: ось «когда задавать пользователю» унифицирована
    через :attr:`visibility` — :class:`VisibilityLevel` положения,
    которое родится из этого уточнения. :attr:`decision_owner_role`
    остаётся **информационной осью** (UI-группировка, CE11 LLM-driver),
    не влияет на ask/assume/defer.
    """

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
    visibility: VisibilityLevel
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
    """Запрос на уточнение, показываемый пользователю или авто-решённый.

    См. :class:`ClarificationCandidate` — те же оси (``visibility`` для
    решения ask/assume/defer; ``decision_owner_role`` информационно).
    """

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
    visibility: VisibilityLevel = "architectural"
    default_assumption: str | None = None
    affected_task_ids: tuple[str, ...] = field(default_factory=tuple)
    related_artifact_ids: tuple[str, ...] = field(default_factory=tuple)
    blocking_scope: ClarificationBlockingScope = "task"
    decision_owner_role: DecisionOwnerRole = "business"
    # V1 (W6): True если первоначальная судьба request'а — авто-решение
    # (autopilot/balanced auto-assume или auto-defer). UI рисует 🤖 badge,
    # а инбокс — отдельный счётчик «N решено автоматически».
    auto_resolved: bool = False
    source_type: ClarificationSourceType = "validation"
    source_id: str = ""
    created_from_candidate_ids: tuple[str, ...] = field(default_factory=tuple)
    selected_option_ids: tuple[str, ...] = field(default_factory=tuple)
    free_text: str | None = None
    resolution_summary: str | None = None
    created_at: str = ""
    updated_at: str = ""
