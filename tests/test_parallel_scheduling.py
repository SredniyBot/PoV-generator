"""Тесты параллельного шедулера: чистая логика выбора + интеграция."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from test_m9_api import build_services, init_project

from pov_generator.application.parallel_scheduling import (
    max_concurrency_for,
    select_dispatchable,
    task_write_set,
)
from pov_generator.application.workflow_runner_service import WorkflowRunnerService
from pov_generator.application.workflow_service import WorkflowService

# ---------------------------------------------------------------------------
# Чистая логика (быстро, детерминированно)
# ---------------------------------------------------------------------------


def _cand(task_id: str, write_set: set[str]) -> SimpleNamespace:
    return SimpleNamespace(task_id=task_id, task_key=task_id, _ws=frozenset(write_set))


def _ws_of(c: SimpleNamespace) -> frozenset:
    return c._ws


def test_max_concurrency_provider_aware() -> None:
    assert max_concurrency_for("claude_subscription") == 2
    assert max_concurrency_for("claude_sdk") == 5
    assert max_concurrency_for("openrouter") == 5
    assert max_concurrency_for("stub") == 8
    assert max_concurrency_for(None) == 3
    assert max_concurrency_for("something_else") == 3


def test_max_concurrency_env_override(monkeypatch) -> None:
    monkeypatch.setenv("POV_WORKFLOW_MAX_CONCURRENCY", "1")
    assert max_concurrency_for("stub") == 1
    monkeypatch.setenv("POV_WORKFLOW_MAX_CONCURRENCY", "не-число")
    assert max_concurrency_for("stub") == 8  # мусор игнорируется


def test_task_write_set_from_declared_effects() -> None:
    tpl = SimpleNamespace(
        outputs=SimpleNamespace(artifact_roles=("design_document",)),
        effects=SimpleNamespace(
            closes_gaps=("gap_x",),
            raises_readiness=(SimpleNamespace(dimension="dim_y"),),
        ),
    )
    assert task_write_set(tpl) == frozenset(
        {"artifact:design_document", "gap:gap_x", "readiness:dim_y"}
    )


def test_select_dispatchable_excludes_write_set_conflicts() -> None:
    cands = [_cand("a", {"artifact:x"}), _cand("b", {"artifact:y"}), _cand("c", {"artifact:x"})]
    chosen = select_dispatchable(
        cands, write_set_of=_ws_of, in_flight_task_ids=[], in_flight_write_sets=[], free_slots=5
    )
    # c конфликтует с a по artifact:x — исключён.
    assert [c.task_id for c in chosen] == ["a", "b"]


def test_select_dispatchable_excludes_in_flight() -> None:
    cands = [_cand("a", {"artifact:x"}), _cand("b", {"artifact:y"})]
    chosen = select_dispatchable(
        cands,
        write_set_of=_ws_of,
        in_flight_task_ids=["a"],
        in_flight_write_sets=[frozenset({"artifact:x"})],
        free_slots=5,
    )
    assert [c.task_id for c in chosen] == ["b"]


def test_select_dispatchable_caps_at_free_slots() -> None:
    cands = [_cand(str(i), {f"artifact:{i}"}) for i in range(5)]
    chosen = select_dispatchable(
        cands, write_set_of=_ws_of, in_flight_task_ids=[], in_flight_write_sets=[], free_slots=2
    )
    assert len(chosen) == 2


def test_select_dispatchable_zero_slots() -> None:
    cands = [_cand("a", {"artifact:x"})]
    assert select_dispatchable(
        cands, write_set_of=_ws_of, in_flight_task_ids=[], in_flight_write_sets=[], free_slots=0
    ) == []


# ---------------------------------------------------------------------------
# Интеграция: шедулер реально параллелит независимые шаги
# ---------------------------------------------------------------------------


def _wait_terminal(runner, workspace, run_id, *, timeout_s=60.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        run = runner.get_run(workspace, run_id)
        if run is not None and run.status in {"completed", "failed", "cancelled"}:
            return run
        time.sleep(0.05)
    raise AssertionError("Run не достиг терминала вовремя")


def test_scheduler_runs_independent_steps_in_parallel(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "runtime" / "case"
    project_id = init_project(workspace, "Подготовить ТЗ для сервиса нормализации заявок.")
    registry, runtime, _ps, planning, workflow = build_services()
    runner = WorkflowRunnerService(runtime, registry, workflow, planning)

    # Инструментируем execute_step: считаем максимум одновременных входов.
    original = WorkflowService.execute_step
    lock = threading.Lock()
    state = {"current": 0, "max": 0}

    def instrumented(self, workspace, snapshot, **kwargs):
        with lock:
            state["current"] += 1
            state["max"] = max(state["max"], state["current"])
        try:
            time.sleep(0.12)  # удерживаем перекрытие, чтобы зафиксировать конкуренцию
            return original(self, workspace, snapshot, **kwargs)
        finally:
            with lock:
                state["current"] -= 1

    monkeypatch.setattr(WorkflowService, "execute_step", instrumented)

    record = runner.start_run_until_blocked(
        workspace, project_id, provider="stub", model=None, max_steps=200
    )
    run = _wait_terminal(runner, workspace, record.run_id)

    # Корректность: run завершился, шаги записаны.
    assert run.status == "completed"
    assert run.total_steps_completed >= 2
    # Параллелизм: хотя бы раз ≥2 шага выполнялись одновременно.
    assert state["max"] >= 2, f"параллелизма не наблюдалось, max concurrent={state['max']}"


def test_scheduler_fail_soft_one_failure_does_not_abort_run(tmp_path: Path, monkeypatch) -> None:
    """Fail-soft: упавший шаг не валит весь run — остальные доезжают."""
    from pov_generator.application.workflow_service import WorkflowStepResult

    workspace = tmp_path / "runtime" / "case"
    project_id = init_project(workspace, "Подготовить ТЗ для сервиса обработки заявок.")
    registry, runtime, _ps, planning, workflow = build_services()
    runner = WorkflowRunnerService(runtime, registry, workflow, planning)

    original = WorkflowService.execute_step
    lock = threading.Lock()
    fail_target = {"id": None}

    def flaky(self, workspace, snapshot, *, task_id, selected_step_id=None,
              provider=None, model=None, cancellation=None):
        # Первую попавшую задачу принудительно валим (как реальный
        # execute_step при ошибке: start → fail), остальные — штатно.
        with lock:
            if fail_target["id"] is None:
                fail_target["id"] = task_id
        if task_id == fail_target["id"]:
            planning.transition_task(workspace, task_id, "start")
            planning.transition_task(
                workspace, task_id, "fail", payload={"error_message": "forced failure"}
            )
            return WorkflowStepResult(
                planning_outcome="selected",
                task_id=task_id,
                selected_step_id=selected_step_id,
                execution_run_id=None,
                validation_status="failed",
                reasons=("forced failure",),
            )
        return original(
            self, workspace, snapshot, task_id=task_id, selected_step_id=selected_step_id,
            provider=provider, model=model, cancellation=cancellation,
        )

    monkeypatch.setattr(WorkflowService, "execute_step", flaky)

    record = runner.start_run_until_blocked(
        workspace, project_id, provider="stub", model=None, max_steps=200
    )
    run = _wait_terminal(runner, workspace, record.run_id)

    # Run завершился (не завис на падении), и другие шаги отработали — fail-soft.
    assert run.status == "completed"
    assert run.total_steps_completed >= 1
    # Упавшая задача помечена failed и не ре-диспатчилась бесконечно.
    failed_task = runtime.get_task(workspace, fail_target["id"])
    assert failed_task.status == "failed"


def test_scheduler_sequential_when_concurrency_one(tmp_path: Path, monkeypatch) -> None:
    """max_concurrency=1 → строго последовательное поведение (LSP / safe rollout)."""
    monkeypatch.setenv("POV_WORKFLOW_MAX_CONCURRENCY", "1")
    workspace = tmp_path / "runtime" / "case"
    project_id = init_project(workspace, "Подготовить ТЗ для сервиса маршрутизации обращений.")
    registry, runtime, _ps, planning, workflow = build_services()
    runner = WorkflowRunnerService(runtime, registry, workflow, planning)

    original = WorkflowService.execute_step
    lock = threading.Lock()
    state = {"current": 0, "max": 0}

    def instrumented(self, workspace, snapshot, **kwargs):
        with lock:
            state["current"] += 1
            state["max"] = max(state["max"], state["current"])
        try:
            time.sleep(0.02)
            return original(self, workspace, snapshot, **kwargs)
        finally:
            with lock:
                state["current"] -= 1

    monkeypatch.setattr(WorkflowService, "execute_step", instrumented)

    record = runner.start_run_until_blocked(
        workspace, project_id, provider="stub", model=None, max_steps=200
    )
    run = _wait_terminal(runner, workspace, record.run_id)
    assert run.status == "completed"
    assert state["max"] == 1, f"при concurrency=1 ожидаем строго 1, получили {state['max']}"
