"""Тесты REST API реестра решений (v3.0).

Покрывают:
- GET /api/projects/{id}/decisions — список с агрегатами + фильтры
- GET /api/projects/{id}/decisions/{decision_id} — детали
- Scope-protection: decision из проекта A не открывается через id проекта B

Изоляция: создаётся отдельный workspace per-test через tmp_path,
runtime не разделяется между тестами.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_m9_api import init_project  # type: ignore

from pov_generator.domain.decisions import Decision, DecisionAlternative
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime
from pov_generator.interfaces.api import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_decision(
    *,
    decision_id: str,
    project_id: str,
    title: str = "Test decision",
    level: str = "architecture",
    confidence: float = 0.8,
    status: str = "proposed",
) -> Decision:
    return Decision(
        decision_id=decision_id,
        project_id=project_id,
        title=title,
        description=f"Description for {title}",
        chosen_option_id="opt-a",
        alternatives=(
            DecisionAlternative(
                option_id="opt-a",
                label="Variant A",
                description="primary choice",
                pros=("good",),
                cons=("ok",),
                confidence=0.85,
            ),
            DecisionAlternative(
                option_id="opt-b",
                label="Variant B",
                description="alternative",
                pros=(),
                cons=("worse",),
                confidence=0.3,
            ),
        ),
        rationale="Because in this context A is better",
        level=level,  # type: ignore[arg-type]
        level_rationale="Affects multiple components",
        confidence=confidence,
        status=status,  # type: ignore[arg-type]
        source="pre_flight",
        source_task_id="task-1",
        affected_artifact_ids=("art-1",),
    )


def _build_client_with_project(tmp_path: Path) -> tuple[TestClient, str, Path]:
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "case_decisions"
    project_id = init_project(workspace, "Bootstrap project for decision-ledger tests.")
    app = create_app(repo_root=REPO_ROOT, runtime_root=runtime_root, websocket_poll_interval=0.05)
    client = TestClient(app)
    return client, project_id, workspace


# ---------------------------------------------------------------------------
# GET list — base behavior
# ---------------------------------------------------------------------------


def test_list_endpoint_returns_empty_view_when_no_decisions(tmp_path: Path) -> None:
    client, project_id, _ws = _build_client_with_project(tmp_path)
    response = client.get(f"/api/projects/{project_id}/decisions")
    assert response.status_code == 200
    body = response.json()
    assert body["project_id"] == project_id
    assert body["items"] == []
    # Все счётчики должны быть 0, не отсутствовать
    for key in (
        "surfaced_total",
        "surfaced_pending",
        "business_count",
        "architecture_count",
        "detail_count",
        "proposed_count",
        "accepted_count",
        "overridden_count",
        "low_confidence_count",
    ):
        assert body[key] == 0, f"{key} должен быть 0 в пустом проекте"


def test_list_endpoint_returns_seeded_decisions(tmp_path: Path) -> None:
    client, project_id, workspace = _build_client_with_project(tmp_path)
    runtime = SqliteRuntime()
    runtime.upsert_decision(
        workspace,
        _make_decision(decision_id="d-biz", project_id=project_id, level="business"),
    )
    runtime.upsert_decision(
        workspace,
        _make_decision(decision_id="d-arch", project_id=project_id, level="architecture"),
    )

    response = client.get(f"/api/projects/{project_id}/decisions")
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    assert body["business_count"] == 1
    assert body["architecture_count"] == 1
    assert body["detail_count"] == 0


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_list_endpoint_filters_by_level(tmp_path: Path) -> None:
    client, project_id, workspace = _build_client_with_project(tmp_path)
    runtime = SqliteRuntime()
    for did, lvl in [("d-biz", "business"), ("d-arch", "architecture"), ("d-det", "detail")]:
        runtime.upsert_decision(
            workspace,
            _make_decision(decision_id=did, project_id=project_id, level=lvl),
        )

    response = client.get(f"/api/projects/{project_id}/decisions?level=business")
    assert response.status_code == 200
    body = response.json()
    assert [item["decision_id"] for item in body["items"]] == ["d-biz"]
    # Агрегаты не должны зависеть от фильтра — это контракт UI
    assert body["business_count"] == 1
    assert body["architecture_count"] == 1
    assert body["detail_count"] == 1


def test_list_endpoint_filters_by_status(tmp_path: Path) -> None:
    client, project_id, workspace = _build_client_with_project(tmp_path)
    runtime = SqliteRuntime()
    runtime.upsert_decision(
        workspace,
        _make_decision(decision_id="d-prop", project_id=project_id, status="proposed"),
    )
    runtime.upsert_decision(
        workspace,
        _make_decision(decision_id="d-lock", project_id=project_id, status="locked_in"),
    )

    response = client.get(f"/api/projects/{project_id}/decisions?status=locked_in")
    assert response.status_code == 200
    body = response.json()
    assert [item["decision_id"] for item in body["items"]] == ["d-lock"]


# ---------------------------------------------------------------------------
# Surfaced counters (mode-dependent)
# ---------------------------------------------------------------------------


def test_surfaced_counters_reflect_project_mode(tmp_path: Path) -> None:
    """surfaced_total — это про режим проекта.

    Проект по умолчанию `balanced` (см. ProcessState defaults) → видит
    только business-уровень. Architecture-решения не попадают в surfaced.
    """
    client, project_id, workspace = _build_client_with_project(tmp_path)
    runtime = SqliteRuntime()
    runtime.upsert_decision(
        workspace,
        _make_decision(decision_id="d-biz", project_id=project_id, level="business"),
    )
    runtime.upsert_decision(
        workspace,
        _make_decision(decision_id="d-arch-1", project_id=project_id, level="architecture"),
    )
    runtime.upsert_decision(
        workspace,
        _make_decision(decision_id="d-arch-2", project_id=project_id, level="architecture"),
    )

    response = client.get(f"/api/projects/{project_id}/decisions")
    body = response.json()
    # mode='balanced' → видит только business → 1
    assert body["mode"] == "balanced"
    assert body["surfaced_total"] == 1
    # И поскольку статус proposed — surfaced_pending тоже 1
    assert body["surfaced_pending"] == 1


# ---------------------------------------------------------------------------
# Low confidence highlighting
# ---------------------------------------------------------------------------


def test_low_confidence_flagged_in_view(tmp_path: Path) -> None:
    client, project_id, workspace = _build_client_with_project(tmp_path)
    runtime = SqliteRuntime()
    runtime.upsert_decision(
        workspace,
        _make_decision(decision_id="d-risky", project_id=project_id, confidence=0.3),
    )
    runtime.upsert_decision(
        workspace,
        _make_decision(decision_id="d-safe", project_id=project_id, confidence=0.9),
    )

    body = client.get(f"/api/projects/{project_id}/decisions").json()
    assert body["low_confidence_count"] == 1

    by_id = {item["decision_id"]: item for item in body["items"]}
    assert by_id["d-risky"]["is_low_confidence"] is True
    assert by_id["d-safe"]["is_low_confidence"] is False


# ---------------------------------------------------------------------------
# Detail endpoint
# ---------------------------------------------------------------------------


def test_detail_endpoint_returns_chosen_label_and_alternatives(tmp_path: Path) -> None:
    client, project_id, workspace = _build_client_with_project(tmp_path)
    runtime = SqliteRuntime()
    runtime.upsert_decision(
        workspace,
        _make_decision(decision_id="d-detail", project_id=project_id),
    )

    response = client.get(f"/api/projects/{project_id}/decisions/d-detail")
    assert response.status_code == 200
    body = response.json()
    assert body["decision_id"] == "d-detail"
    assert body["chosen_option_label"] == "Variant A"
    assert len(body["alternatives"]) == 2
    # is_chosen помечается у выбранного варианта, не у других
    by_id = {alt["option_id"]: alt for alt in body["alternatives"]}
    assert by_id["opt-a"]["is_chosen"] is True
    assert by_id["opt-b"]["is_chosen"] is False


def test_detail_endpoint_404_when_not_found(tmp_path: Path) -> None:
    client, project_id, _ws = _build_client_with_project(tmp_path)
    response = client.get(f"/api/projects/{project_id}/decisions/non-existent")
    assert response.status_code == 404


def test_detail_endpoint_blocks_cross_project_access(tmp_path: Path) -> None:
    """Decision из проекта A не должна открываться через id проекта B,
    даже если оба workspace в одном runtime_root.

    Это защита от scope-confusion: пользователь не может через знание
    decision_id вытащить чужие решения, подставив свой project_id."""
    runtime_root = tmp_path / "runtime"
    project_a_ws = runtime_root / "case_a"
    project_b_ws = runtime_root / "case_b"
    project_a = init_project(project_a_ws, "Project A.")
    project_b = init_project(project_b_ws, "Project B.")

    runtime = SqliteRuntime()
    # Решение принадлежит проекту A, лежит в workspace A
    runtime.upsert_decision(
        project_a_ws,
        _make_decision(decision_id="d-secret", project_id=project_a),
    )

    app = create_app(repo_root=REPO_ROOT, runtime_root=runtime_root, websocket_poll_interval=0.05)
    client = TestClient(app)

    # Запрос через project_a — должен находить (это позитивный sanity check)
    ok = client.get(f"/api/projects/{project_a}/decisions/d-secret")
    assert ok.status_code == 200

    # Запрос через project_b — должен возвращать 404, не отдавать чужие данные
    # (workspace разные → runtime.get_decision из workspace B не найдёт)
    leaked = client.get(f"/api/projects/{project_b}/decisions/d-secret")
    assert leaked.status_code == 404
