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
    assert len(snapshot.objectives) >= 1
    assert len(snapshot.templates) >= 21
    assert len(snapshot.artifact_contracts) >= 16
    assert len(snapshot.domain_packs) == 4
    assert len(snapshot.methodology_packs) >= 1
    assert len(snapshot.quality_gates) >= 1
    assert len(snapshot.vocabularies) == 6
    assert len(snapshot.agent_capabilities) >= 5


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
    state = project_service.load_project_state(workspace)
    history = project_service.state_history(workspace)

    assert state.knowledge.goal_statement() == "Подготовить качественное ТЗ."
    assert state.process.readiness["request_normalized"].status == "ready"
    assert "missing_kpi" not in state.process.active_gaps
    assert len(history) >= 5


def test_planner_expands_objective_into_hierarchical_task_graph(tmp_path: Path) -> None:
    workspace, snapshot, runtime, project_service, planning_service = init_workspace(tmp_path)

    planning_service.expand_graph(workspace, snapshot)
    state = project_service.load_project_state(workspace)
    tasks = runtime.list_tasks(workspace)

    assert state.process.root_task_id is not None
    # Обновлено: после Phase 3 число задач было 23; после удаления LLM-ревью
    # (вариант B — убраны композит review_requirements_spec + leaf
    # requirements_spec_review) стало 21. Затем +1 leaf
    # common.feasibility_assessment (оценка реализуемости частей проекта) →
    # 22. Privacy_impact_assessment живёт в security-домене и появляется
    # только когда активен security pack.
    assert len(tasks) == 22
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


def test_default_methodology_is_activated_on_project_init(tmp_path: Path) -> None:
    workspace, _, _, project_service, _ = init_workspace(tmp_path)
    state = project_service.load_project_state(workspace)
    active = state.process.active_methodology_pack_records
    assert "process.lean_jtbd@1.0.0" in active
    assert active["process.lean_jtbd@1.0.0"].source == "bootstrap"


def test_objectives_declare_default_methodology_in_yaml() -> None:
    """Оба objective'а декларируют ``default_methodology_pack`` в YAML.
    Поле читается верхним слоем (CLI / workspace_command_service) и
    передаётся в init_project. Архитектура использует тот же lean_jtbd, что
    и ТЗ — архитектурные решения тоже выбор из вариантов."""
    registry_service, _, _, _ = build_services()
    snapshot, report = registry_service.validate()
    assert report.is_valid

    arch_spec = snapshot.resolve_objective(ObjectRef.parse("architecture.system_design@1.0.0"))
    assert arch_spec.default_methodology_pack_ref is not None
    assert (
        arch_spec.default_methodology_pack_ref.as_string()
        == "process.lean_jtbd@1.0.0"
    )

    tz_spec = snapshot.resolve_objective(ObjectRef.parse("common.requirements_specification@1.0.0"))
    assert tz_spec.default_methodology_pack_ref is not None
    assert (
        tz_spec.default_methodology_pack_ref.as_string()
        == "process.lean_jtbd@1.0.0"
    )


def test_architecture_objective_activates_methodology_from_yaml(tmp_path: Path) -> None:
    """init_project активирует ту методологию, которую ему передал caller.
    Раньше дефолт выбирался по префиксу id; теперь — caller сам читает
    ``ObjectiveSpec.default_methodology_pack_ref`` из реестра."""
    registry_service, _, project_service, _ = build_services()
    snapshot, report = registry_service.validate()
    assert report.is_valid

    arch_spec = snapshot.resolve_objective(ObjectRef.parse("architecture.system_design@1.0.0"))
    assert arch_spec.default_methodology_pack_ref is not None
    workspace = tmp_path / "arch"
    project_service.init_project(
        workspace=workspace,
        name="Arch",
        objective_ref=arch_spec.ref,
        request_text="Описать архитектуру сервиса.",
        default_methodology_pack_ref=arch_spec.default_methodology_pack_ref.as_string(),
    )
    state = project_service.load_project_state(workspace)
    active = state.process.active_methodology_pack_records
    assert "process.lean_jtbd@1.0.0" in active
    assert "process.descriptive_decomposition@1.0.0" not in active


