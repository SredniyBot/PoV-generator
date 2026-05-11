from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from ..common.serialization import to_primitive, utc_now_iso
from ..domain.problem_state import (
    ActivateDomainPackPatch,
    ActivateMethodologyPackPatch,
    AddFactPatch,
    CloseGapPatch,
    DisableMethodologyPackPatch,
    ProblemEvent,
    ProblemState,
    SetClarificationModePatch,
    SetGoalPatch,
    UpsertGapPatch,
    UpsertReadinessPatch,
    apply_problem_patch,
)
from ..domain.registry import DomainPackSpec, ObjectRef
from ..infrastructure.sqlite_runtime import ProjectManifest, SqliteRuntime


@dataclass(frozen=True)
class ProjectBootstrap:
    manifest: ProjectManifest
    state: ProblemState


class ProjectService:
    def __init__(self, runtime: SqliteRuntime) -> None:
        self._runtime = runtime

    def init_project(
        self,
        workspace: Path,
        name: str,
        objective_ref: ObjectRef,
        request_text: str,
        domain_packs: tuple[DomainPackSpec, ...] = (),
        default_methodology_pack_ref: str | None = "process.lean_jtbd@1.0.0",
    ) -> ProjectBootstrap:
        project_id = str(uuid.uuid4())
        manifest = ProjectManifest(
            project_id=project_id,
            name=name,
            objective_ref=objective_ref.as_string(),
            created_at=utc_now_iso(),
        )
        state = ProblemState(
            project_id=project_id,
            objective_ref=objective_ref.as_string(),
            root_task_id=None,
            business_request=request_text.strip(),
            goal=None,
        )

        state = apply_problem_patch(
            state,
            AddFactPatch(
                fact_id="initial_request",
                statement=request_text.strip(),
                source="project_init",
            ),
        )
        for pack in domain_packs:
            state = apply_problem_patch(
                state,
                ActivateDomainPackPatch(
                    pack_ref=pack.ref.as_string(),
                    domain=pack.domain,
                    source="bootstrap",
                    rationale="Доменный пакет выбран при создании проекта.",
                    confidence=1.0,
                ),
            )
        if default_methodology_pack_ref:
            state = apply_problem_patch(
                state,
                ActivateMethodologyPackPatch(
                    pack_ref=default_methodology_pack_ref,
                    source="bootstrap",
                    rationale="Дефолтная методология проекта.",
                ),
            )

        bootstrap_event = ProblemEvent(
            version=state.version,
            patch_type="bootstrap_state",
            payload={"objective_ref": objective_ref.as_string(), "state": to_primitive(state)},
            actor="system",
            reason="project initialization",
            created_at=utc_now_iso(),
        )
        self._runtime.create_workspace(workspace, manifest, state, bootstrap_event)
        return ProjectBootstrap(manifest=manifest, state=state)

    def load_manifest(self, workspace: Path) -> ProjectManifest:
        return self._runtime.load_manifest(workspace)

    def load_problem_state(self, workspace: Path) -> ProblemState:
        return self._runtime.load_problem_state(workspace)

    def problem_history(self, workspace: Path) -> list[ProblemEvent]:
        return self._runtime.list_problem_events(workspace)

    def set_goal(self, workspace: Path, text: str, actor: str = "operator", reason: str = "manual update") -> ProblemState:
        return self._runtime.apply_problem_patch(workspace, SetGoalPatch(text=text), actor=actor, reason=reason)

    def add_gap(
        self,
        workspace: Path,
        gap_id: str,
        title: str,
        description: str,
        severity: str,
        blocking: bool,
        actor: str = "operator",
        reason: str = "manual update",
    ) -> ProblemState:
        return self._runtime.apply_problem_patch(
            workspace,
            UpsertGapPatch(gap_id=gap_id, title=title, description=description, severity=severity, blocking=blocking),
            actor=actor,
            reason=reason,
        )

    def close_gap(self, workspace: Path, gap_id: str, actor: str = "operator", reason: str = "manual update") -> ProblemState:
        return self._runtime.apply_problem_patch(workspace, CloseGapPatch(gap_id=gap_id), actor=actor, reason=reason)

    def set_readiness(
        self,
        workspace: Path,
        dimension: str,
        status: str,
        blocking: bool,
        confidence: float,
        actor: str = "operator",
        reason: str = "manual update",
    ) -> ProblemState:
        return self._runtime.apply_problem_patch(
            workspace,
            UpsertReadinessPatch(dimension=dimension, status=status, blocking=blocking, confidence=confidence),
            actor=actor,
            reason=reason,
        )

    def add_fact(self, workspace: Path, fact_id: str, statement: str, source: str) -> ProblemState:
        return self._runtime.apply_problem_patch(
            workspace,
            AddFactPatch(fact_id=fact_id, statement=statement, source=source),
            actor="operator",
            reason="manual fact registration",
        )

    def set_clarification_mode(self, workspace: Path, mode: str) -> ProblemState:
        return self._runtime.apply_problem_patch(
            workspace,
            SetClarificationModePatch(mode=mode),  # type: ignore[arg-type]
            actor="operator",
            reason="clarification mode changed",
        )

    def enable_domain_pack(
        self,
        workspace: Path,
        pack: DomainPackSpec,
        actor: str = "operator",
        reason: str = "manual domain activation",
    ) -> ProblemState:
        return self._runtime.apply_problem_patch(
            workspace,
            ActivateDomainPackPatch(
                pack_ref=pack.ref.as_string(),
                domain=pack.domain,
                source="operator" if actor == "operator" else "system",
                rationale=reason,
                confidence=1.0,
            ),
            actor=actor,
            reason=reason,
        )

    def set_methodology(
        self,
        workspace: Path,
        pack_ref: str,
        actor: str = "operator",
        reason: str = "manual methodology activation",
    ) -> ProblemState:
        state = self._runtime.load_problem_state(workspace)
        for ref, record in state.active_methodology_packs.items():
            if record.status == "active" and ref != pack_ref:
                state = self._runtime.apply_problem_patch(
                    workspace,
                    DisableMethodologyPackPatch(pack_ref=ref),
                    actor=actor,
                    reason=f"replaced by {pack_ref}",
                )
        return self._runtime.apply_problem_patch(
            workspace,
            ActivateMethodologyPackPatch(
                pack_ref=pack_ref,
                source="operator" if actor == "operator" else "system",
                rationale=reason,
            ),
            actor=actor,
            reason=reason,
        )
