from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .clarifications import ClarificationMode
from ..common.errors import ConflictError, NotFoundError
from ..common.serialization import utc_now_iso


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
class DomainSignalRecord:
    domain: str
    signal: str
    source: str
    confidence: float
    detected_at: str


@dataclass(frozen=True)
class ActiveDomainPackRecord:
    ref: str
    domain: str
    status: Literal["candidate", "active", "disabled"]
    source: Literal["llm_detector", "operator", "artifact", "system", "bootstrap"]
    rationale: str
    confidence: float
    activated_at: str


@dataclass(frozen=True)
class ActiveMethodologyPackRecord:
    ref: str
    status: Literal["active", "disabled"]
    source: Literal["operator", "objective_default", "system", "bootstrap"]
    rationale: str
    activated_at: str


@dataclass(frozen=True)
class ProblemState:
    project_id: str
    objective_ref: str
    root_task_id: str | None
    business_request: str
    goal: str | None
    known_facts: dict[str, FactRecord] = field(default_factory=dict)
    assumptions: dict[str, FactRecord] = field(default_factory=dict)
    constraints: dict[str, FactRecord] = field(default_factory=dict)
    risks: dict[str, FactRecord] = field(default_factory=dict)
    active_gaps: dict[str, GapRecord] = field(default_factory=dict)
    decisions: dict[str, FactRecord] = field(default_factory=dict)
    readiness: dict[str, ReadinessRecord] = field(default_factory=dict)
    domain_signals: dict[str, DomainSignalRecord] = field(default_factory=dict)
    active_domain_packs: dict[str, ActiveDomainPackRecord] = field(default_factory=dict)
    active_methodology_packs: dict[str, ActiveMethodologyPackRecord] = field(default_factory=dict)
    clarification_mode: ClarificationMode = "balanced"
    version: int = 0
    updated_at: str = field(default_factory=utc_now_iso)

    @property
    def active_domain_pack_records(self) -> dict[str, ActiveDomainPackRecord]:
        return {key: value for key, value in self.active_domain_packs.items() if value.status == "active"}

    @property
    def active_methodology_pack_records(self) -> dict[str, ActiveMethodologyPackRecord]:
        return {key: value for key, value in self.active_methodology_packs.items() if value.status == "active"}


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
class SetRootTaskPatch:
    task_id: str


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


@dataclass(frozen=True)
class UpsertAssumptionPatch:
    assumption_id: str
    statement: str
    source: str


@dataclass(frozen=True)
class UpsertDecisionPatch:
    decision_id: str
    statement: str
    source: str


@dataclass(frozen=True)
class DetectDomainSignalPatch:
    domain: str
    signal: str
    source: str
    confidence: float


@dataclass(frozen=True)
class ActivateDomainPackPatch:
    pack_ref: str
    domain: str
    source: Literal["llm_detector", "operator", "artifact", "system", "bootstrap"] = "operator"
    rationale: str = ""
    confidence: float = 1.0


@dataclass(frozen=True)
class DisableDomainPackPatch:
    pack_ref: str


@dataclass(frozen=True)
class ActivateMethodologyPackPatch:
    pack_ref: str
    source: Literal["operator", "objective_default", "system", "bootstrap"] = "operator"
    rationale: str = ""


@dataclass(frozen=True)
class DisableMethodologyPackPatch:
    pack_ref: str


@dataclass(frozen=True)
class SetClarificationModePatch:
    mode: ClarificationMode


ProblemPatch = (
    SetGoalPatch
    | SetRootTaskPatch
    | UpsertGapPatch
    | CloseGapPatch
    | UpsertReadinessPatch
    | AddFactPatch
    | UpsertAssumptionPatch
    | UpsertDecisionPatch
    | DetectDomainSignalPatch
    | ActivateDomainPackPatch
    | DisableDomainPackPatch
    | ActivateMethodologyPackPatch
    | DisableMethodologyPackPatch
    | SetClarificationModePatch
)


def _copy_state(state: ProblemState, **changes) -> ProblemState:
    payload = {
        "project_id": state.project_id,
        "objective_ref": state.objective_ref,
        "root_task_id": state.root_task_id,
        "business_request": state.business_request,
        "goal": state.goal,
        "known_facts": dict(state.known_facts),
        "assumptions": dict(state.assumptions),
        "constraints": dict(state.constraints),
        "risks": dict(state.risks),
        "active_gaps": dict(state.active_gaps),
        "decisions": dict(state.decisions),
        "readiness": dict(state.readiness),
        "domain_signals": dict(state.domain_signals),
        "active_domain_packs": dict(state.active_domain_packs),
        "active_methodology_packs": dict(state.active_methodology_packs),
        "clarification_mode": state.clarification_mode,
        "version": state.version + 1,
        "updated_at": utc_now_iso(),
    }
    payload.update(changes)
    return ProblemState(**payload)


