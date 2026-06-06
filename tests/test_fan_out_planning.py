# tests/test_fan_out_planning.py
from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml

from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.common.serialization import utc_now_iso
from pov_generator.domain.artifacts import ArtifactMetadata, ArtifactRecord, ArtifactRelations
from pov_generator.domain.registry import ObjectRef
from pov_generator.domain.tasks import TaskRecord
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_ROOT = REPO_ROOT / "templates"


def _build_services(registry_root: Path):
    registry_service = RegistryService(FilesystemRegistryLoader(registry_root))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)
    return registry_service, runtime, project_service, planning_service


def _inject_fan_out_template(templates_root: Path) -> None:
    # Register "test" as a known domain so the registry validator accepts our templates.
    domains_path = templates_root / "vocabularies" / "domains.yaml"
    import yaml as _yaml
    domains_data = _yaml.safe_load(domains_path.read_text(encoding="utf-8"))
    if not any(e["id"] == "test" for e in domains_data["entries"]):
        domains_data["entries"].append({"id": "test", "label": "Test", "description": "Test domain for unit tests."})
        domains_path.write_text(_yaml.dump(domains_data, allow_unicode=True), encoding="utf-8")

    fan_out_dir = templates_root / "tasks" / "test_fan_out"
    fan_out_dir.mkdir(parents=True, exist_ok=True)

    (fan_out_dir / "test_fan_out_child.yaml").write_text(yaml.dump({
        "id": "test.fan_out_child",
        "version": "1.0.0",
        "kind": "task_template",
        "type": "leaf",
        "status": "active",
        "domain": "test",
        "executor": "stub",
        "title": "Process single item",
        "requires": {"artifacts": {"required": [], "optional": []}, "state": [], "readiness": [], "forbidden_open_gaps": [], "domain_packs": []},
        "produces": {"artifact": "test.fan_out_child_output@1.0.0"},
        "effects": {"readiness": {"set": []}, "gaps": {"close": []}},
        "context": {"include": []},
        "planning": {},
        "validation": {},
    }, allow_unicode=True), encoding="utf-8")

    (fan_out_dir / "test_fan_out_wrapper.yaml").write_text(yaml.dump({
        "id": "test.fan_out_wrapper",
        "version": "1.0.0",
        "kind": "task_template",
        "type": "fan_out",
        "status": "active",
        "domain": "test",
        "title": "Fan-out over items",
        "fan_out_spec": {
            "artifact_role": "item_list",
            "array_path": "items",
            "key_field": "id",
            "label_field": "name",
        },
        "children_template_ref": "test.fan_out_child@1.0.0",
        "children": [],
        "slots": [],
        "requires": {"artifacts": {"required": [], "optional": []}, "state": [], "readiness": [], "forbidden_open_gaps": [], "domain_packs": []},
        "produces": {},
        "effects": {"readiness": {"set": []}, "gaps": {"close": []}},
        "context": {"include": []},
        "planning": {},
        "validation": {},
    }, allow_unicode=True), encoding="utf-8")


def _make_fan_out_workspace(tmp_path: Path) -> tuple:
    registry_root = tmp_path / "templates"
    shutil.copytree(TEMPLATES_ROOT, registry_root)
    _inject_fan_out_template(registry_root)
    registry_service, runtime, project_service, planning_service = _build_services(registry_root)
    snapshot, report = registry_service.validate()
    assert report.is_valid, [str(e) for e in report.errors]
    workspace = tmp_path / "ws"
    project_service.init_project(
        workspace=workspace,
        name="Test",
        objective_ref=ObjectRef.parse("common.requirements_specification@1.0.0"),
        request_text="Test request",
        domain_packs=(),
    )
    return workspace, snapshot, runtime, planning_service


def _store_item_list_artifact(runtime: SqliteRuntime, workspace: Path, items: list[dict]) -> None:
    artifact = ArtifactRecord(
        artifact_id="art-test-1",
        project_id="p1",
        artifact_role="item_list",
        title="Item list",
        description=None,
        artifact_format="json",
        artifact_kind="primary",
        created_by_task_id=None,
        storage_path="artifacts/item_list.json",
        created_at=utc_now_iso(),
        relations=ArtifactRelations(),
        metadata=ArtifactMetadata(),
    )
    runtime.store_artifact(workspace, artifact=artifact, content=json.dumps({"items": items}))


