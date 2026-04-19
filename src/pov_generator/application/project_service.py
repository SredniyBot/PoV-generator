from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import uuid

from pov_generator.common.serialization import to_primitive, utc_now_iso
from pov_generator.domain.problem_state import (
    AddFactPatch,
    ProblemEvent,
    ProblemState,
    SetGoalPatch,
    UpsertGapPatch,
    UpsertReadinessPatch,
    apply_problem_patch,
)
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.sqlite_runtime import ProjectManifest, SqliteRuntime


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
        bootstrap_recipe: Any,
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

        core_orders = [
            step.order
            for step in bootstrap_recipe.recipe.steps
            if bootstrap_recipe.template_lookup[step.identifier].semantics.template_role == "core_task"
        ]
        first_core_order = min(core_orders) if core_orders else 10_000
        for step in bootstrap_recipe.recipe.steps:
            template = bootstrap_recipe.template_lookup[step.identifier]
            if step.order < first_core_order and template.semantics.template_role == "meta_analysis":
                for gap_id in template.semantics.closes_gaps:
                    state = apply_problem_patch(
                        state,
                        UpsertGapPatch(
                            gap_id=gap_id,
                            title=gap_id.replace("_", " ").title(),
                            description=f"Initial gap derived from recipe step '{step.identifier}'.",
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
        from pov_generator.domain.problem_state import CloseGapPatch

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
