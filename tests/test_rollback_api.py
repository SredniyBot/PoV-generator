"""REST API ролбека (Ф5): превью, команда, история, шлюз во время отката.

Превью и история — чистое чтение через query_service; команда идёт через
координатор (замок + авто-отмена). Пока проект заблокирован откатом,
мутации (запуск прогона, ответы на решения) отклоняются 409.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from test_m9_api import REPO_ROOT, build_services, init_project  # type: ignore

from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime
from pov_generator.interfaces.api import create_app


def _run_steps(workspace: Path, steps: int) -> None:
    registry_service, _runtime, _ps, _pl, workflow_service = build_services()
    snapshot, report = registry_service.validate()
    assert report.is_valid
    workflow_service.run_until_blocked(
        workspace, snapshot, provider="stub", max_steps=steps
    )


def _bootstrap(tmp_path: Path) -> tuple[TestClient, str, Path, str]:
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "case_rb"
    project_id = init_project(workspace, "Нужно ТЗ для CRM.")
    _run_steps(workspace, 3)
    app = create_app(
        repo_root=REPO_ROOT, runtime_root=runtime_root, websocket_poll_interval=0.05
    )
    client = TestClient(app)
    target = SqliteRuntime().list_step_checkpoints(workspace)[0].task_id
    return client, project_id, workspace, target


def test_rollback_preview_lists_target_and_archived(tmp_path: Path) -> None:
    client, project_id, _ws, target = _bootstrap(tmp_path)

    resp = client.get(
        f"/api/projects/{project_id}/rollback/preview",
        params={"target_task_id": target},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["target_task_id"] == target
    assert body["target_title"]
    assert body["rollbackable"] is True  # у выполненного шага есть чекпоинт
    target_steps = [s for s in body["reverted_steps"] if s["task_id"] == target]
    assert target_steps and target_steps[0]["is_target"] is True
    # Откат самого раннего шага утягивает зависимые артефакты в архив.
    assert isinstance(body["archived_artifacts"], list)


def test_rollback_preview_unavailable_without_checkpoint(tmp_path: Path) -> None:
    client, project_id, workspace, _target = _bootstrap(tmp_path)
    runtime = SqliteRuntime()
    with_checkpoint = {c.task_id for c in runtime.list_step_checkpoints(workspace)}
    # Задача без чекпоинта (например, композит/невыполненный лист): откат недоступен.
    no_checkpoint = next(
        t.task_id for t in runtime.list_tasks(workspace) if t.task_id not in with_checkpoint
    )
    resp = client.get(
        f"/api/projects/{project_id}/rollback/preview",
        params={"target_task_id": no_checkpoint},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rollbackable"] is False
    assert body["blocked_reason"]


def test_rollback_command_then_history(tmp_path: Path) -> None:
    client, project_id, _ws, target = _bootstrap(tmp_path)

    resp = client.post(
        f"/api/projects/{project_id}/commands/rollback",
        json={"target_task_id": target, "reason": "проверка"},
    )
    assert resp.status_code == 200
    result = resp.json()
    assert target in result["reverted_task_ids"]

    hist = client.get(f"/api/projects/{project_id}/rollback/history").json()
    assert len(hist["items"]) == 1
    item = hist["items"][0]
    assert item["target_task_id"] == target
    assert item["reason"] == "проверка"
    assert item["reverted_count"] >= 1


def test_archive_endpoint_lists_rolled_back_and_excludes_current(tmp_path: Path) -> None:
    client, project_id, _ws, target = _bootstrap(tmp_path)

    # До отката архив пуст, но маршрут резолвится (не перехватывается /{id}).
    pre = client.get(f"/api/projects/{project_id}/artifacts/archive")
    assert pre.status_code == 200
    assert pre.json() == []

    resp = client.post(
        f"/api/projects/{project_id}/commands/rollback",
        json={"target_task_id": target, "reason": "t"},
    )
    assert resp.status_code == 200
    archived_ids = set(resp.json()["archived_artifact_ids"])
    assert archived_ids, "откат самого раннего шага должен что-то заархивировать"

    archive = client.get(f"/api/projects/{project_id}/artifacts/archive").json()
    archive_ids = {a["artifact_id"] for a in archive}
    assert archived_ids <= archive_ids
    assert all(a["archived"] for a in archive if a["artifact_id"] in archived_ids)

    # В текущем списке заархивированных артефактов нет.
    current = client.get(f"/api/projects/{project_id}/artifacts").json()
    current_ids = {a["artifact_id"] for a in current}
    assert not (archived_ids & current_ids)


def test_rollback_command_unknown_target_returns_409(tmp_path: Path) -> None:
    client, project_id, _ws, _target = _bootstrap(tmp_path)
    resp = client.post(
        f"/api/projects/{project_id}/commands/rollback",
        json={"target_task_id": "no-such-task"},
    )
    assert resp.status_code == 409


def test_mutations_refused_while_project_locked(tmp_path: Path) -> None:
    client, project_id, workspace, target = _bootstrap(tmp_path)
    # Эмулируем удержание замка (как во время идущего отката).
    runtime = SqliteRuntime()
    assert runtime.acquire_project_lock(workspace, "rollback", "external") is True
    try:
        # Чтение превью не блокируется.
        preview = client.get(
            f"/api/projects/{project_id}/rollback/preview",
            params={"target_task_id": target},
        )
        assert preview.status_code == 200
        # Запуск прогона и повторный откат — отклоняются.
        run = client.post(
            f"/api/projects/{project_id}/commands/run-until-blocked", json={}
        )
        assert run.status_code == 409
        rollback = client.post(
            f"/api/projects/{project_id}/commands/rollback",
            json={"target_task_id": target},
        )
        assert rollback.status_code == 409
    finally:
        runtime.release_project_lock(workspace, "external")