def test_parse_objective_rejects_empty_default_methodology_pack(
    tmp_path: Path,
) -> None:
    """parse_objective падает, если ``default_methodology_pack`` есть, но пустой.
    Полное отсутствие поля допустимо — это просто fallback."""
    import shutil as _shutil

    from pov_generator.common.errors import ValidationError

    registry_root = tmp_path / "templates"
    _shutil.copytree(REPO_ROOT / "templates", registry_root)
    obj_path = registry_root / "objectives" / "architecture" / "system_design.yaml"
    raw = yaml.safe_load(obj_path.read_text(encoding="utf-8"))
    raw["default_methodology_pack"] = "   "
    obj_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    registry_service, _, _, _ = build_services(registry_root)
    raised = False
    try:
        registry_service.validate()
    except ValidationError:
        raised = True
    assert raised


def test_init_project_falls_back_to_lean_jtbd_when_no_methodology_passed(
    tmp_path: Path,
) -> None:
    """Если caller не передал ``default_methodology_pack_ref``, активируется
    fallback ``process.lean_jtbd@1.0.0`` (обратная совместимость)."""
    _, _, project_service, _ = build_services()
    workspace = tmp_path / "fallback"
    project_service.init_project(
        workspace=workspace,
        name="Fallback case",
        objective_ref=ObjectRef.parse("common.requirements_specification@1.0.0"),
        request_text="Минимальный запрос.",
    )
    state = project_service.load_project_state(workspace)
    active = state.process.active_methodology_pack_records
    assert "process.lean_jtbd@1.0.0" in active


def test_objective_compatible_next_objectives_defaults_to_empty() -> None:
    """Поле ``compatible_next_objectives`` опционально; отсутствие в YAML
    даёт пустой tuple."""
    registry_service, _, _, _ = build_services()
    snapshot, report = registry_service.validate()
    assert report.is_valid

    # implementation — конечный objective цепочки: compatible_next_objectives
    # объявлен пустым списком, что даёт пустой tuple.
    impl_spec = snapshot.resolve_objective(ObjectRef.parse("implementation.build_plan@1.0.0"))
    assert impl_spec.compatible_next_objectives == ()


def test_parse_objective_rejects_unknown_compatible_next_objective(
    tmp_path: Path,
) -> None:
    """Если ``compatible_next_objectives`` ссылается на несуществующий
    objective — валидация реестра падает."""
    import shutil as _shutil

    registry_root = tmp_path / "templates"
    _shutil.copytree(REPO_ROOT / "templates", registry_root)
    obj_path = registry_root / "objectives" / "common" / "requirements_specification.yaml"
    raw = yaml.safe_load(obj_path.read_text(encoding="utf-8"))
    raw["compatible_next_objectives"] = ["nonexistent.objective@1.0.0"]
    obj_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    registry_service, _, _, _ = build_services(registry_root)
    _, report = registry_service.validate()
    assert not report.is_valid
    messages = [issue.message for issue in report.errors]
    assert any("nonexistent.objective@1.0.0" in msg for msg in messages)


def test_parse_objective_rejects_self_reference_in_compatible_next(
    tmp_path: Path,
) -> None:
    """Самореференция в ``compatible_next_objectives`` запрещена."""
    import shutil as _shutil

    registry_root = tmp_path / "templates"
    _shutil.copytree(REPO_ROOT / "templates", registry_root)
    obj_path = registry_root / "objectives" / "common" / "requirements_specification.yaml"
    raw = yaml.safe_load(obj_path.read_text(encoding="utf-8"))
    raw["compatible_next_objectives"] = ["common.requirements_specification@1.0.0"]
    obj_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    registry_service, _, _, _ = build_services(registry_root)
    _, report = registry_service.validate()
    assert not report.is_valid


