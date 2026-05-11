from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
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
    return registry_service, runtime, project_service, planning_service


def init_workspace(tmp_path: Path, domain_packs: tuple[str, ...] = ()):
    registry_service, runtime, project_service, planning_service = build_services()
    snapshot, report = registry_service.validate()
    assert report.is_valid
    packs = tuple(snapshot.resolve_domain_pack(pack_ref) for pack_ref in domain_packs)
    workspace = tmp_path / "case"
    project_service.init_project(
        workspace=workspace,
        name="Demo",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="Нужен сервис для преобразования бизнес-запроса в ТЗ.",
        domain_packs=packs,
    )
    return workspace, snapshot, runtime, project_service, planning_service


def test_registry_validation_passes_for_task_graph_corpus() -> None:
    registry_service, _, _, _ = build_services()
    snapshot, report = registry_service.validate()

    assert report.is_valid
    assert len(snapshot.objectives) == 1
    assert len(snapshot.templates) >= 21
    assert len(snapshot.artifact_contracts) >= 16
    assert len(snapshot.domain_packs) == 4
    assert len(snapshot.methodology_packs) >= 1
    assert len(snapshot.quality_gates) >= 2
    assert len(snapshot.vocabularies) == 5


def test_registry_validation_detects_unknown_domain_slot(tmp_path: Path) -> None:
    registry_root = tmp_path / "templates"
    shutil.copytree(REPO_ROOT / "templates", registry_root)
    pack_path = registry_root / "domains" / "ml" / "predictive_analytics.yaml"
    raw = yaml.safe_load(pack_path.read_text(encoding="utf-8"))
    raw["contributes"][0]["to"] = "unknown.slot"
    pack_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")

    registry_service, _, _, _ = build_services(registry_root)
    _, report = registry_service.validate()

    assert not report.is_valid
    assert any("unknown.slot" in issue.message for issue in report.errors)


def test_problem_state_patches_persist_and_history(tmp_path: Path) -> None:
    workspace, _, _, project_service, _ = init_workspace(tmp_path)

    project_service.set_goal(workspace, "Подготовить качественное ТЗ.")
    project_service.set_readiness(workspace, "request_normalized", "ready", blocking=False, confidence=0.95)
    project_service.add_gap(workspace, "missing_kpi", "Нет KPI", "Не указан измеримый эффект.", "medium", True)
    project_service.close_gap(workspace, "missing_kpi")
    state = project_service.load_problem_state(workspace)
    history = project_service.problem_history(workspace)

    assert state.goal == "Подготовить качественное ТЗ."
    assert state.readiness["request_normalized"].status == "ready"
    assert "missing_kpi" not in state.active_gaps
    assert len(history) >= 5


def test_planner_expands_objective_into_hierarchical_task_graph(tmp_path: Path) -> None:
    workspace, snapshot, runtime, project_service, planning_service = init_workspace(tmp_path)

    planning_service.expand_graph(workspace, snapshot)
    state = project_service.load_problem_state(workspace)
    tasks = runtime.list_tasks(workspace)

    assert state.root_task_id is not None
    assert len(tasks) == 16
    assert any(task.template_type == "composite" and task.title == "Разобрать исходный бизнес-запрос" for task in tasks)
    assert any(task.template_type == "leaf" and task.title == "Выделить факты из запроса" for task in tasks)

    decision = planning_service.plan(workspace, snapshot, mode="dry-run")
    assert decision.outcome == "selected"
    assert decision.selected_task_key == "common.request_fact_extraction@1.0.0"


def test_domain_pack_contributes_tasks_into_configured_slots(tmp_path: Path) -> None:
    workspace, snapshot, runtime, _, planning_service = init_workspace(
        tmp_path,
        domain_packs=(
            "ml.predictive_analytics@1.0.0",
            "frontend.web_workspace@1.0.0",
        ),
    )

    planning_service.expand_graph(workspace, snapshot)
    tasks = runtime.list_tasks(workspace)

    assert any(
        task.template_ref == "ml.predictive_problem_definition@1.0.0"
        and task.origin_kind == "domain_contribution"
        and task.slot_id == "solution.evaluation"
        for task in tasks
    )
    assert any(
        task.template_ref == "frontend.user_flow_analysis@1.0.0"
        and task.origin_kind == "domain_contribution"
        and task.slot_id == "spec.domain_sections"
        for task in tasks
    )


