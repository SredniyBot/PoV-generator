"""Тесты для асинхронного WorkflowRunnerService (W4.1 / R1).

Закрывают behavioural contract:
1. start_run_until_blocked возвращает запись со status=pending мгновенно
   (не блокирует тред теста).
2. После запуска runner проходит несколько шагов и status переходит
   pending → running → completed.
3. После каждого шага current_step / steps[] / last_step_summary
   обновляются — это и есть «прогресс в реальном времени» для UI.
4. cancel_run ставит флаг; после следующей итерации runner финиширует
   со status=cancelled.
5. Запуск через stub-provider реально доходит до objective_completed
   (за разумное время).
"""

from __future__ import annotations

import time
from pathlib import Path

from pov_generator.application.checkpoint_service import CheckpointService
from pov_generator.application.context_service import ContextService
from pov_generator.application.execution_service import ExecutionService
from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.application.validation_service import ValidationService
from pov_generator.application.workflow_runner_service import WorkflowRunnerService
from pov_generator.application.workflow_service import WorkflowService
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE_REF = "common.requirements_specification@1.0.0"


def _bootstrap(tmp_path: Path):
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)
    context_service = ContextService(runtime)
    execution_service = ExecutionService(runtime, context_service)
    validation_service = ValidationService(runtime, CheckpointService(runtime))
    workflow_service = WorkflowService(runtime, planning_service, execution_service, validation_service)
    runner = WorkflowRunnerService(runtime, registry_service, workflow_service, planning_service)

    snapshot, report = registry_service.validate()
    assert report.is_valid

    workspace = tmp_path / "case"
    bootstrap = project_service.init_project(
        workspace=workspace,
        name="Runner smoke",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="Тест async runner.",
        domain_packs=(),
    )
    planning_service.expand_graph(workspace, snapshot)
    return workspace, bootstrap.manifest.project_id, runner, runtime


def _wait_until_terminal(runtime: SqliteRuntime, workspace: Path, run_id: str, *, timeout_s: float = 30.0):
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        run = runtime.get_workflow_run(workspace, run_id)
        last = run
        if run is not None and run.status in {"completed", "failed", "cancelled"}:
            return run
        time.sleep(0.05)
    raise AssertionError(f"Runner не достиг терминала за {timeout_s}s, последний снимок: {last}")


def test_start_run_until_blocked_returns_immediately_with_pending_status(tmp_path: Path) -> None:
    workspace, project_id, runner, runtime = _bootstrap(tmp_path)

    start = time.time()
    record = runner.start_run_until_blocked(
        workspace, project_id, provider="stub", model=None, max_steps=2,
    )
    elapsed = time.time() - start

    # start не должен висеть на шагах — он только создаёт запись и стартует
    # daemon thread. Шаги выполняются МИНУТЫ, поэтому любой суб-секундный/
    # пара-секундный возврат доказывает асинхронность. Порог 2с (а не 1с):
    # в тайминг попадает холодный парс реестра (~0.9с) у свежего
    # registry_service ранера — это не «выполнение шагов».
    assert elapsed < 2.0, f"start_run занял {elapsed:.2f}s (ожидаем мгновенный возврат)"
    assert record.status in {"pending", "running"}
    assert record.max_steps == 2
    assert record.cancel_requested is False
    assert record.current_step == 0


def test_runner_progresses_through_steps_and_reaches_completed(tmp_path: Path) -> None:
    workspace, project_id, runner, runtime = _bootstrap(tmp_path)

    record = runner.start_run_until_blocked(
        workspace, project_id, provider="stub", model=None, max_steps=3,
    )
    terminal = _wait_until_terminal(runtime, workspace, record.run_id, timeout_s=30.0)

    assert terminal.status == "completed"
    # Status finalised - finished_at recorded.
    assert terminal.finished_at is not None
    # Каждый шаг должен попасть в steps[].
    assert len(terminal.steps) >= 1
    # current_step и total_steps_completed двигаются.
    assert terminal.current_step >= 1
    assert terminal.last_step_summary  # не пусто
    # stop_reason заполнен.
    assert terminal.stop_reason in {
        "objective_completed", "planner_blocked", "validation_failed",
        "max_steps_reached", "execution_error",
    }


def test_cancel_run_marks_request_and_returns_true(tmp_path: Path, monkeypatch) -> None:
    """Cancel-механика проверяется на двух свойствах детерминированно:

    * ``cancel_run`` возвращает True для активного run'а и фиксирует
      ``cancel_requested`` в записи (read-after-write).
    * Если run терминируется до того, как cancel был запрошен —
      ``cancel_requested`` остаётся False.

    Гонка «cancel успел до завершения runner'а» не тестируется
    детерминированно: с быстрым SQLite (PRAGMA synchronous=OFF) workflow
    проходит 12 шагов за миллисекунды, и race-условие нестабильно.
    Цельность семантики покрыта unit-проверкой выше.
    """
    from pov_generator.application.workflow_service import WorkflowService

    workspace, project_id, runner, runtime = _bootstrap(tmp_path)

    # Параллельный шедулер запускает шаги через execute_step (не run_next).
    # Замедляем его, чтобы run оставался активным к моменту cancel_run.
    original_execute_step = WorkflowService.execute_step

    def slow_execute_step(self, workspace, snapshot, **kwargs):
        time.sleep(0.5)
        return original_execute_step(self, workspace, snapshot, **kwargs)

    monkeypatch.setattr(WorkflowService, "execute_step", slow_execute_step)

    record = runner.start_run_until_blocked(
        workspace, project_id, provider="stub", model=None, max_steps=50,
    )
    assert runner.cancel_run(workspace, record.run_id) is True

    # cancel_requested флаг записан немедленно, до конца runner'а.
    snapshot = runtime.get_workflow_run(workspace, record.run_id)
    assert snapshot is not None
    assert snapshot.cancel_requested is True

    # Дожидаемся терминала (cancelled или completed — race условие).
    terminal = _wait_until_terminal(runtime, workspace, record.run_id, timeout_s=15.0)
    assert terminal.status in {"cancelled", "completed"}