def test_parse_objective_accepts_valid_compatible_next_objective(
    tmp_path: Path,
) -> None:
    """Корректная ссылка на существующий objective проходит валидацию и
    подхватывается в ``ObjectiveSpec``."""
    import shutil as _shutil

    registry_root = tmp_path / "templates"
    _shutil.copytree(REPO_ROOT / "templates", registry_root)
    obj_path = registry_root / "objectives" / "common" / "requirements_specification.yaml"
    raw = yaml.safe_load(obj_path.read_text(encoding="utf-8"))
    raw["compatible_next_objectives"] = ["architecture.system_design@1.0.0"]
    obj_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    registry_service, _, _, _ = build_services(registry_root)
    snapshot, report = registry_service.validate()
    assert report.is_valid

    spec = snapshot.resolve_objective(
        ObjectRef.parse("common.requirements_specification@1.0.0")
    )
    assert len(spec.compatible_next_objectives) == 1
    assert (
        spec.compatible_next_objectives[0].as_string()
        == "architecture.system_design@1.0.0"
    )


def test_activate_next_objective_switches_active_ref_and_appends_history(
    tmp_path: Path,
) -> None:
    """``activate_next_objective`` переключает active objective и
    переносит предыдущий в ``objective_history``."""
    workspace, _, _, project_service, _ = init_workspace(tmp_path)
    old_manifest = project_service.load_manifest(workspace)
    assert old_manifest.objective_ref == "common.requirements_specification@1.0.0"
    assert old_manifest.objective_history == ()

    new_manifest = project_service.activate_next_objective(
        workspace,
        ObjectRef.parse("architecture.system_design@1.0.0"),
        default_methodology_pack_ref="process.lean_jtbd@1.0.0",
    )
    assert new_manifest.objective_ref == "architecture.system_design@1.0.0"
    assert new_manifest.objective_history == (
        "common.requirements_specification@1.0.0",
    )

    reloaded = project_service.load_manifest(workspace)
    assert reloaded.objective_ref == "architecture.system_design@1.0.0"
    assert reloaded.objective_history == (
        "common.requirements_specification@1.0.0",
    )


def test_activate_next_objective_rejects_same_objective(
    tmp_path: Path,
) -> None:
    """Активация того же objective'а, что уже активен, отклоняется."""
    workspace, _, _, project_service, _ = init_workspace(tmp_path)
    raised = False
    try:
        project_service.activate_next_objective(
            workspace,
            ObjectRef.parse("common.requirements_specification@1.0.0"),
        )
    except ValueError:
        raised = True
    assert raised


def test_activate_next_objective_keeps_methodology_when_same_default(
    tmp_path: Path,
) -> None:
    """Если новая методология совпадает с уже активной, событий смены
    методологии в state log не появляется."""
    workspace, _, _, project_service, _ = init_workspace(tmp_path)
    before_events = len(project_service.state_history(workspace))

    project_service.activate_next_objective(
        workspace,
        ObjectRef.parse("architecture.system_design@1.0.0"),
        default_methodology_pack_ref="process.lean_jtbd@1.0.0",
    )

    after_events = len(project_service.state_history(workspace))
    # Никаких новых событий процесса — обе методологии одинаковы.
    assert after_events == before_events


def test_activate_next_objective_preserves_knowledge_and_process(
    tmp_path: Path,
) -> None:
    """Состояние (положения, цель, gaps, readiness) сохраняется при
    смене objective'а — это и есть смысл chain-ов."""
    workspace, _, _, project_service, _ = init_workspace(tmp_path)
    project_service.set_goal(workspace, "Достичь цели X.")
    knowledge_before = project_service.load_project_state(workspace).knowledge
    goal_before = knowledge_before.positions["project.goal"].statement

    project_service.activate_next_objective(
        workspace,
        ObjectRef.parse("architecture.system_design@1.0.0"),
    )

    knowledge_after = project_service.load_project_state(workspace).knowledge
    assert knowledge_after.positions["project.goal"].statement == goal_before


