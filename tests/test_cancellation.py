"""Тесты принудительной остановки шага: домен отмены, доменная команда
``cancel`` и интеграция через workflow/runner.

Контракт фичи: остановка работы над проектом форсированно прерывает текущий
шаг (в т.ч. получение ответа LLM), обнуляет его результаты и возвращает
задачу в ``ready`` — чтобы следующий запуск продолжил ровно с неё.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from test_m9_api import build_services, init_project

from pov_generator.application.workflow_runner_service import WorkflowRunnerService
from pov_generator.common.cancellation import (
    CancellationError,
    CancellationToken,
    cancellation_scope,
    current_cancellation,
)
from pov_generator.domain.tasks import TaskRecord, apply_task_command

# ---------------------------------------------------------------------------
# CancellationToken
# ---------------------------------------------------------------------------


def test_token_cancel_is_idempotent_and_fires_callbacks_once() -> None:
    token = CancellationToken()
    fired: list[int] = []
    token.register(lambda: fired.append(1))
    assert not token.is_cancelled

    token.cancel()
    token.cancel()  # повтор — без эффекта
    assert token.is_cancelled
    assert fired == [1]


def test_token_register_after_cancel_fires_immediately() -> None:
    token = CancellationToken()
    token.cancel()
    fired: list[int] = []
    unregister = token.register(lambda: fired.append(7))
    assert fired == [7]
    unregister()  # no-op, не падает


def test_token_unregister_prevents_callback() -> None:
    token = CancellationToken()
    fired: list[int] = []
    unregister = token.register(lambda: fired.append(1))
    unregister()
    token.cancel()
    assert fired == []


def test_token_raise_if_cancelled() -> None:
    token = CancellationToken()
    token.raise_if_cancelled()  # не отменён — тихо
    token.cancel()
    with pytest.raises(CancellationError):
        token.raise_if_cancelled()


def test_cancellation_scope_sets_and_resets_ambient_token() -> None:
    assert current_cancellation() is None
    token = CancellationToken()
    with cancellation_scope(token):
        assert current_cancellation() is token
        with cancellation_scope(None):
            assert current_cancellation() is None
        assert current_cancellation() is token
    assert current_cancellation() is None


# ---------------------------------------------------------------------------
# Доменная команда cancel
# ---------------------------------------------------------------------------


def _task(status: str, **overrides) -> TaskRecord:
    base = dict(
        task_id="t1",
        project_id="p1",
        objective_ref="o@1.0.0",
        parent_task_id=None,
        template_ref="tpl@1.0.0",
        template_type="leaf",
        title="Шаг",
        status=status,
        origin_kind="objective_root",
        origin_ref="o@1.0.0",
        stable_key="p1:tpl",
        depth=0,
        slot_id=None,
        attempt=1,
        error_message=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    base.update(overrides)
    return TaskRecord(**base)


def test_cancel_command_resets_in_progress_to_ready() -> None:
    task = _task("in_progress")
    result = apply_task_command(task, "cancel")
    assert result.status == "ready"
    assert result.error_message is None
    # attempt не инкрементируется — это отмена, а не повтор.
    assert result.attempt == task.attempt


def test_cancel_command_clears_error_from_failed() -> None:
    task = _task("failed", error_message="boom")
    result = apply_task_command(task, "cancel")
    assert result.status == "ready"
    assert result.error_message is None
    assert result.attempt == task.attempt


def test_cancel_command_noop_on_terminal_states() -> None:
    for terminal in ("completed", "obsolete", "skipped"):
        task = _task(terminal)
        result = apply_task_command(task, "cancel")
        assert result is task  # без изменений


# ---------------------------------------------------------------------------
# Интеграция: run_next с отменённым токеном
# ---------------------------------------------------------------------------


def test_run_next_with_cancelled_token_resets_task_and_writes_nothing(tmp_path: Path) -> None:
    workspace = tmp_path / "runtime" / "case"
    init_project(
        workspace,
        "Подготовить техническое задание для сервиса нормализации заявок.",
    )
    registry_service, runtime, _ps, _pl, workflow_service = build_services()
    snapshot, report = registry_service.validate()
    assert report.is_valid

    token = CancellationToken()
    token.cancel()  # отмена ещё до начала шага → срабатывает входной чекпоинт

    with pytest.raises(CancellationError):
        workflow_service.run_next(workspace, snapshot, provider="stub", cancellation=token)

    tasks = runtime.list_tasks(workspace)
    # Задача, которую планировщик стартовал, возвращена в ready (не failed).
    assert all(t.status != "in_progress" for t in tasks)
    assert all(t.status != "failed" for t in tasks)
    assert any(t.status == "ready" for t in tasks)
    # Результаты обнулены: отмена до коммита — ни одного артефакта.
    assert len(runtime.list_artifacts(workspace)) == 0


# ---------------------------------------------------------------------------
# Интеграция: runner форсированно прерывает идущий шаг
# ---------------------------------------------------------------------------


class _BlockingWorkflowService:
    """Фейковый WorkflowService: имитирует долгий шаг (как получение ответа
    LLM), который висит до отмены, затем форсированно прерывается."""

    def __init__(self) -> None:
        self.entered = threading.Event()

    def run_next(self, workspace, snapshot, *, provider=None, model=None, cancellation=None):
        self.entered.set()
        # Блокируемся как реальный LLM-вызов; выходим только по отмене.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if cancellation is not None and cancellation.is_cancelled:
                raise CancellationError("LLM-вызов прерван пользователем.")
            time.sleep(0.01)
        raise AssertionError("Шаг не был прерван отменой в отведённое время.")


def test_runner_cancel_run_forcibly_interrupts_step(tmp_path: Path) -> None:
    workspace = tmp_path / "runtime" / "case"
    project_id = init_project(
        workspace,
        "Подготовить техническое задание для сервиса маршрутизации обращений.",
    )
    registry_service, runtime, _ps, planning_service, _ws = build_services()
    fake_workflow = _BlockingWorkflowService()
    runner = WorkflowRunnerService(runtime, registry_service, fake_workflow, planning_service)

    record = runner.start_run_until_blocked(
        workspace, project_id, provider="stub", model=None, max_steps=5
    )
    assert fake_workflow.entered.wait(timeout=3.0), "Шаг не стартовал"

    # Форсированная остановка из «HTTP-потока».
    assert runner.cancel_run(workspace, record.run_id) is True

    # Runner должен финализировать run как cancelled.
    final = None
    for _ in range(300):
        final = runner.get_run(workspace, record.run_id)
        if final is not None and final.status == "cancelled":
            break
        time.sleep(0.02)
    assert final is not None and final.status == "cancelled"
    assert final.stop_reason == "cancelled_by_user"

    # Токен снят из реестра после завершения run'а (не течёт).
    assert record.run_id not in runner._tokens
