from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import uuid

from ..common.serialization import to_primitive, utc_now_iso
from ..domain.problem_state import (
    AddFactPatch,
    EnableDomainPackPatch,
    ProblemEvent,
    ProblemState,
    SetGoalPatch,
    SetRecipeCompositionPatch,
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
        recipe_ref: ObjectRef,
        request_text: str,
        bootstrap_recipe,
    ) -> ProjectBootstrap:
        project_id = str(uuid.uuid4())
        manifest = ProjectManifest(
            project_id=project_id,
            name=name,
            recipe_ref=recipe_ref.as_string(),
            created_at=utc_now_iso(),
        )
        state = ProblemState(
            project_id=project_id,
            recipe_ref=recipe_ref.as_string(),
            business_request=request_text.strip(),
            goal=None,
        )

        for pack_ref in bootstrap_recipe.enabled_domain_pack_refs:
            pack = next(
                (
                    snapshot_pack
                    for snapshot_pack in bootstrap_recipe.domain_packs
                    if snapshot_pack.ref.as_string() == pack_ref
                ),
                None,
            )
            if pack is None:
                raise ValueError(f"Bootstrap pack not found: {pack_ref}")
            state = apply_problem_patch(
                state,
                EnableDomainPackPatch(
                    pack_ref=pack.ref.as_string(),
                    domain=pack.domain,
                    source="bootstrap",
                ),
            )

        state = apply_problem_patch(
            state,
            SetRecipeCompositionPatch(
                base_recipe_ref=bootstrap_recipe.base_recipe.ref.as_string(),
                domain_pack_refs=bootstrap_recipe.enabled_domain_pack_refs,
                recipe_fragment_refs=bootstrap_recipe.composed_recipe.recipe_fragment_refs,
                step_ids=tuple(step.identifier for step in bootstrap_recipe.composed_recipe.steps),
            ),
        )

        core_orders = [
            step.order
            for step in bootstrap_recipe.composed_recipe.steps
            if bootstrap_recipe.template_lookup[step.identifier].semantics.template_role == "core_task"
        ]
        first_core_order = min(core_orders) if core_orders else 10_000
        for step in bootstrap_recipe.composed_recipe.steps:
            template = bootstrap_recipe.template_lookup[step.identifier]
            if step.order < first_core_order and template.semantics.template_role == "meta_analysis":
                for gap_id in template.semantics.closes_gaps:
                    state = apply_problem_patch(
                        state,
                        UpsertGapPatch(
                            gap_id=gap_id,
                            title=bootstrap_recipe.gap_labels.get(gap_id, gap_id.replace("_", " ").title()),
                            description=f"Стартовый gap сформирован из шага '{step.identifier}'.",
                            severity="high",
                            blocking=True,
                        ),
                    )
            for readiness_id in step.completion.readiness:
                state = apply_problem_patch(
                    state,
                    UpsertReadinessPatch(
                        dimension=readiness_id,
                        status="missing",
                        blocking=step.order <= first_core_order,
                        confidence=1.0,
                    ),
                )
        state = apply_problem_patch(
            state,
            AddFactPatch(
                fact_id="initial_request",
                statement=request_text.strip(),
                source="project_init",
            ),
        )

        bootstrap_event = ProblemEvent(
            version=state.version,
            patch_type="bootstrap_state",
            payload={"recipe_ref": recipe_ref.as_string(), "state": to_primitive(state)},
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
        from ..domain.problem_state import CloseGapPatch

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

    def enable_domain_pack(
        self,
        workspace: Path,
        pack: DomainPackSpec,
        actor: str = "operator",
        reason: str = "manual domain activation",
    ) -> ProblemState:
        return self._runtime.apply_problem_patch(
            workspace,
            EnableDomainPackPatch(pack_ref=pack.ref.as_string(), domain=pack.domain, source=actor),
            actor=actor,
            reason=reason,
        )
