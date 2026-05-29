from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import yaml

from pov_generator.application.artifact_contracts import artifact_schema
from pov_generator.application.clarification_service import ClarificationService
from pov_generator.application.context_service import ContextService
from pov_generator.application.domain_pack_selection_service import DomainPackSelectionService
from pov_generator.application.execution_service import ExecutionBundle, ExecutionService
from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.application.validation_service import ValidationService
from pov_generator.application.workflow_service import WorkflowService
from pov_generator.domain.artifacts import ArtifactMetadata, ArtifactRecord
from pov_generator.domain.execution import ExecutionOutput, ExecutionRequest, ExecutionResult
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE_REF = "common.requirements_specification@1.0.0"


def build_services(registry_root: Path | None = None):
    registry_service = RegistryService(FilesystemRegistryLoader(registry_root or REPO_ROOT / "templates"))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)
    context_service = ContextService(runtime)
    execution_service = ExecutionService(runtime, context_service)
    validation_service = ValidationService(runtime, ClarificationService(runtime, provider="stub"))
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


def init_workspace(
    tmp_path: Path,
    domain_packs: tuple[str, ...] = (),
    *,
    registry_root: Path | None = None,
):
    (
        registry_service,
        runtime,
        project_service,
        planning_service,
        context_service,
        execution_service,
        validation_service,
        workflow_service,
    ) = build_services(registry_root)
    snapshot, report = registry_service.validate()
    assert report.is_valid
    packs = tuple(snapshot.resolve_domain_pack(pack_ref) for pack_ref in domain_packs)
    workspace = tmp_path / "case"
    project_service.init_project(
        workspace=workspace,
        name="Demo",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text=(
            "Нужно в рамках PoV подготовить ТЗ для сервиса, который преобразует бизнес-запрос "
            "в структурированные требования."
        ),
        domain_packs=packs,
    )
    planning_service.expand_graph(workspace, snapshot)
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

    while True:
        decision = planning_service.plan(workspace, snapshot, mode="dry-run", record=False)
        assert decision.outcome == "selected"
        if decision.selected_template_ref == "common.requirements_spec_generation@1.0.0":
            task_id = decision.selected_task_id
            break
        result = workflow_service.run_next(workspace, snapshot, provider="stub")
        assert result.validation_status == "passed"

    assert task_id is not None
    context_result = context_service.build_for_task(workspace, snapshot, task_id)
    manifest = context_result.manifest

    assert manifest.template_ref == "common.requirements_spec_generation@1.0.0"
    artifact_titles = {item.title for item in manifest.items if item.item_type == "artifact"}
    assert any("Нормализовать запрос" in title for title in artifact_titles)
    assert any("Определить бизнес-результат" in title for title in artifact_titles)
    assert any("Сформировать варианты решения" in title for title in artifact_titles)
    assert manifest.budget.used_tokens > 0


def _approve_requirements_signoff(runtime: SqliteRuntime, workspace: Path) -> None:
    """Хелпер: после первого `run_until_blocked` находит открытое
    уточнение `client.requirements_signoff@1.0.0` и отвечает на него
    `approved`. Нужен, потому что objective не закроется, пока заказчик
    не согласовал ТЗ через human_approval gate."""
    clarification_service = ClarificationService(runtime, provider="stub")
    target = next(
        req
        for req in runtime.list_clarification_requests(workspace)
        if req.source_type == "quality_gate"
        and req.source_id == "client.requirements_signoff@1.0.0"
        and req.status == "open"
    )
    clarification_service.answer_clarification(
        workspace, request_id=target.request_id, selected_option_ids=("approved",)
    )


def test_stub_workflow_runs_common_objective_end_to_end(tmp_path: Path) -> None:
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

    result = workflow_service.run_until_blocked(workspace, snapshot, provider="stub", max_steps=50)
    # Stub-flow доходит до момента, когда requirements_spec готов и
    # human_approval gate `client.requirements_signoff@1.0.0` блокирует
    # завершение objective: ждём согласования заказчика.
    assert result.stopped_reason == "planner_blocked"

    _approve_requirements_signoff(runtime, workspace)

    result = workflow_service.run_until_blocked(workspace, snapshot, provider="stub", max_steps=5)
    assert result.stopped_reason == "objective_completed"
    artifact_roles = {artifact.artifact_role for artifact in runtime.list_artifacts(workspace)}
    assert {
        "request_fact_sheet",
        "goal_hypothesis",
        "constraint_inventory",
        "ambiguity_gap_report",
        "normalized_request",
        "business_outcome_model",
        "scope_boundary_matrix",
        "stakeholder_map",
        "solution_option_inventory",
        "requirements_spec",
    }.issubset(artifact_roles)
    assert all(run.status == "passed" for run in runtime.list_validation_runs(workspace))


def test_domain_packs_change_task_graph_and_produce_rich_spec(tmp_path: Path) -> None:
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
    ) = init_workspace(
        tmp_path,
        domain_packs=(
            "ml.predictive_analytics@1.0.0",
            "security.enterprise_compliance@1.0.0",
            "integration.enterprise_integration@1.0.0",
            "frontend.web_workspace@1.0.0",
        ),
    )

    result = workflow_service.run_until_blocked(workspace, snapshot, provider="stub", max_steps=50)
    assert result.stopped_reason == "planner_blocked"
    _approve_requirements_signoff(runtime, workspace)
    result = workflow_service.run_until_blocked(workspace, snapshot, provider="stub", max_steps=5)

    assert result.stopped_reason == "objective_completed"
    artifact_roles = {artifact.artifact_role for artifact in runtime.list_artifacts(workspace)}
    assert {
        "predictive_problem_definition",
        "data_landscape_assessment",
        "security_compliance_constraints",
        "integration_operating_model",
        "ui_requirements_outline",
        "requirements_spec",
    }.issubset(artifact_roles)

    spec_artifact = runtime.latest_artifact_by_role(workspace, "requirements_spec")
    assert spec_artifact is not None
    payload = json.loads(runtime.load_artifact_content(workspace, spec_artifact.artifact_id))
    assert payload["ml_requirements"]["prediction_target"]
    assert payload["security_constraints_detail"]["mandatory_controls"]
    assert payload["integration_model"]["delivery_pattern"]
    assert payload["frontend_requirements"]["screens"]


