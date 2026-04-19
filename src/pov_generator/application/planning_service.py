from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import uuid

from pov_generator.common.serialization import utc_now_iso
from pov_generator.domain.planning import AdmissionCheck, CandidateEvaluation, PlanningDecision
from pov_generator.domain.registry import RecipeSpec, RegistrySnapshot, TemplateSpec
from pov_generator.domain.tasks import TaskRecord, initial_task_status
from pov_generator.infrastructure.sqlite_runtime import ProjectManifest, SqliteRuntime


@dataclass(frozen=True)
class RecipeBootstrap:
    recipe: RecipeSpec
    template_lookup: dict[str, TemplateSpec]


class PlanningService:
    def __init__(self, runtime: SqliteRuntime) -> None:
        self._runtime = runtime

    def build_recipe_bootstrap(self, snapshot: RegistrySnapshot, recipe_ref: str) -> RecipeBootstrap:
        recipe = snapshot.resolve_recipe(recipe_ref)
        template_lookup = {step.identifier: snapshot.resolve_template(step.template_ref) for step in recipe.steps}
        return RecipeBootstrap(recipe=recipe, template_lookup=template_lookup)

    def plan(self, workspace: Path, snapshot: RegistrySnapshot, mode: str = "dry-run") -> PlanningDecision:
        manifest = self._runtime.load_manifest(workspace)
        recipe = snapshot.resolve_recipe(manifest.recipe_ref)
        state = self._runtime.load_problem_state(workspace)
        tasks = self._runtime.list_tasks(workspace)
        recipe_progress = {
            item.recipe_step_id: item for item in self._runtime.list_recipe_progress(workspace, recipe.ref.as_string())
        }
        active_family_keys = {
            task.task_family_key
            for task in tasks
            if task.status not in {"completed", "obsolete"}
        }

        candidates: list[CandidateEvaluation] = []
        selected_step = None
        selected_template = None
        best_score = -10_000

        completed_steps = {
            step_id for step_id, progress in recipe_progress.items() if progress.status == "completed"
        }

        for step in recipe.steps:
            if step.identifier in completed_steps:
                continue
            template = snapshot.resolve_template(step.template_ref)
            prior_required_incomplete = [
                previous.identifier
                for previous in recipe.steps
                if previous.order < step.order and previous.required and previous.identifier not in completed_steps
            ]
            readiness_missing = [
                dimension
                for dimension in template.activation.required_readiness
                if state.readiness.get(dimension) is None or state.readiness[dimension].status not in {"ready", "waived"}
            ]
            forbidden_gaps = [gap_id for gap_id in template.activation.forbidden_open_gaps if gap_id in state.active_gaps]
            duplicate = f"{manifest.project_id}:{step.identifier}" in active_family_keys

            checks = (
                AdmissionCheck(
                    name="recipe_obligations",
                    passed=not prior_required_incomplete,
                    detail="pending prior steps: " + ", ".join(prior_required_incomplete)
                    if prior_required_incomplete
                    else "all prior required steps are satisfied",
                ),
                AdmissionCheck(
                    name="required_readiness",
                    passed=not readiness_missing,
                    detail="missing readiness: " + ", ".join(readiness_missing)
                    if readiness_missing
                    else "readiness preconditions satisfied",
                ),
                AdmissionCheck(
                    name="forbidden_open_gaps",
                    passed=not forbidden_gaps,
                    detail="open gaps: " + ", ".join(forbidden_gaps)
                    if forbidden_gaps
                    else "no forbidden gaps are open",
                ),
                AdmissionCheck(
                    name="dedup",
                    passed=not duplicate,
                    detail="active task already exists for this recipe step"
                    if duplicate
                    else "no active task exists for this recipe step",
                ),
            )
            admissible = all(check.passed for check in checks)
            score = template.planning.priority - step.order
            reasons = tuple(check.detail for check in checks if not check.passed)
            evaluation = CandidateEvaluation(
                recipe_step_id=step.identifier,
                template_ref=step.template_ref.as_string(),
                admissible=admissible,
                score=score,
                duplicate=duplicate,
                checks=checks,
                reasons=reasons,
            )
            candidates.append(evaluation)
            if admissible and score > best_score:
                best_score = score
                selected_step = step
                selected_template = template

        if selected_step is None or selected_template is None:
            decision = PlanningDecision(
                project_id=manifest.project_id,
                recipe_ref=manifest.recipe_ref,
                mode=mode,
                outcome="blocked",
                selected_step_id=None,
                selected_template_ref=None,
                created_task_id=None,
                candidates=tuple(candidates),
                reasons=("No admissible step found. Review failed admission checks.",),
                created_at=utc_now_iso(),
            )
            self._runtime.record_planning_decision(workspace, decision)
            return decision

        created_task_id = None
        outcome = "selected"
        if mode == "apply":
            created_task_id = str(uuid.uuid4())
            created_task = TaskRecord(
                task_id=created_task_id,
                project_id=manifest.project_id,
                template_id=selected_template.identifier,
                template_version=selected_template.version,
                template_type=selected_template.template_type,
                template_role=selected_template.semantics.template_role,
                recipe_id=recipe.identifier,
                recipe_version=recipe.version,
                recipe_step_id=selected_step.identifier,
                task_family_key=f"{manifest.project_id}:{selected_step.identifier}",
                status=initial_task_status(selected_template.template_type),
                attempt=1,
                created_at=utc_now_iso(),
                updated_at=utc_now_iso(),
            )
            self._runtime.create_task(workspace, created_task)
            outcome = "materialized"

        decision = PlanningDecision(
            project_id=manifest.project_id,
            recipe_ref=manifest.recipe_ref,
            mode=mode,
            outcome=outcome,
            selected_step_id=selected_step.identifier,
            selected_template_ref=selected_template.ref.as_string(),
            created_task_id=created_task_id,
            candidates=tuple(candidates),
            reasons=(f"Selected admissible step '{selected_step.identifier}'.",),
            created_at=utc_now_iso(),
        )
        self._runtime.record_planning_decision(workspace, decision)
        return decision

    def planning_history(self, workspace: Path) -> list[PlanningDecision]:
        return self._runtime.list_planning_decisions(workspace)

    def transition_task(self, workspace: Path, task_id: str, command: str):
        return self._runtime.transition_task(workspace, task_id, command)

    def list_tasks(self, workspace: Path):
        return self._runtime.list_tasks(workspace)

    def list_task_events(self, workspace: Path, task_id: str | None = None):
        return self._runtime.list_task_events(workspace, task_id=task_id)

    def list_recipe_progress(self, workspace: Path, recipe_ref: str):
        return self._runtime.list_recipe_progress(workspace, recipe_ref)
