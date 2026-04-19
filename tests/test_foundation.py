from __future__ import annotations

from pathlib import Path
import shutil

import yaml

from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime


REPO_ROOT = Path(__file__).resolve().parents[1]


def build_services(registry_root: Path | None = None):
    registry_service = RegistryService(FilesystemRegistryLoader(registry_root or REPO_ROOT / "templates"))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)
    return registry_service, project_service, planning_service


def init_workspace(tmp_path: Path, domain_packs: tuple[str, ...] = ()):
    registry_service, project_service, planning_service = build_services()
    snapshot, report = registry_service.validate()
    assert report.is_valid
    recipe_ref = ObjectRef.parse("common.build_requirements_spec@1.0.0")
    bootstrap_recipe = planning_service.build_recipe_bootstrap(
        snapshot,
        recipe_ref.as_string(),
        enabled_domain_pack_refs=domain_packs,
    )
    workspace = tmp_path / "case"
    project_service.init_project(
        workspace=workspace,
        name="Demo",
        recipe_ref=recipe_ref,
        request_text="Нужен сервис для преобразования бизнес-запроса в ТЗ.",
        bootstrap_recipe=bootstrap_recipe,
    )
    return workspace, registry_service, project_service, planning_service


def complete_task(workspace: Path, planning_service: PlanningService, task_id: str) -> None:
    planning_service.transition_task(workspace, task_id, "start")
    planning_service.transition_task(workspace, task_id, "complete")


def test_registry_validation_passes_for_sample_corpus() -> None:
    registry_service, _, _ = build_services()
    snapshot, report = registry_service.validate()

    assert report.is_valid
    assert len(snapshot.templates) == 6
    assert len(snapshot.recipes) == 1
    assert len(snapshot.recipe_fragments) == 1
    assert len(snapshot.domain_packs) == 1
    assert len(snapshot.vocabularies) == 5


def test_registry_validation_detects_unknown_gap_reference(tmp_path: Path) -> None:
    registry_root = tmp_path / "templates"
    shutil.copytree(REPO_ROOT / "templates", registry_root)
    template_path = registry_root / "templates" / "common" / "goal_clarification.yaml"
    raw = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    raw["semantics"]["closes_gaps"] = ["unknown_gap"]
    template_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")

    registry_service, _, _ = build_services(registry_root)
    _, report = registry_service.validate()

    assert not report.is_valid
    assert any("unknown_gap" in issue.message for issue in report.errors)


def test_problem_state_patches_persist_and_history(tmp_path: Path) -> None:
    workspace, _, project_service, _ = init_workspace(tmp_path)

    project_service.set_goal(workspace, "Подготовить качественное ТЗ.")
    project_service.set_readiness(workspace, "goal_clarity", "ready", blocking=False, confidence=0.95)
    project_service.close_gap(workspace, "unclear_goal")
    state = project_service.load_problem_state(workspace)
    history = project_service.problem_history(workspace)

    assert state.goal == "Подготовить качественное ТЗ."
    assert state.readiness["goal_clarity"].status == "ready"
    assert "unclear_goal" not in state.active_gaps
    assert len(history) >= 4


def test_planner_materializes_first_meta_step_and_tracks_progress(tmp_path: Path) -> None:
    workspace, registry_service, project_service, planning_service = init_workspace(tmp_path)
    snapshot, _ = registry_service.validate()

    decision = planning_service.plan(workspace, snapshot, mode="apply")
    tasks = planning_service.list_tasks(workspace)

    assert decision.outcome == "materialized"
    assert decision.selected_step_id == "goal_clarification"
    assert decision.domain_pack_refs == ()
    assert len(tasks) == 1
    assert tasks[0].status == "queued"

    complete_task(workspace, planning_service, tasks[0].task_id)
    recipe_progress = planning_service.list_recipe_progress(
        workspace, project_service.load_manifest(workspace).recipe_ref
    )

    assert any(item.recipe_step_id == "goal_clarification" and item.status == "completed" for item in recipe_progress)


def test_planner_moves_to_next_step_only_after_manual_readiness_progress(tmp_path: Path) -> None:
    workspace, registry_service, project_service, planning_service = init_workspace(tmp_path)
    snapshot, _ = registry_service.validate()

    first_decision = planning_service.plan(workspace, snapshot, mode="apply")
    first_task = planning_service.list_tasks(workspace)[0]
    assert first_decision.selected_step_id == "goal_clarification"

    planning_service.transition_task(workspace, first_task.task_id, "start")
    project_service.set_goal(workspace, "Подготовить согласованное ТЗ.")
    project_service.set_readiness(workspace, "goal_clarity", "ready", blocking=False, confidence=0.9)
    project_service.close_gap(workspace, "unclear_goal")
    planning_service.transition_task(workspace, first_task.task_id, "complete")

    second_decision = planning_service.plan(workspace, snapshot, mode="dry-run")

    assert second_decision.selected_step_id == "user_story_scan"
    assert second_decision.selected_template_ref == "common.user_story_scan@1.0.0"


def test_planner_blocks_duplicate_materialization(tmp_path: Path) -> None:
    workspace, registry_service, _, planning_service = init_workspace(tmp_path)
    snapshot, _ = registry_service.validate()

    planning_service.plan(workspace, snapshot, mode="apply")
    second_decision = planning_service.plan(workspace, snapshot, mode="dry-run")

    assert second_decision.outcome == "blocked"
    assert any(
        candidate.recipe_step_id == "goal_clarification" and candidate.duplicate
        for candidate in second_decision.candidates
    )


def test_frontend_domain_pack_extends_recipe_and_blocks_core_until_frontend_step_done(tmp_path: Path) -> None:
    workspace, registry_service, project_service, planning_service = init_workspace(
        tmp_path,
        domain_packs=("frontend.web_app_requirements@1.0.0",),
    )
    snapshot, _ = registry_service.validate()
    state = project_service.load_problem_state(workspace)
    composed_recipe = planning_service.current_composed_recipe(workspace, snapshot)

    assert "frontend.web_app_requirements@1.0.0" in state.enabled_domain_packs
    assert state.recipe_composition is not None
    assert "frontend_user_flow_analysis" in state.recipe_composition.step_ids
    assert composed_recipe.domain_pack_refs == ("frontend.web_app_requirements@1.0.0",)
    assert any(step.identifier == "frontend_user_flow_analysis" for step in composed_recipe.steps)

    for readiness_id, gap_id in (
        ("goal_clarity", "unclear_goal"),
        ("user_story_coverage", "missing_user_stories"),
        ("alternatives_explored", "alternatives_not_explored"),
    ):
        decision = planning_service.plan(workspace, snapshot, mode="apply")
        assert decision.outcome == "materialized"
        assert decision.created_task_id is not None
        complete_task(workspace, planning_service, decision.created_task_id)
        project_service.set_readiness(workspace, readiness_id, "ready", blocking=False, confidence=0.9)
        project_service.close_gap(workspace, gap_id)

    frontend_decision = planning_service.plan(workspace, snapshot, mode="dry-run")

    assert frontend_decision.selected_step_id == "frontend_user_flow_analysis"
    assert frontend_decision.domain_pack_refs == ("frontend.web_app_requirements@1.0.0",)
    assert frontend_decision.recipe_fragment_refs == ("frontend.requirements_extension@1.0.0",)
    assert any(
        candidate.recipe_step_id == "frontend_user_flow_analysis"
        and candidate.step_source_kind == "recipe_fragment"
        and candidate.step_source_ref == "frontend.requirements_extension@1.0.0"
        for candidate in frontend_decision.candidates
    )