def test_graph_expansion_is_idempotent(tmp_path: Path) -> None:
    workspace, snapshot, runtime, _, planning_service = init_workspace(
        tmp_path,
        domain_packs=("security.enterprise_compliance@1.0.0",),
    )

    planning_service.expand_graph(workspace, snapshot)
    first_count = len(runtime.list_tasks(workspace))
    planning_service.expand_graph(workspace, snapshot)
    second_count = len(runtime.list_tasks(workspace))

    assert first_count == second_count


def test_methodology_pack_is_registered_with_stages() -> None:
    registry_service, _, _, _ = build_services()
    snapshot, report = registry_service.validate()

    assert report.is_valid
    pack = snapshot.resolve_methodology_pack("process.lean_jtbd@1.0.0")
    assert pack.stage_execution_mode == "single_call"
    stage_ids = tuple(stage.identifier for stage in pack.stages)
    assert {"goal_framing", "decision"}.issubset(set(stage_ids))
    assert "goal_framing" in pack.reasoning_artifact.required_stages
    trivial_stages = pack.stages_for_complexity("trivial")
    assert all(stage.identifier != "option_generation" for stage in trivial_stages)


def test_quality_gate_normalizes_legacy_check_type() -> None:
    registry_service, _, _, _ = build_services()
    snapshot, report = registry_service.validate()

    assert report.is_valid
    gate = snapshot.resolve_quality_gate("common.requirements_spec_review_passed@1.0.0")
    assert gate.check_type == "automated_review"


def test_default_methodology_is_activated_on_project_init(tmp_path: Path) -> None:
    workspace, _, _, project_service, _ = init_workspace(tmp_path)
    state = project_service.load_problem_state(workspace)
    active = state.active_methodology_pack_records
    assert "process.lean_jtbd@1.0.0" in active
    assert active["process.lean_jtbd@1.0.0"].source == "bootstrap"


def test_set_methodology_keeps_active_pack(tmp_path: Path) -> None:
    workspace, _, _, project_service, _ = init_workspace(tmp_path)
    state = project_service.set_methodology(workspace, "process.lean_jtbd@1.0.0")
    assert "process.lean_jtbd@1.0.0" in state.active_methodology_pack_records


def test_execution_emits_reasoning_and_methodology_trace_artifacts(tmp_path: Path) -> None:
    from pov_generator.application.context_service import ContextService
    from pov_generator.application.execution_service import ExecutionService

    workspace, snapshot, runtime, _, planning_service = init_workspace(tmp_path)
    planning_service.expand_graph(workspace, snapshot)
    decision = planning_service.plan(workspace, snapshot, mode="dry-run")
    task_id = decision.selected_task_id
    assert task_id is not None

    context_service = ContextService(runtime)
    execution_service = ExecutionService(runtime, context_service)
    bundle = execution_service.execute_task(workspace, snapshot, task_id, provider="stub")

    output_kinds = {output.kind for output in bundle.result.outputs}
    assert output_kinds == {"primary", "reasoning", "trace"}
    assert bundle.request.methodology_pack_ref == "process.lean_jtbd@1.0.0"

    artifacts = list(runtime.list_artifacts(workspace))
    kinds = {artifact.artifact_kind for artifact in artifacts}
    assert {"primary", "reasoning", "trace"}.issubset(kinds)


