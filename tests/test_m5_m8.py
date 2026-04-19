from __future__ import annotations

from pathlib import Path
import json
import uuid

from pov_generator.application.context_service import ContextService
from pov_generator.application.execution_service import ExecutionBundle, ExecutionService
from pov_generator.application.artifact_contracts import artifact_schema
from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.application.validation_service import ValidationService
from pov_generator.application.workflow_service import WorkflowService
from pov_generator.domain.artifacts import ArtifactRecord
from pov_generator.domain.execution import ExecutionOutput, ExecutionRequest, ExecutionResult
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime


REPO_ROOT = Path(__file__).resolve().parents[1]


def build_services(registry_root: Path | None = None):
    registry_service = RegistryService(FilesystemRegistryLoader(registry_root or REPO_ROOT / "templates"))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)
    context_service = ContextService(runtime)
    execution_service = ExecutionService(runtime, context_service)
    validation_service = ValidationService(runtime)
    workflow_service = WorkflowService(runtime, planning_service, execution_service, validation_service)
    return (
        registry_service,
        runtime,
        project_service,
        planning_service,
        context_service,
        execution_service,
        validation_service,
        workflow_service,
    )


def init_workspace(tmp_path: Path, domain_packs: tuple[str, ...] = ()):
    (
        registry_service,
        runtime,
        project_service,
        planning_service,
        context_service,
        execution_service,
        validation_service,
        workflow_service,
    ) = build_services()
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
    return (
        workspace,
        snapshot,
        runtime,
        project_service,
        planning_service,
        context_service,
        execution_service,
        validation_service,
        workflow_service,
    )


def test_context_builder_collects_previous_artifacts_for_spec_generation(tmp_path: Path) -> None:
    (
        workspace,
        snapshot,
        _runtime,
        _project_service,
        planning_service,
        context_service,
        _execution_service,
        _validation_service,
        workflow_service,
    ) = init_workspace(tmp_path)

    for _ in range(3):
        result = workflow_service.run_next(workspace, snapshot, provider="stub")
        assert result.validation_status == "passed"

    decision = planning_service.plan(workspace, snapshot, mode="apply")
    assert decision.selected_step_id == "requirements_spec_generation"
    assert decision.created_task_id is not None

    context_result = context_service.build_for_task(workspace, snapshot, decision.created_task_id)
    manifest = context_result.manifest

    assert manifest.template_ref == "common.requirements_spec_generation@1.0.0"
    assert any(item.title == "ProblemState.business_request" for item in manifest.items)
    artifact_titles = {item.title for item in manifest.items if item.item_type == "artifact"}
    assert "Уточнение бизнес-цели (clarification_notes)" in artifact_titles
    assert "Анализ user story (user_story_map)" in artifact_titles
    assert "Сравнение альтернатив решения (alternatives_analysis)" in artifact_titles
    assert manifest.budget.used_tokens > 0


def test_stub_workflow_runs_common_recipe_end_to_end(tmp_path: Path) -> None:
    (
        workspace,
        snapshot,
        runtime,
        project_service,
        _planning_service,
        _context_service,
        _execution_service,
        _validation_service,
        workflow_service,
    ) = init_workspace(tmp_path)

    result = workflow_service.run_until_blocked(workspace, snapshot, provider="stub")

    assert result.stopped_reason == "recipe_completed"
    assert len(result.steps) == 5
    artifact_roles = {artifact.artifact_role for artifact in runtime.list_artifacts(workspace)}
    assert artifact_roles == {
        "clarification_notes",
        "user_story_map",
        "alternatives_analysis",
        "requirements_spec",
        "review_report",
    }
    validation_runs = runtime.list_validation_runs(workspace)
    assert len(validation_runs) == 5
    assert all(run.status == "passed" for run in validation_runs)
    state = project_service.load_problem_state(workspace)
    assert state.active_gaps == {}
    assert all(item.status == "ready" for item in state.readiness.values())