def test_chain_expand_graph_creates_new_root_for_next_objective(
    tmp_path: Path,
) -> None:
    """После activate_next_objective + expand_graph в БД появляется
    второй root-таск для нового objective'а, а старый сохраняется.

    Это интеграционный тест механики chain'а: backend корректно создаёт
    новое дерево задач рядом со старым, артефакты ТЗ становятся доступны
    архитектурным задачам через ``requires.artifacts.optional``."""
    registry_service, runtime, project_service, planning_service = build_services()
    snapshot, _ = registry_service.validate()
    workspace = tmp_path / "chain"

    # 1. Создаём TZ workspace + expand_graph
    project_service.init_project(
        workspace=workspace,
        name="Chain demo",
        objective_ref=ObjectRef.parse("common.requirements_specification@1.0.0"),
        request_text="Нужен сервис, генерирующий ТЗ.",
    )
    planning_service.expand_graph(workspace, snapshot)
    tasks_after_tz = runtime.list_tasks(workspace)
    tz_root_count = sum(
        1
        for t in tasks_after_tz
        if t.origin_kind == "objective_root"
        and "requirements_specification" in t.origin_ref
    )
    assert tz_root_count == 1

    # 2. Активируем следующий objective + expand_graph
    project_service.activate_next_objective(
        workspace,
        ObjectRef.parse("architecture.system_design@1.0.0"),
    )
    planning_service.expand_graph(workspace, snapshot)
    tasks_after_arch = runtime.list_tasks(workspace)

    arch_root_count = sum(
        1
        for t in tasks_after_arch
        if t.origin_kind == "objective_root"
        and "system_design" in t.origin_ref
    )
    tz_root_count_still = sum(
        1
        for t in tasks_after_arch
        if t.origin_kind == "objective_root"
        and "requirements_specification" in t.origin_ref
    )
    # Оба root'а живут в одной БД задач.
    assert arch_root_count == 1
    assert tz_root_count_still == 1
    # Manifest показывает arch как активный, TZ в истории.
    final_manifest = project_service.load_manifest(workspace)
    assert final_manifest.objective_ref == "architecture.system_design@1.0.0"
    assert final_manifest.objective_history == (
        "common.requirements_specification@1.0.0",
    )


def test_set_methodology_keeps_active_pack(tmp_path: Path) -> None:
    workspace, _, _, project_service, _ = init_workspace(tmp_path)
    process = project_service.set_methodology(workspace, "process.lean_jtbd@1.0.0")
    assert "process.lean_jtbd@1.0.0" in process.active_methodology_pack_records


def test_execution_emits_primary_artifact_with_reasoning_and_trace_metadata(
    tmp_path: Path,
) -> None:
    """Этап 1.1: на одно исполнение leaf-задачи — один primary артефакт.

    Reasoning и methodology trace живут в его :class:`ArtifactMetadata`,
    а не как отдельные ``ArtifactRecord`` объекты.
    """
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

    # Только один output — primary артефакт.
    output_kinds = {output.kind for output in bundle.result.outputs}
    assert output_kinds == {"primary"}
    assert bundle.request.methodology_pack_ref == "process.lean_jtbd@1.0.0"

    # И только primary артефакт в реестре, без отдельных reasoning/trace.
    artifacts = list(runtime.list_artifacts(workspace))
    kinds = {artifact.artifact_kind for artifact in artifacts}
    assert kinds == {"primary"}

    # Reasoning и trace доступны через метаинформацию primary артефакта.
    primary = next(a for a in artifacts if a.created_by_task_id == task_id)
    assert primary.metadata.methodology_pack_ref == "process.lean_jtbd@1.0.0"
    assert "stages" in primary.metadata.reasoning
    assert "stages_executed" in primary.metadata.methodology_trace