def test_project_overview_exposes_methodology_and_progress(tmp_path: Path) -> None:
    from pov_generator.application.clarification_service import ClarificationService
    from pov_generator.application.context_service import ContextService
    from pov_generator.application.execution_service import ExecutionService
    from pov_generator.application.validation_service import ValidationService
    from pov_generator.application.workflow_service import WorkflowService
    from pov_generator.application.workspace_catalog import WorkspaceCatalog
    from pov_generator.application.workspace_query_service import WorkspaceQueryService

    workspace, snapshot, runtime, project_service, planning_service = init_workspace(tmp_path)
    context = ContextService(runtime)
    execution = ExecutionService(runtime, context)
    cl = ClarificationService(runtime)
    val = ValidationService(runtime, cl)
    wf = WorkflowService(runtime, planning_service, execution, val)
    wf.run_until_blocked(workspace, snapshot, provider="stub", max_steps=2)

    catalog = WorkspaceCatalog(workspace.parent, runtime)
    qs = WorkspaceQueryService(catalog, RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates")), runtime, planning_service)
    pid = runtime.load_manifest(workspace).project_id
    overview = qs.project_overview(pid)
    assert overview.active_methodology == "process.lean_jtbd@1.0.0"
    assert overview.objective_progress.artifacts_required >= 1
    assert isinstance(overview.stage_summary, str) and overview.stage_summary

    methodologies = qs.list_methodology_packs()
    assert any(m["pack_ref"] == "process.lean_jtbd@1.0.0" for m in methodologies)


def test_task_methodology_trace_returns_execution_summary_for_provenance(tmp_path: Path) -> None:
    """W2.3: methodology-trace должен возвращать execution_run_id /
    provider / model / context_manifest_id, чтобы UI L4 ProvenanceViewer
    мог показать «откуда это» без отдельного запроса к /debug."""
    from pov_generator.application.clarification_service import ClarificationService
    from pov_generator.application.context_service import ContextService
    from pov_generator.application.execution_service import ExecutionService
    from pov_generator.application.validation_service import ValidationService
    from pov_generator.application.workflow_service import WorkflowService
    from pov_generator.application.workspace_catalog import WorkspaceCatalog
    from pov_generator.application.workspace_query_service import WorkspaceQueryService

    workspace, snapshot, runtime, _, planning_service = init_workspace(tmp_path)
    context = ContextService(runtime)
    execution = ExecutionService(runtime, context)
    cl = ClarificationService(runtime)
    val = ValidationService(runtime, cl)
    wf = WorkflowService(runtime, planning_service, execution, val)
    wf.run_until_blocked(workspace, snapshot, provider="stub", max_steps=2)

    catalog = WorkspaceCatalog(workspace.parent, runtime)
    qs = WorkspaceQueryService(
        catalog, RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates")), runtime, planning_service
    )
    pid = runtime.load_manifest(workspace).project_id
    completed = next(t for t in runtime.list_tasks(workspace) if t.template_type == "leaf" and t.status == "completed")

    trace = qs.task_methodology_trace(pid, completed.task_id)
    assert trace["execution"] is not None
    execution_summary = trace["execution"]
    assert execution_summary["execution_run_id"]
    assert execution_summary["provider"] == "stub"
    assert execution_summary["context_manifest_id"]
    assert execution_summary["status"] == "succeeded"


def test_task_methodology_trace_returns_reasoning_and_trace(tmp_path: Path) -> None:
    from pov_generator.application.clarification_service import ClarificationService
    from pov_generator.application.context_service import ContextService
    from pov_generator.application.execution_service import ExecutionService
    from pov_generator.application.validation_service import ValidationService
    from pov_generator.application.workflow_service import WorkflowService
    from pov_generator.application.workspace_catalog import WorkspaceCatalog
    from pov_generator.application.workspace_query_service import WorkspaceQueryService

    workspace, snapshot, runtime, _, planning_service = init_workspace(tmp_path)
    context = ContextService(runtime)
    execution = ExecutionService(runtime, context)
    cl = ClarificationService(runtime)
    val = ValidationService(runtime, cl)
    wf = WorkflowService(runtime, planning_service, execution, val)
    wf.run_until_blocked(workspace, snapshot, provider="stub", max_steps=2)

    catalog = WorkspaceCatalog(workspace.parent, runtime)
    qs = WorkspaceQueryService(catalog, RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates")), runtime, planning_service)
    pid = runtime.load_manifest(workspace).project_id
    completed = next(t for t in runtime.list_tasks(workspace) if t.template_type == "leaf" and t.status == "completed")
    trace = qs.task_methodology_trace(pid, completed.task_id)
    assert trace["trace"] is not None
    assert trace["reasoning"] is not None
