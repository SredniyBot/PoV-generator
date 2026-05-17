"""Тесты REST API checkpoint-сессий (v3.0).

Покрывают:
- GET /api/projects/{id}/checkpoints — список + pending_count
- GET /api/projects/{id}/checkpoints/{session_id} — детали с decisions
- POST /api/projects/{id}/checkpoints/{session_id}/answer — submit
- 404 на чужой проект и несуществующую сессию
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from test_m9_api import init_project  # type: ignore

from pov_generator.application.checkpoint_service import CheckpointService
from pov_generator.domain.decisions import Decision, DecisionAlternative
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime
from pov_generator.interfaces.api import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]


def _alt(option_id: str, label: str) -> DecisionAlternative:
    return DecisionAlternative(
        option_id=option_id, label=label, description=f"desc {option_id}"
    )


def _make_decision(
    *, decision_id: str, project_id: str, level: str = "business"
) -> Decision:
    return Decision(
        decision_id=decision_id,
        project_id=project_id,
        title=f"Decision {decision_id}",
        description="x",
        chosen_option_id="opt-a",
        alternatives=(_alt("opt-a", "Option A"), _alt("opt-b", "Option B")),
        rationale="x",
        level=level,  # type: ignore[arg-type]
        level_rationale="x",
        confidence=0.8,
        status="proposed",
        source="pre_flight",
        source_task_id="task-1",
    )


def _build_app_with_session(tmp_path: Path) -> tuple[TestClient, str, str, Path]:
    """Создать app + проект + checkpoint-сессию с 2 business-decisions
    под mode=balanced."""
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "case_cp"
    project_id = init_project(workspace, "Bootstrap for checkpoint tests.")

    runtime = SqliteRuntime()
    svc = CheckpointService(runtime)
    decisions = (
        _make_decision(decision_id="d-1", project_id=project_id),
        _make_decision(decision_id="d-2", project_id=project_id),
    )
    result = svc.process_planned_decisions(
        workspace,
        project_id=project_id,
        task_id="task-1",
        task_title="Pilot task",
        artifact_role="requirements_spec",
        decisions=decisions,
        mode="balanced",
    )
    assert result.session is not None

    app = create_app(repo_root=REPO_ROOT, runtime_root=runtime_root, websocket_poll_interval=0.05)
    client = TestClient(app)
    return client, project_id, result.session.session_id, workspace


# ---------------------------------------------------------------------------
# GET list
# ---------------------------------------------------------------------------


def test_list_returns_pending_session_with_decisions(tmp_path: Path) -> None:
    client, project_id, session_id, _ws = _build_app_with_session(tmp_path)
    response = client.get(f"/api/projects/{project_id}/checkpoints")
    assert response.status_code == 200
    body = response.json()
    assert body["pending_count"] == 1
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["session_id"] == session_id
    assert item["status"] == "pending"
    assert item["task_title"] == "Pilot task"
    assert item["artifact_role"] == "requirements_spec"
    assert len(item["decisions"]) == 2
    # Решения в развёрнутом виде, с chosen_option_label
    by_id = {d["decision_id"]: d for d in item["decisions"]}
    assert "d-1" in by_id and "d-2" in by_id
    assert by_id["d-1"]["chosen_option_label"] == "Option A"


def test_list_returns_empty_when_no_sessions(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "case"
    project_id = init_project(workspace, "Empty.")
    app = create_app(repo_root=REPO_ROOT, runtime_root=runtime_root, websocket_poll_interval=0.05)
    client = TestClient(app)
    body = client.get(f"/api/projects/{project_id}/checkpoints").json()
    assert body["pending_count"] == 0
    assert body["items"] == []


# ---------------------------------------------------------------------------
# GET detail
# ---------------------------------------------------------------------------


def test_detail_endpoint_returns_full_session(tmp_path: Path) -> None:
    client, project_id, session_id, _ws = _build_app_with_session(tmp_path)
    response = client.get(f"/api/projects/{project_id}/checkpoints/{session_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == session_id
    assert body["status"] == "pending"
    assert len(body["decisions"]) == 2


def test_detail_endpoint_404_when_session_missing(tmp_path: Path) -> None:
    client, project_id, _sid, _ws = _build_app_with_session(tmp_path)
    response = client.get(f"/api/projects/{project_id}/checkpoints/non-existent")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST answer
# ---------------------------------------------------------------------------


def test_answer_endpoint_finalizes_session(tmp_path: Path) -> None:
    """Полный happy path: пользователь ответил на оба decision — сессия
    финализирована, в ответе уже финальный view."""
    client, project_id, session_id, _ws = _build_app_with_session(tmp_path)
    body = {
        "answers": [
            {"decision_id": "d-1", "kind": "accept_default"},
            {"decision_id": "d-2", "kind": "select_alternative", "selected_option_id": "opt-b"},
        ]
    }
    response = client.post(
        f"/api/projects/{project_id}/checkpoints/{session_id}/answer", json=body
    )
    assert response.status_code == 200
    final = response.json()
    assert final["status"] == "finalized"
    assert final["finalized_by"] == "user"
    by_id = {d["decision_id"]: d for d in final["decisions"]}
    assert by_id["d-1"]["status"] == "accepted_default"
    assert by_id["d-2"]["status"] == "user_overridden"
    assert by_id["d-2"]["chosen_option_id"] == "opt-b"


def test_answer_endpoint_400_on_malformed_body(tmp_path: Path) -> None:
    client, project_id, session_id, _ws = _build_app_with_session(tmp_path)
    # answers — не массив
    response = client.post(
        f"/api/projects/{project_id}/checkpoints/{session_id}/answer",
        json={"answers": "not-a-list"},
    )
    assert response.status_code == 400


def test_answer_endpoint_404_when_session_in_other_project(tmp_path: Path) -> None:
    """Scope protection: запрос с правильным session_id, но через id
    другого проекта — должен возвращать 404."""
    runtime_root = tmp_path / "runtime"
    project_a_ws = runtime_root / "case_a"
    project_b_ws = runtime_root / "case_b"
    project_a = init_project(project_a_ws, "Project A.")
    project_b = init_project(project_b_ws, "Project B.")

    runtime = SqliteRuntime()
    svc = CheckpointService(runtime)
    decisions = (_make_decision(decision_id="d-1", project_id=project_a),)
    result = svc.process_planned_decisions(
        project_a_ws,
        project_id=project_a,
        task_id="task-1",
        task_title="x",
        artifact_role="x",
        decisions=decisions,
        mode="balanced",
    )
    session_id = result.session.session_id  # type: ignore[union-attr]

    app = create_app(repo_root=REPO_ROOT, runtime_root=runtime_root, websocket_poll_interval=0.05)
    client = TestClient(app)

    # GET через project_a — OK
    ok = client.get(f"/api/projects/{project_a}/checkpoints/{session_id}")
    assert ok.status_code == 200

    # GET через project_b — 404 (либо потому что в его workspace нет такой
    # сессии, либо потому что scope check её отвергнет)
    leaked = client.get(f"/api/projects/{project_b}/checkpoints/{session_id}")
    assert leaked.status_code == 404
