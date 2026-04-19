from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pov_generator.common.errors import ConflictError, NotFoundError
from pov_generator.common.serialization import utc_now_iso


ReadinessStatus = Literal["missing", "partial", "ready", "waived"]
GapSeverity = Literal["low", "medium", "high", "critical"]


@dataclass(frozen=True)
class FactRecord:
    identifier: str
    statement: str
    source: str


@dataclass(frozen=True)
class GapRecord:
    identifier: str
    title: str
    description: str
    severity: GapSeverity
    blocking: bool
    opened_at: str
    closed_at: str | None = None


@dataclass(frozen=True)
class ReadinessRecord:
    dimension: str
    status: ReadinessStatus
    blocking: bool
    confidence: float
    evidence: tuple[str, ...]
    updated_at: str


@dataclass(frozen=True)
class ProblemState:
    project_id: str
    recipe_ref: str
    business_request: str
    goal: str | None
    known_facts: dict[str, FactRecord] = field(default_factory=dict)
    active_gaps: dict[str, GapRecord] = field(default_factory=dict)
    readiness: dict[str, ReadinessRecord] = field(default_factory=dict)
    version: int = 0
    updated_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class ProblemEvent:
    version: int
    patch_type: str
    payload: dict[str, object]
    actor: str
    reason: str
    created_at: str


@dataclass(frozen=True)
class SetGoalPatch:
    text: str


@dataclass(frozen=True)
class UpsertGapPatch:
    gap_id: str
    title: str
    description: str
    severity: GapSeverity = "medium"
    blocking: bool = True


@dataclass(frozen=True)
class CloseGapPatch:
    gap_id: str


@dataclass(frozen=True)
class UpsertReadinessPatch:
    dimension: str
    status: ReadinessStatus
    blocking: bool
    confidence: float = 1.0
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class AddFactPatch:
    fact_id: str
    statement: str
    source: str


ProblemPatch = SetGoalPatch | UpsertGapPatch | CloseGapPatch | UpsertReadinessPatch | AddFactPatch


def apply_problem_patch(state: ProblemState, patch: ProblemPatch) -> ProblemState:
    now = utc_now_iso()
    next_version = state.version + 1
    if isinstance(patch, SetGoalPatch):
        return ProblemState(
            project_id=state.project_id,
            recipe_ref=state.recipe_ref,
            business_request=state.business_request,
            goal=patch.text,
            known_facts=dict(state.known_facts),
            active_gaps=dict(state.active_gaps),
            readiness=dict(state.readiness),
            version=next_version,
            updated_at=now,
        )
    if isinstance(patch, UpsertGapPatch):
        gaps = dict(state.active_gaps)
        gaps[patch.gap_id] = GapRecord(
            identifier=patch.gap_id,
            title=patch.title,
            description=patch.description,
            severity=patch.severity,
            blocking=patch.blocking,
            opened_at=now,
        )
        return ProblemState(
            project_id=state.project_id,
            recipe_ref=state.recipe_ref,
            business_request=state.business_request,
            goal=state.goal,
            known_facts=dict(state.known_facts),
            active_gaps=gaps,
            readiness=dict(state.readiness),
            version=next_version,
            updated_at=now,
        )
    if isinstance(patch, CloseGapPatch):
        if patch.gap_id not in state.active_gaps:
            raise NotFoundError(f"Gap not found: {patch.gap_id}")
        gaps = dict(state.active_gaps)
        gaps.pop(patch.gap_id)
        return ProblemState(
            project_id=state.project_id,
            recipe_ref=state.recipe_ref,
            business_request=state.business_request,
            goal=state.goal,
            known_facts=dict(state.known_facts),
            active_gaps=gaps,
            readiness=dict(state.readiness),
            version=next_version,
            updated_at=now,
        )
    if isinstance(patch, UpsertReadinessPatch):
        if not 0.0 <= patch.confidence <= 1.0:
            raise ConflictError("Readiness confidence must be between 0 and 1.")
        readiness = dict(state.readiness)
        readiness[patch.dimension] = ReadinessRecord(
            dimension=patch.dimension,
            status=patch.status,
            blocking=patch.blocking,
            confidence=patch.confidence,
            evidence=patch.evidence,
            updated_at=now,
        )
        return ProblemState(
            project_id=state.project_id,
            recipe_ref=state.recipe_ref,
            business_request=state.business_request,
            goal=state.goal,
            known_facts=dict(state.known_facts),
            active_gaps=dict(state.active_gaps),
            readiness=readiness,
            version=next_version,
            updated_at=now,
        )
    if isinstance(patch, AddFactPatch):
        facts = dict(state.known_facts)
        facts[patch.fact_id] = FactRecord(patch.fact_id, patch.statement, patch.source)
        return ProblemState(
            project_id=state.project_id,
            recipe_ref=state.recipe_ref,
            business_request=state.business_request,
            goal=state.goal,
            known_facts=facts,
            active_gaps=dict(state.active_gaps),
            readiness=dict(state.readiness),
            version=next_version,
            updated_at=now,
        )
    raise TypeError(f"Unsupported problem patch: {type(patch)!r}")