def _make_fan_out_task(runtime: SqliteRuntime, workspace: Path, stable_key: str, task_id: str = "fan-out-1") -> TaskRecord:
    now = utc_now_iso()
    return runtime.create_task(workspace, TaskRecord(
        task_id=task_id,
        project_id=runtime.load_manifest(workspace).project_id,
        objective_ref="common.requirements_specification@1.0.0",
        parent_task_id=None,
        template_ref="test.fan_out_wrapper@1.0.0",
        template_type="fan_out",
        title="Fan-out over items",
        status="waiting_for_fan_out_source",
        origin_kind="base_child",
        origin_ref="test",
        stable_key=stable_key,
        depth=1,
        slot_id=None,
        attempt=1,
        error_message=None,
        created_at=now,
        updated_at=now,
    ))


def test_fan_out_node_initial_status(tmp_path: Path):
    from pov_generator.domain.tasks import initial_task_status
    assert initial_task_status("fan_out") == "waiting_for_fan_out_source"


def test_expand_fan_outs_no_artifact_does_nothing(tmp_path: Path):
    workspace, snapshot, runtime, planning_service = _make_fan_out_workspace(tmp_path)
    fan_out_task = _make_fan_out_task(runtime, workspace, "test:fan_out_wrapper")

    planning_service._expand_fan_outs(workspace, snapshot)
    tasks = runtime.list_tasks(workspace)
    wrapper = next(t for t in tasks if t.task_id == fan_out_task.task_id)
    assert wrapper.status == "waiting_for_fan_out_source"
    assert not any(t.parent_task_id == fan_out_task.task_id for t in tasks)


def test_expand_fan_outs_creates_instances_when_artifact_ready(tmp_path: Path):
    workspace, snapshot, runtime, planning_service = _make_fan_out_workspace(tmp_path)
    fan_out_task = _make_fan_out_task(runtime, workspace, "test:fan_out_wrapper2", "fan-out-2")

    _store_item_list_artifact(runtime, workspace, [
        {"id": "item_a", "name": "Item A"},
        {"id": "item_b", "name": "Item B"},
    ])

    planning_service._expand_fan_outs(workspace, snapshot)
    tasks = runtime.list_tasks(workspace)
    instances = [t for t in tasks if t.parent_task_id == fan_out_task.task_id]
    assert len(instances) == 2
    assert all(t.origin_kind == "fan_out_instance" for t in instances)
    wrapper = next(t for t in tasks if t.task_id == fan_out_task.task_id)
    assert wrapper.status == "waiting_for_children"


def test_expand_fan_outs_is_idempotent(tmp_path: Path):
    workspace, snapshot, runtime, planning_service = _make_fan_out_workspace(tmp_path)
    fan_out_task = _make_fan_out_task(runtime, workspace, "test:fan_out_wrapper3", "fan-out-3")
    _store_item_list_artifact(runtime, workspace, [{"id": "x", "name": "X"}])

    planning_service._expand_fan_outs(workspace, snapshot)
    count_first = len([t for t in runtime.list_tasks(workspace) if t.parent_task_id == fan_out_task.task_id])
    planning_service._expand_fan_outs(workspace, snapshot)
    count_second = len([t for t in runtime.list_tasks(workspace) if t.parent_task_id == fan_out_task.task_id])

    assert count_first == count_second == 1


def test_expand_fan_outs_empty_array_completes_wrapper(tmp_path: Path):
    workspace, snapshot, runtime, planning_service = _make_fan_out_workspace(tmp_path)
    fan_out_task = _make_fan_out_task(runtime, workspace, "test:fan_out_wrapper_empty", "fan-out-empty")
    # Store artifact with empty items array
    _store_item_list_artifact(runtime, workspace, [])

    planning_service._expand_fan_outs(workspace, snapshot)
    tasks = runtime.list_tasks(workspace)
    wrapper = next(t for t in tasks if t.task_id == fan_out_task.task_id)
    # Empty array → wrapper should complete immediately (no children to wait for)
    assert wrapper.status == "completed"
    instances = [t for t in tasks if t.parent_task_id == fan_out_task.task_id]
    assert len(instances) == 0


def test_task_graph_view_has_fan_out_meta_for_fan_out_nodes(tmp_path: Path):
    from pov_generator.application.registry_service import RegistryService
    from pov_generator.application.workspace_catalog import WorkspaceCatalog
    from pov_generator.application.workspace_query_service import WorkspaceQueryService
    from pov_generator.domain.workspace_views import FanOutMeta

    registry_root = tmp_path / "templates"
    workspace, snapshot, runtime, planning_service = _make_fan_out_workspace(tmp_path)
    # Создаём fan-out задачу ради побочного эффекта (попадёт в граф); сам
    # объект не нужен — проверяем узлы через query_service ниже.
    _make_fan_out_task(runtime, workspace, "test:fan_out_view", "fan-out-view")

    catalog = WorkspaceCatalog(workspace.parent, runtime)
    registry_service = RegistryService(FilesystemRegistryLoader(registry_root))
    query_service = WorkspaceQueryService(catalog, registry_service, runtime, planning_service)
    tasks = runtime.list_tasks(workspace)
    nodes = query_service._build_task_tree(workspace, tasks, None, snapshot)
    fan_out_nodes = [n for n in nodes if n.template_type == "fan_out"]
    assert len(fan_out_nodes) == 1
    assert fan_out_nodes[0].fan_out_meta is not None
    assert isinstance(fan_out_nodes[0].fan_out_meta, FanOutMeta)
    assert fan_out_nodes[0].fan_out_meta.source_artifact_role == "item_list"
    assert fan_out_nodes[0].fan_out_meta.total_instances == 0
    assert fan_out_nodes[0].fan_out_meta.completed_instances == 0


