"""Шлюз/конкуррентность ролбека (Ф4): замок проекта, guard, координатор."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_m9_api import OBJECTIVE_REF, build_services  # type: ignore

from pov_generator.application.project_lock import ensure_project_unlocked
from pov_generator.application.rollback_coordinator import RollbackCoordinator
from pov_generator.application.rollback_service import RollbackService
from pov_generator.application.workflow_runner_service import WorkflowRunnerService
from pov_generator.common.errors import ConflictError
from pov_generator.domain.registry import ObjectRef


def _setup(tmp_path: Path, *, run_steps: int = 0):
    registry_service, runtime, project_service, planning_service, workflow_service = build_services()
    snapshot, report = registry_service.validate()
    assert report.is_valid
    ws = tmp_path / "ws"
    bootstrap = project_service.init_project(
        workspace=ws,
        name="T",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="Нужно ТЗ для CRM.",
        domain_packs=(),
    )
    planning_service.expand_graph(ws, snapshot)
    if run_steps:
        workflow_service.run_until_blocked(ws, snapshot, provider="stub", max_steps=run_steps)
    runner = WorkflowRunnerService(runtime, registry_service, workflow_service, planning_service)
    return {
        "ws": ws,
        "snapshot": snapshot,
        "runtime": runtime,
        "runner": runner,
        "project_id": bootstrap.manifest.project_id,
    }


# --- замок ------------------------------------------------------------------


def test_lock_acquire_release_conflict(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    ws, runtime = env["ws"], env["runtime"]
    assert runtime.active_project_lock(ws) is None
    assert runtime.acquire_project_lock(ws, "rollback", "h1") is True
    assert runtime.acquire_project_lock(ws, "rollback", "h2") is False  # уже занят
    assert runtime.active_project_lock(ws).holder == "h1"
    runtime.release_project_lock(ws, "h2")  # чужой holder — не снимает
    assert runtime.active_project_lock(ws) is not None
    runtime.release_project_lock(ws, "h1")
    assert runtime.active_project_lock(ws) is None


# --- guard ------------------------------------------------------------------


def test_ensure_unlocked_raises_when_locked(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    ws, runtime = env["ws"], env["runtime"]
    ensure_project_unlocked(runtime, ws)  # без замка — ок
    runtime.acquire_project_lock(ws, "rollback", "h")
    with pytest.raises(ConflictError):
        ensure_project_unlocked(runtime, ws)


def test_start_run_refused_under_lock(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    ws, runtime, runner = env["ws"], env["runtime"], env["runner"]
    runtime.acquire_project_lock(ws, "rollback", "h")
    with pytest.raises(ConflictError):
        runner.start_run_until_blocked(
            ws, env["project_id"], provider="stub", model=None, max_steps=1
        )
    runtime.release_project_lock(ws, "h")
    record = runner.start_run_until_blocked(
        ws, env["project_id"], provider="stub", model=None, max_steps=1
    )
    assert record is not None
    runner.wait_until_idle(record.run_id, timeout_s=15.0)


# --- координатор ------------------------------------------------------------


def test_coordinator_rolls_back_and_releases_lock(tmp_path: Path) -> None:
    env = _setup(tmp_path, run_steps=3)
    ws, runtime = env["ws"], env["runtime"]
    coordinator = RollbackCoordinator(runtime, env["runner"], RollbackService(runtime))
    target = runtime.list_step_checkpoints(ws)[0].task_id

    result = coordinator.rollback_step(ws, env["snapshot"], env["project_id"], target)
    assert target in result.reverted_task_ids
    assert runtime.active_project_lock(ws) is None  # замок снят


def test_coordinator_releases_lock_on_error(tmp_path: Path) -> None:
    env = _setup(tmp_path)  # шаги не выполнялись → откатывать нечего
    ws, runtime = env["ws"], env["runtime"]
    coordinator = RollbackCoordinator(runtime, env["runner"], RollbackService(runtime))
    some_task = next(t for t in runtime.list_tasks(ws) if t.template_type == "leaf")
    with pytest.raises(ConflictError):
        coordinator.rollback_step(ws, env["snapshot"], env["project_id"], some_task.task_id)
    assert runtime.active_project_lock(ws) is None  # снят даже при ошибке
