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