def test_requirements_spec_schema_depends_on_active_domain_packs() -> None:
    base_schema = artifact_schema("requirements_spec", ())
    rich_schema = artifact_schema(
        "requirements_spec",
        (
            "ml.predictive_analytics@1.0.0",
            "security.enterprise_compliance@1.0.0",
            "integration.enterprise_integration@1.0.0",
            "frontend.web_workspace@1.0.0",
        ),
    )

    assert "frontend_requirements" not in base_schema["properties"]
    assert "frontend_requirements" in rich_schema["properties"]
    assert "ml_requirements" in rich_schema["required"]
    assert "security_constraints_detail" in rich_schema["required"]
    assert "integration_model" in rich_schema["required"]


def test_low_confidence_artifact_triggers_blocking_validation(tmp_path: Path) -> None:
    (
        workspace,
        snapshot,
        runtime,
        _project_service,
        _planning_service,
        _context_service,
        _execution_service,
        validation_service,
        _workflow_service,
    ) = init_workspace(tmp_path)
    task = next(task for task in runtime.list_tasks(workspace) if task.template_ref == "common.request_normalization@1.0.0")

    artifact = ArtifactRecord(
        artifact_id=str(uuid.uuid4()),
        project_id=task.project_id,
        artifact_role="normalized_request",
        title="Нормализовать запрос (normalized_request)",
        description="Искусственно созданный артефакт для теста",
        artifact_format="json",
        artifact_kind="primary",
        created_by_task_id=task.task_id,
        storage_path=f"artifacts/{uuid.uuid4()}.json",
        created_at="2026-04-20T00:00:00+00:00",
        metadata=ArtifactMetadata(template_ref=task.template_ref),
    )
    runtime.store_artifact(
        workspace,
        artifact=artifact,
        content=json.dumps(
            {
                "request_summary": "Краткий запрос",
                "business_problem": "Неясно, что именно нужно сделать.",
                "requested_solution_elements": ["Что-то сделать"],
                "explicit_constraints": [],
                "implicit_risks": ["Очень высокая неопределенность"],
                "ambiguous_points": ["Почти все"],
                "confidence": 0.2,
                "blocking_questions": ["Нужна ясная формулировка бизнес-результата."],
            },
            ensure_ascii=False,
        ),
    )

    bundle = ExecutionBundle(
        request=ExecutionRequest(
            execution_run_id=str(uuid.uuid4()),
            project_id=task.project_id,
            task_id=task.task_id,
            template_ref=task.template_ref,
            context_manifest_id="manual-test",
            provider="stub",
            model="stub",
            actor="test",
        ),
        result=ExecutionResult(
            execution_run_id=str(uuid.uuid4()),
            status="succeeded",
            outputs=(ExecutionOutput(artifact_id=artifact.artifact_id, artifact_role="normalized_request"),),
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
    assert any(finding.finding_type == "low_confidence" for finding in validation_run.findings)
    assert any(finding.finding_type == "needs_user_input" for finding in validation_run.findings)


def test_domain_pack_selector_stub_picks_relevant_packs() -> None:
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    snapshot, report = registry_service.validate()
    assert report.is_valid

    selector = DomainPackSelectionService()
    result = selector.select_for_request(
        snapshot,
        objective_ref=OBJECTIVE_REF,
        request_text=(
            "Нужен PoV по предиктивной аналитике оттока на машинном обучении. "
            "Данные берем из 1С и корпоративного портала. "
            "Решение должно работать on-prem, учитывать персональные данные, "
            "обновляться через API и показывать результат в BI и веб-интерфейсе."
        ),
        provider="stub",
    )

    assert result.provider == "stub"
    assert result.selected_pack_refs == (
        "frontend.web_workspace@1.0.0",
        "integration.enterprise_integration@1.0.0",
        "ml.predictive_analytics@1.0.0",
        "security.enterprise_compliance@1.0.0",
    )


def test_context_builder_can_disable_template_budget_via_env(monkeypatch, tmp_path: Path) -> None:
    registry_root = tmp_path / "templates"
    shutil.copytree(REPO_ROOT / "templates", registry_root)

    template_path = registry_root / "tasks" / "common" / "requirements_spec_generation.yaml"
    raw = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    raw["context"]["max_tokens"] = 10
    template_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")

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
    ) = init_workspace(tmp_path, registry_root=registry_root)

    while True:
        decision = planning_service.plan(workspace, snapshot, mode="dry-run", record=False)
        assert decision.outcome == "selected"
        if decision.selected_template_ref == "common.requirements_spec_generation@1.0.0":
            task_id = decision.selected_task_id
            break
        result = workflow_service.run_next(workspace, snapshot, provider="stub")
        assert result.validation_status == "passed"

    assert task_id is not None
    monkeypatch.setenv("POV_DISABLE_TEMPLATE_CONTEXT_BUDGET", "true")
    manifest = context_service.build_for_task(workspace, snapshot, task_id).manifest

    assert manifest.budget.used_tokens > 10
    assert manifest.budget.max_input_tokens == 1_048_576
