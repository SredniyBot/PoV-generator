"""Ф1: граф задач любого гейта (подвкладки графа по гейтам).

Активный гейт отдаёт живой граф (та же форма, что /task-graph) с
``available=True``; ещё не запущенный — статический скелет из реестра с
``available=False``, без записи задач в runtime, fan-out не раскрыт.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from test_m9_api import OBJECTIVE_REF, REPO_ROOT, build_services, init_project

from pov_generator.interfaces.api import create_app

ARCH_REF = "architecture.system_design@1.0.0"


def _flatten(nodes: list[dict]) -> list[dict]:
    out: list[dict] = []
    for node in nodes:
        out.append(node)
        out.extend(_flatten(node.get("children", [])))
    return out


def test_active_objective_graph_matches_live_task_graph(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "case"
    project_id = init_project(workspace, "Запрос для подвкладок графа гейтов.")
    client = TestClient(create_app(repo_root=REPO_ROOT, runtime_root=runtime_root))

    base = client.get(f"/api/projects/{project_id}/task-graph").json()
    resp = client.get(
        f"/api/projects/{project_id}/objectives/task-graph", params={"ref": OBJECTIVE_REF}
    )
    assert resp.status_code == 200
    active = resp.json()
    assert active["objective_state"] == "active"
    assert active["objective_ref"] == OBJECTIVE_REF
    assert active["total_leaf_tasks"] == base["total_leaf_tasks"]
    nodes = _flatten(active["nodes"])
    assert nodes
    assert all(node["available"] for node in nodes)


def test_locked_objective_graph_is_readonly_skeleton(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "case"
    project_id = init_project(workspace, "Запрос для скелета архитектуры.")

    # Число задач до запроса скелета будущего гейта (скелет не должен писать).
    _registry, runtime, *_rest = build_services()
    before = len(runtime.list_tasks(workspace))

    client = TestClient(create_app(repo_root=REPO_ROOT, runtime_root=runtime_root))
    resp = client.get(
        f"/api/projects/{project_id}/objectives/task-graph", params={"ref": ARCH_REF}
    )
    assert resp.status_code == 200
    locked = resp.json()
    assert locked["objective_state"] == "locked"
    assert locked["objective_ref"] == ARCH_REF

    nodes = _flatten(locked["nodes"])
    assert nodes, "скелет будущего гейта не должен быть пустым"
    assert all(not node["available"] for node in nodes), "задачи будущего гейта недоступны"
    # fan-out остаётся нераскрытым — один узел-заглушка без детей.
    for node in nodes:
        if node["template_type"] == "fan_out":
            assert not node["children"]

    # Скелет строится только из реестра — задачи в runtime не появляются.
    after = len(runtime.list_tasks(workspace))
    assert after == before


def test_active_graph_excludes_other_gate_tasks_and_gate_lookup(tmp_path: Path) -> None:
    """П.2a: граф активного гейта не подмешивает задачи прошлого гейта.
    П.2b: резолвер /tasks/{id}/gate возвращает гейт задачи (для дип-линка)."""
    from pov_generator.application.checkpoint_service import CheckpointService
    from pov_generator.domain.registry import ObjectRef

    arch_ref = "architecture.system_design@1.0.0"
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "case"
    project_id = init_project(workspace, "Нужно ТЗ, затем архитектура.")

    registry_service, runtime, project_service, planning_service, workflow_service = build_services()
    snapshot, report = registry_service.validate()
    assert report.is_valid
    # Завершаем ТЗ (+согласование) и переходим на архитектуру.
    r = workflow_service.run_until_blocked(workspace, snapshot, provider="stub", max_steps=50)
    assert r.stopped_reason == "planner_blocked"
    spec = runtime.latest_artifact_by_role(workspace, "requirements_spec")
    CheckpointService(runtime).set_artifact_signed_off(
        workspace, artifact_id=spec.artifact_id, signed_off=True
    )
    workflow_service.run_until_blocked(workspace, snapshot, provider="stub", max_steps=5)
    project_service.activate_next_objective(workspace, ObjectRef.parse(arch_ref))
    planning_service.expand_graph(workspace, snapshot)

    tz_tasks = [
        t
        for t in runtime.list_tasks(workspace)
        if t.objective_ref == OBJECTIVE_REF and t.template_type == "leaf"
    ]
    assert tz_tasks
    tz_id = tz_tasks[0].task_id

    client = TestClient(create_app(repo_root=REPO_ROOT, runtime_root=runtime_root))

    # П.2a: активный граф (архитектура) НЕ содержит задач гейта ТЗ.
    active = client.get(f"/api/projects/{project_id}/task-graph").json()
    assert active["objective_ref"] == arch_ref
    active_ids = {n["task_id"] for n in _flatten(active["nodes"])}
    assert tz_id not in active_ids

    # П.2b: гейт задачи ТЗ определяется корректно.
    gate = client.get(f"/api/projects/{project_id}/tasks/{tz_id}/gate")
    assert gate.status_code == 200
    assert gate.json()["objective_ref"] == OBJECTIVE_REF


def test_unknown_objective_returns_404(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "case"
    project_id = init_project(workspace, "Запрос.")
    client = TestClient(create_app(repo_root=REPO_ROOT, runtime_root=runtime_root))
    resp = client.get(
        f"/api/projects/{project_id}/objectives/task-graph",
        params={"ref": "nonexistent.objective@9.9.9"},
    )
    assert resp.status_code == 404