def test_fan_out_wrapper_completes_when_all_instances_done(tmp_path: Path):
    workspace, snapshot, runtime, planning_service = _make_fan_out_workspace(tmp_path)
    fan_out_task = _make_fan_out_task(runtime, workspace, "test:fan_out_wrapper4", "fan-out-4")
    _store_item_list_artifact(runtime, workspace, [{"id": "c1", "name": "C1"}])
    planning_service._expand_fan_outs(workspace, snapshot)

    tasks = runtime.list_tasks(workspace)
    instance = next(t for t in tasks if t.parent_task_id == fan_out_task.task_id)
    runtime.transition_task(workspace, instance.task_id, "complete")

    planning_service._refresh_composite_completion(workspace)
    tasks = runtime.list_tasks(workspace)
    wrapper = next(t for t in tasks if t.task_id == fan_out_task.task_id)
    assert wrapper.status == "completed"


def test_expand_fan_outs_over_width_limit_fails_wrapper(tmp_path: Path, monkeypatch):
    # Потолок ширины: массив больше лимита → обёртка падает явно, без создания
    # инстансов (защита от разрастания графа).
    monkeypatch.setenv("POV_MAX_FAN_OUT_INSTANCES", "2")
    workspace, snapshot, runtime, planning_service = _make_fan_out_workspace(tmp_path)
    fan_out_task = _make_fan_out_task(runtime, workspace, "test:fan_out_limit", "fan-out-limit")
    _store_item_list_artifact(runtime, workspace, [
        {"id": "a", "name": "A"},
        {"id": "b", "name": "B"},
        {"id": "c", "name": "C"},
    ])

    planning_service._expand_fan_outs(workspace, snapshot)
    tasks = runtime.list_tasks(workspace)
    wrapper = next(t for t in tasks if t.task_id == fan_out_task.task_id)
    assert wrapper.status == "failed"
    assert wrapper.error_message and "POV_MAX_FAN_OUT_INSTANCES" in wrapper.error_message
    instances = [t for t in tasks if t.parent_task_id == fan_out_task.task_id]
    assert len(instances) == 0


def test_reset_fan_out_obsoletes_prior_attempt_instances(tmp_path: Path):
    # После reset (attempt+1) и повторного разворачивания старый незавершённый
    # инстанс прошлой попытки помечается obsolete и НЕ блокирует завершение.
    workspace, snapshot, runtime, planning_service = _make_fan_out_workspace(tmp_path)
    fan_out_task = _make_fan_out_task(runtime, workspace, "test:fan_out_reset", "fan-out-reset")
    _store_item_list_artifact(runtime, workspace, [{"id": "r1", "name": "R1"}])

    planning_service._expand_fan_outs(workspace, snapshot)
    old_instance = next(
        t for t in runtime.list_tasks(workspace) if t.parent_task_id == fan_out_task.task_id
    )
    # Прошлая попытка «провалилась».
    runtime.transition_task(workspace, old_instance.task_id, "fail")

    # Сброс обёртки → attempt 2, статус снова waiting_for_fan_out_source.
    runtime.transition_task(workspace, fan_out_task.task_id, "reset_fan_out")
    planning_service._expand_fan_outs(workspace, snapshot)

    tasks = runtime.list_tasks(workspace)
    old_after = next(t for t in tasks if t.task_id == old_instance.task_id)
    assert old_after.status == "obsolete"  # старый инстанс снят
    fresh = [
        t
        for t in tasks
        if t.parent_task_id == fan_out_task.task_id and t.status != "obsolete"
    ]
    assert len(fresh) == 1  # новый инстанс прошлой попытки

    # Завершаем новый инстанс — обёртка должна завершиться (obsolete не мешает).
    runtime.transition_task(workspace, fresh[0].task_id, "complete")
    planning_service._refresh_composite_completion(workspace)
    wrapper = next(t for t in runtime.list_tasks(workspace) if t.task_id == fan_out_task.task_id)
    assert wrapper.status == "completed"