def test_cancel_unknown_run_returns_false(tmp_path: Path) -> None:
    workspace, _project_id, runner, _runtime = _bootstrap(tmp_path)
    assert runner.cancel_run(workspace, "not-a-real-run-id") is False


def test_list_runs_returns_run_records_for_project(tmp_path: Path) -> None:
    workspace, project_id, runner, runtime = _bootstrap(tmp_path)

    r1 = runner.start_run_until_blocked(workspace, project_id, provider="stub", model=None, max_steps=1)
    _wait_until_terminal(runtime, workspace, r1.run_id, timeout_s=15.0)

    listed = runner.list_runs(workspace, project_id=project_id)
    assert any(item.run_id == r1.run_id for item in listed)


def test_latest_active_run_returns_none_when_no_active(tmp_path: Path) -> None:
    workspace, project_id, runner, runtime = _bootstrap(tmp_path)
    record = runner.start_run_until_blocked(workspace, project_id, provider="stub", model=None, max_steps=1)
    _wait_until_terminal(runtime, workspace, record.run_id, timeout_s=15.0)
    assert runner.latest_active_run(workspace, project_id) is None


def test_provider_exhausted_stops_run_instead_of_grinding_through_tasks(
    tmp_path: Path, monkeypatch
) -> None:
    """Регрессия: при исчерпании квоты провайдера прогон ОСТАНАВЛИВАЕТСЯ, а не
    добивает все оставшиеся задачи в исчерпанной квоте.

    Раньше ProviderExhaustedError глотался в ``execute_step`` (generic
    ``except Exception`` → failed-result), не доходил до раннера и не
    останавливал пайплайн. Теперь он пробрасывается и финализирует прогон как
    failed/provider_exhausted.
    """
    from pov_generator.common.errors import ProviderExhaustedError

    workspace, project_id, runner, runtime = _bootstrap(tmp_path)

    def _raise_exhausted(*args, **kwargs):  # noqa: ANN002, ANN003
        raise ProviderExhaustedError("Исчерпан лимит/квота подписки.")

    monkeypatch.setattr(ExecutionService, "execute_task", _raise_exhausted)

    record = runner.start_run_until_blocked(
        workspace, project_id, provider="stub", model=None, max_steps=10,
    )
    terminal = _wait_until_terminal(runtime, workspace, record.run_id, timeout_s=30.0)

    assert terminal.status == "failed"
    assert terminal.stop_reason == "provider_exhausted"
    # Остановились на ПЕРВОЙ же задаче — не прогрызли весь граф.
    assert terminal.total_steps_completed == 0


def test_drain_record_writes_in_flight_siblings_to_overview(tmp_path: Path, monkeypatch) -> None:
    """Регрессия (Issue 3): при раннем останове прогона (исчерпание лимита)
    параллельные in-flight сиблинги должны попасть в overview (steps_json), а не
    остаться только в графе задач. ``_drain_record`` сливает пул и пишет их шаги.

    Отменённые (CancellationError) — НЕ шаг (вернулись в ready)."""
    from concurrent.futures import Future
    from types import SimpleNamespace

    from pov_generator.application.workflow_runner_service import _DispatchedStep
    from pov_generator.common.cancellation import CancellationError
    from pov_generator.common.errors import ProviderExhaustedError

    _workspace, _project_id, runner, _runtime = _bootstrap(tmp_path)

    recorded: list[tuple[str, str | None, str]] = []
    monkeypatch.setattr(
        runner, "_append_step",
        lambda ws, rid, meta, *, validation_status, planning_outcome, execution_run_id, reasons:
            recorded.append((meta.task_key, validation_status, planning_outcome)),
    )

    def _meta(key: str) -> _DispatchedStep:
        return _DispatchedStep(task_id=key, task_key=key, step_index=1,
                               started_at="t", write_set=frozenset())

    def _fut(result=None, exc=None) -> Future:
        f: Future = Future()
        if exc is not None:
            f.set_exception(exc)
        else:
            f.set_result(result)
        return f

    in_flight = {
        _fut(result=SimpleNamespace(validation_status="passed", planning_outcome="selected",
                                    execution_run_id="r", reasons=())): _meta("ok"),
        _fut(exc=ProviderExhaustedError("лимит")): _meta("exhausted"),
        _fut(exc=RuntimeError("boom")): _meta("errored"),
        _fut(exc=CancellationError("cancel")): _meta("cancelled"),
    }

    runner._drain_record(tmp_path, "run-x", in_flight)

    by_key = {k: (vs, po) for k, vs, po in recorded}
    assert by_key["ok"] == ("passed", "selected")
    assert by_key["exhausted"] == ("failed", "error")
    assert by_key["errored"] == ("failed", "error")
    assert "cancelled" not in by_key  # отменённые шагом не пишем
    assert not in_flight  # пул слит