def test_frontend_domain_pack_changes_spec_generation_and_produces_ui_artifact(tmp_path: Path) -> None:
    (
        workspace,
        snapshot,
        runtime,
        _project_service,
        _planning_service,
        _context_service,
        _execution_service,
        _validation_service,
        workflow_service,
    ) = init_workspace(tmp_path, domain_packs=("frontend.web_app_requirements@1.0.0",))

    result = workflow_service.run_until_blocked(workspace, snapshot, provider="stub")

    assert result.stopped_reason == "recipe_completed"
    artifact_roles = {artifact.artifact_role for artifact in runtime.list_artifacts(workspace)}
    assert "ui_requirements_outline" in artifact_roles
    spec_artifact = runtime.latest_artifact_by_role(workspace, "requirements_spec")
    assert spec_artifact is not None
    payload = json.loads(runtime.load_artifact_content(workspace, spec_artifact.artifact_id))
    assert "frontend_requirements" in payload
    assert payload["frontend_requirements"]["screens"]
    assert payload["frontend_requirements"]["user_flows"]


def test_validation_creates_escalation_for_failed_review_report(tmp_path: Path) -> None:
    (
        workspace,
        snapshot,
        runtime,
        _project_service,
        planning_service,
        _context_service,
        _execution_service,
        validation_service,
        _workflow_service,
    ) = init_workspace(tmp_path)

    decision = planning_service.plan(workspace, snapshot, mode="apply")
    assert decision.created_task_id is not None
    task = runtime.get_task(workspace, decision.created_task_id)

    artifact = ArtifactRecord(
        artifact_id=str(uuid.uuid4()),
        project_id=task.project_id,
        artifact_role="review_report",
        title="Ревью ТЗ (review_report)",
        description="Искусственно созданный артефакт для теста",
        artifact_format="json",
        artifact_kind="primary",
        created_by_task_id=task.task_id,
        parent_artifact_id=None,
        metadata={"template_ref": "common.requirements_spec_review@1.0.0"},
        storage_path=f"artifacts/{uuid.uuid4()}.json",
        created_at="2026-04-20T00:00:00+00:00",
    )
    runtime.store_artifact(
        workspace,
        artifact=artifact,
        content=json.dumps(
            {
                "overall_status": "needs_changes",
                "summary": "Документ требует доработки.",
                "strengths": ["Структура документа понятна."],
                "issues": [{"severity": "error", "message": "Нет функциональных требований."}],
                "recommendations": ["Исправить замечания."],
            },
            ensure_ascii=False,
        ),
    )

    bundle = ExecutionBundle(
        request=ExecutionRequest(
            execution_run_id=str(uuid.uuid4()),
            project_id=task.project_id,
            task_id=task.task_id,
            template_ref="common.requirements_spec_review@1.0.0",
            context_manifest_id="manual-test",
            provider="stub",
            model="stub",
            actor="test",
        ),
        result=ExecutionResult(
            execution_run_id=str(uuid.uuid4()),
            status="succeeded",
            outputs=(ExecutionOutput(artifact_id=artifact.artifact_id, artifact_role="review_report"),),
            trace_ids=(),
        ),
        traces=(),
    )

    validation_run = validation_service.validate_execution(
        workspace,
        snapshot,
        task_id=task.task_id,
        execution_bundle=bundle,
    )

    assert validation_run.status == "failed"
    assert any("Ревью не прошло" in finding.message for finding in validation_run.findings)
    escalations = runtime.list_escalations(workspace)
    assert len(escalations) == 1
    assert escalations[0].reason_code == "validation_failed"


def test_requirements_spec_schema_depends_on_active_domain_packs() -> None:
    base_schema = artifact_schema("requirements_spec", ())
    frontend_schema = artifact_schema("requirements_spec", ("frontend.web_app_requirements@1.0.0",))

    assert "frontend_requirements" not in base_schema["properties"]
    assert "frontend_requirements" in frontend_schema["properties"]
    assert "frontend_requirements" not in base_schema["required"]
    assert "frontend_requirements" in frontend_schema["required"]
