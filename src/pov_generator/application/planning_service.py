from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import uuid

from ..common.serialization import utc_now_iso
from ..domain.planning import AdmissionCheck, CandidateEvaluation, PlanningDecision
from ..domain.problem_state import SetRecipeCompositionPatch
from ..domain.registry import (
    ComposedRecipe,
    DomainPackSpec,
    RecipeSpec,
    RegistrySnapshot,
    TemplateSpec,
    compose_recipe,
)
from ..domain.tasks import TaskRecord, initial_task_status
from ..infrastructure.sqlite_runtime import SqliteRuntime


@dataclass(frozen=True)
class RecipeBootstrap:
    base_recipe: RecipeSpec
    composed_recipe: ComposedRecipe
    template_lookup: dict[str, TemplateSpec]
    enabled_domain_pack_refs: tuple[str, ...]
    domain_packs: tuple[DomainPackSpec, ...]
    gap_labels: dict[str, str]


class PlanningService:
    def __init__(self, runtime: SqliteRuntime) -> None:
        self._runtime = runtime

    def build_recipe_bootstrap(
        self,
        snapshot: RegistrySnapshot,
        recipe_ref: str,
        enabled_domain_pack_refs: tuple[str, ...] = (),
    ) -> RecipeBootstrap:
        base_recipe = snapshot.resolve_recipe(recipe_ref)
        composed_recipe = compose_recipe(snapshot, recipe_ref, enabled_domain_pack_refs)
        template_lookup = {
            step.identifier: snapshot.resolve_template(step.template_ref)
            for step in composed_recipe.steps
        }
        return RecipeBootstrap(
            base_recipe=base_recipe,
            composed_recipe=composed_recipe,
            template_lookup=template_lookup,
            enabled_domain_pack_refs=tuple(sorted(set(enabled_domain_pack_refs))),
            domain_packs=tuple(
                snapshot.resolve_domain_pack(pack_ref)
                for pack_ref in tuple(sorted(set(enabled_domain_pack_refs)))
            ),
            gap_labels={
                entry.identifier: entry.label
                for entry in snapshot.vocabularies.get("gap_types", ()).entries.values()
            }
            if snapshot.vocabularies.get("gap_types")
            else {},
        )

    def _refresh_recipe_composition(self, workspace: Path, snapshot: RegistrySnapshot) -> ComposedRecipe:
        manifest = self._runtime.load_manifest(workspace)
        state = self._runtime.load_problem_state(workspace)
        enabled_pack_refs = tuple(sorted(state.enabled_domain_packs.keys()))
        composed_recipe = compose_recipe(snapshot, manifest.recipe_ref, enabled_pack_refs)
        composition = state.recipe_composition
        needs_update = (
            composition is None
            or composition.base_recipe_ref != composed_recipe.base_recipe_ref
            or composition.domain_pack_refs != composed_recipe.domain_pack_refs
            or composition.recipe_fragment_refs != composed_recipe.recipe_fragment_refs
            or composition.step_ids != tuple(step.identifier for step in composed_recipe.steps)
        )
        if needs_update:
            self._runtime.apply_problem_patch(
                workspace,
                SetRecipeCompositionPatch(
                    base_recipe_ref=composed_recipe.base_recipe_ref,
                    domain_pack_refs=composed_recipe.domain_pack_refs,
                    recipe_fragment_refs=composed_recipe.recipe_fragment_refs,
                    step_ids=tuple(step.identifier for step in composed_recipe.steps),
                ),
                actor="planner",
                reason="refresh composed recipe",
            )
        return composed_recipe

    def current_composed_recipe(self, workspace: Path, snapshot: RegistrySnapshot) -> ComposedRecipe:
        return self._refresh_recipe_composition(workspace, snapshot)

    def plan(
        self,
        workspace: Path,
        snapshot: RegistrySnapshot,
        mode: str = "dry-run",
        *,
        record: bool = True,
        refresh_composition: bool = True,
    ) -> PlanningDecision:
        manifest = self._runtime.load_manifest(workspace)
        recipe = snapshot.resolve_recipe(manifest.recipe_ref)
        state = self._runtime.load_problem_state(workspace)
        composed_recipe = (
            self._refresh_recipe_composition(workspace, snapshot)
            if refresh_composition
            else compose_recipe(snapshot, manifest.recipe_ref, tuple(sorted(state.enabled_domain_packs.keys())))
        )
        tasks = self._runtime.list_tasks(workspace)
        recipe_progress = {
            item.recipe_step_id: item for item in self._runtime.list_recipe_progress(workspace, manifest.recipe_ref)
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

        for step in composed_recipe.steps:
            if step.identifier in completed_steps:
                continue
            template = snapshot.resolve_template(step.template_ref)
            prior_required_incomplete = [
                previous.identifier
                for previous in composed_recipe.steps
                if previous.order < step.order and previous.required and previous.identifier not in completed_steps
            ]
            readiness_missing = [
                dimension
                for dimension in template.activation.required_readiness
                if state.readiness.get(dimension) is None
                or state.readiness[dimension].status not in {"ready", "waived"}
            ]
            forbidden_gaps = [gap_id for gap_id in template.activation.forbidden_open_gaps if gap_id in state.active_gaps]
            duplicate = f"{manifest.project_id}:{step.identifier}" in active_family_keys

            checks = (
                AdmissionCheck(
                    name="recipe_obligations",
                    passed=not prior_required_incomplete,
                    detail="Есть незавершённые обязательные предыдущие шаги: " + ", ".join(prior_required_incomplete)
                    if prior_required_incomplete
                    else "Все обязательные предыдущие шаги выполнены",
                ),
                AdmissionCheck(
                    name="required_readiness",
                    passed=not readiness_missing,
                    detail="Не хватает readiness: " + ", ".join(readiness_missing)
                    if readiness_missing
                    else "Readiness-предпосылки выполнены",
                ),
                AdmissionCheck(
                    name="forbidden_open_gaps",
                    passed=not forbidden_gaps,
                    detail="Есть открытые запрещающие gaps: " + ", ".join(forbidden_gaps)
                    if forbidden_gaps
                    else "Запрещающих gaps нет",
                ),
                AdmissionCheck(
                    name="dedup",
                    passed=not duplicate,
                    detail="Для этого recipe-шага уже существует активная задача"
                    if duplicate
                    else "Активной задачи для этого recipe-шага нет",
                ),
            )
            admissible = all(check.passed for check in checks)
            score = template.planning.priority - step.order
            reasons = tuple(check.detail for check in checks if not check.passed)
            evaluation = CandidateEvaluation(
                recipe_step_id=step.identifier,
                step_title=step.title,
                template_ref=step.template_ref.as_string(),
                step_source_kind=step.source_kind,
                step_source_ref=step.source_ref,
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
                domain_pack_refs=composed_recipe.domain_pack_refs,
                recipe_fragment_refs=composed_recipe.recipe_fragment_refs,
                mode=mode,
                outcome="blocked",
                selected_step_id=None,
                selected_template_ref=None,
                created_task_id=None,
                candidates=tuple(candidates),
                reasons=("Нет допустимых шагов. Проверьте readiness, gaps и обязательные предыдущие шаги.",),
                created_at=utc_now_iso(),
            )
            if record:
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
            domain_pack_refs=composed_recipe.domain_pack_refs,
            recipe_fragment_refs=composed_recipe.recipe_fragment_refs,
            mode=mode,
            outcome=outcome,
            selected_step_id=selected_step.identifier,
            selected_template_ref=selected_template.ref.as_string(),
            created_task_id=created_task_id,
            candidates=tuple(candidates),
            reasons=(f"Выбран допустимый шаг '{selected_step.identifier}'.",),
            created_at=utc_now_iso(),
        )
        if record:
            self._runtime.record_planning_decision(workspace, decision)
        return decision

    def planning_history(self, workspace: Path) -> list[PlanningDecision]:
        return self._runtime.list_planning_decisions(workspace)

    def transition_task(
        self,
        workspace: Path,
        task_id: str,
        command: str,
        *,
        payload: dict[str, object] | None = None,
    ):
        return self._runtime.transition_task(workspace, task_id, command, payload=payload)

    def list_tasks(self, workspace: Path):
        return self._runtime.list_tasks(workspace)

    def list_task_events(self, workspace: Path, task_id: str | None = None):
        return self._runtime.list_task_events(workspace, task_id=task_id)

    def list_recipe_progress(self, workspace: Path, recipe_ref: str):
        return self._runtime.list_recipe_progress(workspace, recipe_ref)