def apply_problem_patch(state: ProblemState, patch: ProblemPatch) -> ProblemState:
    now = utc_now_iso()
    if isinstance(patch, SetGoalPatch):
        return _copy_state(state, goal=patch.text)
    if isinstance(patch, SetRootTaskPatch):
        return _copy_state(state, root_task_id=patch.task_id)
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
        return _copy_state(state, active_gaps=gaps)
    if isinstance(patch, CloseGapPatch):
        if patch.gap_id not in state.active_gaps:
            raise NotFoundError(f"Gap not found: {patch.gap_id}")
        gaps = dict(state.active_gaps)
        gaps.pop(patch.gap_id)
        return _copy_state(state, active_gaps=gaps)
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
        return _copy_state(state, readiness=readiness)
    if isinstance(patch, AddFactPatch):
        facts = dict(state.known_facts)
        facts[patch.fact_id] = FactRecord(patch.fact_id, patch.statement, patch.source)
        return _copy_state(state, known_facts=facts)
    if isinstance(patch, UpsertAssumptionPatch):
        assumptions = dict(state.assumptions)
        assumptions[patch.assumption_id] = FactRecord(patch.assumption_id, patch.statement, patch.source)
        return _copy_state(state, assumptions=assumptions)
    if isinstance(patch, UpsertDecisionPatch):
        decisions = dict(state.decisions)
        decisions[patch.decision_id] = FactRecord(patch.decision_id, patch.statement, patch.source)
        return _copy_state(state, decisions=decisions)
    if isinstance(patch, DetectDomainSignalPatch):
        signals = dict(state.domain_signals)
        key = f"{patch.domain}:{patch.signal}"
        signals[key] = DomainSignalRecord(
            domain=patch.domain,
            signal=patch.signal,
            source=patch.source,
            confidence=patch.confidence,
            detected_at=now,
        )
        return _copy_state(state, domain_signals=signals)
    if isinstance(patch, ActivateDomainPackPatch):
        packs = dict(state.active_domain_packs)
        packs[patch.pack_ref] = ActiveDomainPackRecord(
            ref=patch.pack_ref,
            domain=patch.domain,
            status="active",
            source=patch.source,
            rationale=patch.rationale,
            confidence=patch.confidence,
            activated_at=now,
        )
        return _copy_state(state, active_domain_packs=packs)
    if isinstance(patch, DisableDomainPackPatch):
        if patch.pack_ref not in state.active_domain_packs:
            raise NotFoundError(f"Domain pack not found in ProblemState: {patch.pack_ref}")
        packs = dict(state.active_domain_packs)
        current = packs[patch.pack_ref]
        packs[patch.pack_ref] = ActiveDomainPackRecord(
            ref=current.ref,
            domain=current.domain,
            status="disabled",
            source=current.source,
            rationale=current.rationale,
            confidence=current.confidence,
            activated_at=current.activated_at,
        )
        return _copy_state(state, active_domain_packs=packs)
    if isinstance(patch, ActivateMethodologyPackPatch):
        packs = dict(state.active_methodology_packs)
        packs[patch.pack_ref] = ActiveMethodologyPackRecord(
            ref=patch.pack_ref,
            status="active",
            source=patch.source,
            rationale=patch.rationale,
            activated_at=now,
        )
        return _copy_state(state, active_methodology_packs=packs)
    if isinstance(patch, DisableMethodologyPackPatch):
        if patch.pack_ref not in state.active_methodology_packs:
            raise NotFoundError(f"Methodology pack not found in ProblemState: {patch.pack_ref}")
        packs = dict(state.active_methodology_packs)
        current = packs[patch.pack_ref]
        packs[patch.pack_ref] = ActiveMethodologyPackRecord(
            ref=current.ref,
            status="disabled",
            source=current.source,
            rationale=current.rationale,
            activated_at=current.activated_at,
        )
        return _copy_state(state, active_methodology_packs=packs)
    if isinstance(patch, SetClarificationModePatch):
        return _copy_state(state, clarification_mode=patch.mode)
    raise TypeError(f"Unsupported problem patch: {type(patch)!r}")