def test_methodology_reasoning_applied_decisions_come_from_ledger_not_legacy_knowledge(
    tmp_path: Path,
) -> None:
    from pov_generator.application.context_service import ContextService
    from pov_generator.application.execution_service import ExecutionService
    from pov_generator.domain.decisions import Decision, DecisionAlternative
    from pov_generator.domain.positions import Position
    from pov_generator.domain.project_knowledge import UpsertPositionPatch

    workspace, snapshot, runtime, _, planning_service = init_workspace(tmp_path)
    planning_service.expand_graph(workspace, snapshot)
    decision = planning_service.plan(workspace, snapshot, mode="dry-run")
    task_id = decision.selected_task_id
    assert task_id is not None
    project_id = runtime.load_manifest(workspace).project_id

    runtime.apply_knowledge_patch(
        workspace,
        UpsertPositionPatch(
            position=Position(
                identifier="legacy.decision.trace-test",
                type="decision",
                statement="Legacy Layer A decision must not appear in reasoning trace",
                visibility="principal",
                scope="global",
                source="user",
                taken_by="test",
                taken_at="2026-05-27T00:00:00Z",
            )
        ),
        actor="test",
        reason="seed legacy decision knowledge",
    )
    runtime.upsert_decision(
        workspace,
        Decision(
            decision_id="ledger-applied",
            project_id=project_id,
            title="Граница MVP",
            description="Что включить в первый релиз",
            chosen_option_id="narrow",
            alternatives=(
                DecisionAlternative(option_id="narrow", label="Узкий MVP"),
                DecisionAlternative(option_id="wide", label="Широкий MVP"),
            ),
            rationale="Узкий MVP снижает риск поставки",
            level="business",
            level_rationale="Меняет scope поставки",
            confidence=0.8,
            status="accepted_default",
            source="identification",
            source_task_id="task-a",
            category="scope",
        ),
    )

    execution_service = ExecutionService(runtime, ContextService(runtime))
    execution_service.execute_task(workspace, snapshot, task_id, provider="stub")

    primary = next(
        artifact
        for artifact in runtime.list_artifacts(workspace)
        if artifact.created_by_task_id == task_id
    )
    applied = primary.metadata.reasoning["applied_decisions"]
    assert [item["decision_id"] for item in applied] == ["ledger-applied"]
    assert applied[0]["title"] == "Граница MVP"
    assert applied[0]["category"] == "scope"
    assert all(item["decision_id"] != "legacy.decision.trace-test" for item in applied)


def test_project_overview_exposes_methodology_and_progress(tmp_path: Path) -> None:
    from pov_generator.application.checkpoint_service import CheckpointService
    from pov_generator.application.context_service import ContextService
    from pov_generator.application.execution_service import ExecutionService
    from pov_generator.application.validation_service import ValidationService
    from pov_generator.application.workflow_service import WorkflowService
    from pov_generator.application.workspace_catalog import WorkspaceCatalog
    from pov_generator.application.workspace_query_service import WorkspaceQueryService

    workspace, snapshot, runtime, project_service, planning_service = init_workspace(tmp_path)
    context = ContextService(runtime)
    execution = ExecutionService(runtime, context)
    cl = CheckpointService(runtime)
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
    from pov_generator.application.checkpoint_service import CheckpointService
    from pov_generator.application.context_service import ContextService
    from pov_generator.application.execution_service import ExecutionService
    from pov_generator.application.validation_service import ValidationService
    from pov_generator.application.workflow_service import WorkflowService
    from pov_generator.application.workspace_catalog import WorkspaceCatalog
    from pov_generator.application.workspace_query_service import WorkspaceQueryService

    workspace, snapshot, runtime, _, planning_service = init_workspace(tmp_path)
    context = ContextService(runtime)
    execution = ExecutionService(runtime, context)
    cl = CheckpointService(runtime)
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
    from pov_generator.application.checkpoint_service import CheckpointService
    from pov_generator.application.context_service import ContextService
    from pov_generator.application.execution_service import ExecutionService
    from pov_generator.application.validation_service import ValidationService
    from pov_generator.application.workflow_service import WorkflowService
    from pov_generator.application.workspace_catalog import WorkspaceCatalog
    from pov_generator.application.workspace_query_service import WorkspaceQueryService

    workspace, snapshot, runtime, _, planning_service = init_workspace(tmp_path)
    context = ContextService(runtime)
    execution = ExecutionService(runtime, context)
    cl = CheckpointService(runtime)
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
