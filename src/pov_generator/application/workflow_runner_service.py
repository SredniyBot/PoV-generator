"""Асинхронный runner для `run_until_blocked` (W4.1 / R1).

Старый `WorkflowService.run_until_blocked` блокировал HTTP-запрос на весь
цикл шагов. При openrouter+deepseek один шаг = ~100 сек, max_steps=20
давало 30+ минут зависания UI.

`WorkflowRunnerService` запускает цикл в фоновом потоке, держит state
в `workflow_runs` таблице SQLite и обновляет её после **каждого шага** —
это вызывает изменение mtime БД, что инвалидирует `realtime_token`,
что вызывает WS-broadcast UI. Пользователь видит прогресс в real-time
без дополнительной push-инфраструктуры.

API:

- `start_run_until_blocked(workspace, project_id, *, provider, model,
  max_steps)` — создаёт запись `pending`, стартует thread, возвращает
  свежесозданный `WorkflowRunRecord`. Запрос не блокируется.
- `cancel_run(workspace, run_id)` — ставит флаг `cancel_requested=1`.
  Runner проверяет его между шагами и переходит в `cancelled`.
- `get_run(workspace, run_id)` / `list_runs(workspace, project_id)` —
  чтение state.

## Threading model

Один thread на run, daemon=True (умирает с процессом). SQLite
connection открывается **per-call** в SqliteRuntime, поэтому
thread-safe из коробки. Snapshot реестра фиксируется при старте,
чтобы изменения реестра во время run не влияли на текущий цикл.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from pathlib import Path

from ..common.serialization import utc_now_iso
from ..domain.registry import RegistrySnapshot
from ..domain.workflow_runs import WorkflowRunRecord, WorkflowRunStatus, WorkflowStepRecord
from ..infrastructure.sqlite_runtime import SqliteRuntime
from .planning_service import PlanningService
from .registry_service import RegistryService
from .workflow_service import WorkflowService


class WorkflowRunnerService:
    def __init__(
        self,
        runtime: SqliteRuntime,
        registry_service: RegistryService,
        workflow_service: WorkflowService,
        planning_service: PlanningService,
    ) -> None:
        self._runtime = runtime
        self._registry_service = registry_service
        self._workflow_service = workflow_service
        self._planning_service = planning_service

    # ---- public API ------------------------------------------------------

    def start_run_until_blocked(
        self,
        workspace: Path,
        project_id: str,
        *,
        provider: str | None,
        model: str | None,
        max_steps: int,
        continue_past_validation_failure: bool = False,
    ) -> WorkflowRunRecord:
        """Создаёт запись workflow_run и стартует фоновый thread. Возвращает
        свежий снимок записи (status=pending). UI должен poll'ить или
        слушать WS, чтобы увидеть прогресс.

        ``continue_past_validation_failure``: если True, runner не
        останавливается после validation_failed одной задачи, а пробует
        следующую допустимую. Нужно для auto-resume после ответа на
        уточнение: одна задача может стабильно валить валидацию, но
        другие готовые задачи (например, ``request_normalization``)
        обязаны получить свой шанс — иначе пользователю кажется, что
        система ходит по кругу.
        """
        snapshot, report = self._registry_service.validate()
        if not report.is_valid:
            raise RuntimeError("Registry invalid; cannot start workflow run.")

        run_id = str(uuid.uuid4())
        record = WorkflowRunRecord(
            run_id=run_id,
            project_id=project_id,
            status="pending",
            provider=provider,
            model=model,
            max_steps=int(max_steps),
            current_step=0,
            total_steps_completed=0,
            started_at=utc_now_iso(),
            finished_at=None,
            last_step_summary="Запуск...",
            stop_reason=None,
            error_message=None,
            cancel_requested=False,
            steps=(),
        )
        self._runtime.create_workflow_run(workspace, record)

        thread = threading.Thread(
            target=self._run_loop,
            args=(
                workspace,
                snapshot,
                run_id,
                provider,
                model,
                int(max_steps),
                bool(continue_past_validation_failure),
            ),
            daemon=True,
            name=f"workflow-run-{run_id[:8]}",
        )
        thread.start()
        return record

    def cancel_run(self, workspace: Path, run_id: str) -> bool:
        """Идемпотентный cancel. Возвращает True если запись существует
        (флаг проставлен), False если run_id не найден.

        Runner проверяет флаг МЕЖДУ шагами; уже идущий LLM-вызов не
        прерывается — это известный trade-off.
        """
        return self._runtime.request_workflow_cancel(workspace, run_id)

    def get_run(self, workspace: Path, run_id: str) -> WorkflowRunRecord | None:
        return self._runtime.get_workflow_run(workspace, run_id)

    def list_runs(
        self, workspace: Path, *, project_id: str | None = None, limit: int = 50
    ) -> list[WorkflowRunRecord]:
        return self._runtime.list_workflow_runs(workspace, project_id=project_id, limit=limit)

    def latest_active_run(self, workspace: Path, project_id: str) -> WorkflowRunRecord | None:
        return self._runtime.latest_active_workflow_run(workspace, project_id)

    # ---- internals -------------------------------------------------------

    def _run_loop(
        self,
        workspace: Path,
        snapshot: RegistrySnapshot,
        run_id: str,
        provider: str | None,
        model: str | None,
        max_steps: int,
        continue_past_validation_failure: bool = False,
    ) -> None:
        # pending → running
        self._mutate(workspace, run_id, status="running", last_step_summary="Запуск шагов...")

        for step_index in range(1, max_steps + 1):
            run = self._runtime.get_workflow_run(workspace, run_id)
            if run is None:
                return  # запись удалили извне — корректно молча выйти
            if run.cancel_requested:
                self._finalize(
                    workspace,
                    run_id,
                    status="cancelled",
                    stop_reason="cancelled_by_user",
                    summary="Прервано пользователем.",
                )
                return

            step_started_at = utc_now_iso()
            try:
                result = self._workflow_service.run_next(
                    workspace, snapshot, provider=provider, model=model
                )
            except Exception as exc:  # ловим всё — runner не должен падать
                message = str(exc).strip() or "Неизвестная ошибка в run_next."
                self._append_step_and_finalize(
                    workspace,
                    run_id,
                    step_index=step_index,
                    step_started_at=step_started_at,
                    planning_outcome="error",
                    selected_step_id=None,
                    task_id=None,
                    task_key=None,
                    execution_run_id=None,
                    validation_status=None,
                    error_message=message,
                    final_status="failed",
                    stop_reason="execution_error",
                    summary=f"Ошибка на шаге {step_index}: {message[:120]}",
                )
                return

            step_summary = self._step_summary_from_result(step_index, max_steps, result)
            step_record = WorkflowStepRecord(
                sequence=step_index,
                task_id=result.task_id,
                task_key=result.selected_step_id,
                selected_step_id=result.selected_step_id,
                planning_outcome=result.planning_outcome,
                validation_status=result.validation_status,
                execution_run_id=result.execution_run_id,
                started_at=step_started_at,
                finished_at=utc_now_iso(),
                error_message=None if result.validation_status == "passed" else "; ".join(result.reasons) or None,
            )

            # Если планировщик не выбрал/повторил задачу — это терминал.
            if result.planning_outcome not in {"selected", "retried"}:
                stop_reason = (
                    "objective_completed"
                    if result.planning_outcome == "objective_completed"
                    else "planner_blocked"
                )
                final_status: WorkflowRunStatus = "completed"
                self._append_step_and_finalize(
                    workspace,
                    run_id,
                    step_index=step_index,
                    step_started_at=step_started_at,
                    planning_outcome=result.planning_outcome,
                    selected_step_id=result.selected_step_id,
                    task_id=result.task_id,
                    task_key=result.selected_step_id,
                    execution_run_id=result.execution_run_id,
                    validation_status=result.validation_status,
                    error_message=step_record.error_message,
                    final_status=final_status,
                    stop_reason=stop_reason,
                    summary=step_summary,
                )
                return

            # v3.0: задача приостановлена pre-flight checkpoint'ом.
            # Workflow корректно завершается со специальным stop_reason —
            # это не ошибка, а ожидание пользователя. После submit_answers
            # пользователь увидит блокирующий task в UI и сможет нажать
            # retry; новый run подхватит финализированную сессию.
            if result.validation_status == "paused_for_checkpoint":
                self._append_step_and_finalize(
                    workspace,
                    run_id,
                    step_index=step_index,
                    step_started_at=step_started_at,
                    planning_outcome=result.planning_outcome,
                    selected_step_id=result.selected_step_id,
                    task_id=result.task_id,
                    task_key=result.selected_step_id,
                    execution_run_id=result.execution_run_id,
                    validation_status=result.validation_status,
                    error_message=step_record.error_message,
                    final_status="completed",
                    stop_reason="awaiting_checkpoint",
                    summary=(
                        f"Ждём ответа пользователя в checkpoint-сессии "
                        f"{result.checkpoint_session_id} перед сборкой "
                        f"{result.selected_step_id}."
                    ),
                )
                return

            # Validation провалилась.
            #
            # Default (continue_past_validation_failure=False): останавливаем
            # run — пользователю нужно посмотреть, что не так.
            #
            # Auto-resume (continue_past_validation_failure=True): просто
            # фиксируем step и идём дальше. Failed-задача уже помечена
            # status=failed в `WorkflowService._execute_existing_task`,
            # admission её больше не выберет; planner возьмёт следующую
            # допустимую задачу. Без этого одна стабильно валящая задача
            # блокировала весь pipeline после ответа на уточнение — главный
            # сценарий «система ходит кругами».
            if result.validation_status != "passed":
                if not continue_past_validation_failure:
                    self._append_step_and_finalize(
                        workspace,
                        run_id,
                        step_index=step_index,
                        step_started_at=step_started_at,
                        planning_outcome=result.planning_outcome,
                        selected_step_id=result.selected_step_id,
                        task_id=result.task_id,
                        task_key=result.selected_step_id,
                        execution_run_id=result.execution_run_id,
                        validation_status=result.validation_status,
                        error_message=step_record.error_message,
                        final_status="completed",
                        stop_reason="validation_failed",
                        summary=step_summary,
                    )
                    return
                # continue-режим: апдейтим запись и идём на следующую
                # итерацию планировщика.
                run = self._runtime.get_workflow_run(workspace, run_id)
                if run is None:
                    return
                updated = replace(
                    run,
                    current_step=step_index,
                    last_step_summary=step_summary,
                    steps=run.steps + (step_record,),
                )
                self._runtime.update_workflow_run(workspace, updated)
                continue

            # Шаг успешен — апдейтим запись и идём дальше.
            run = self._runtime.get_workflow_run(workspace, run_id)
            if run is None:
                return
            updated = replace(
                run,
                current_step=step_index,
                total_steps_completed=run.total_steps_completed + 1,
                last_step_summary=step_summary,
                steps=run.steps + (step_record,),
            )
            self._runtime.update_workflow_run(workspace, updated)

            # Возможно цель завершилась этим шагом — проверим dry-run.
            try:
                dry = self._planning_service.plan(
                    workspace, snapshot, mode="dry-run", record=False
                )
            except Exception:
                dry = None
            if dry is not None and dry.outcome == "objective_completed":
                self._finalize(
                    workspace,
                    run_id,
                    status="completed",
                    stop_reason="objective_completed",
                    summary=f"Цель достигнута на шаге {step_index}.",
                )
                return

        # max_steps исчерпан.
        self._finalize(
            workspace,
            run_id,
            status="completed",
            stop_reason="max_steps_reached",
            summary=f"Достигнут лимит {max_steps} шагов. Workflow можно продолжить новым запуском.",
        )

    # ---- helpers ---------------------------------------------------------

    def _mutate(
        self,
        workspace: Path,
        run_id: str,
        *,
        status: WorkflowRunStatus | None = None,
        last_step_summary: str | None = None,
    ) -> None:
        run = self._runtime.get_workflow_run(workspace, run_id)
        if run is None:
            return
        updated = replace(
            run,
            status=status if status is not None else run.status,
            last_step_summary=last_step_summary if last_step_summary is not None else run.last_step_summary,
        )
        self._runtime.update_workflow_run(workspace, updated)

    def _finalize(
        self,
        workspace: Path,
        run_id: str,
        *,
        status: WorkflowRunStatus,
        stop_reason: str,
        summary: str,
        error_message: str | None = None,
    ) -> None:
        run = self._runtime.get_workflow_run(workspace, run_id)
        if run is None:
            return
        updated = replace(
            run,
            status=status,
            finished_at=utc_now_iso(),
            stop_reason=stop_reason,
            last_step_summary=summary,
            error_message=error_message,
        )
        self._runtime.update_workflow_run(workspace, updated)

    def _append_step_and_finalize(
        self,
        workspace: Path,
        run_id: str,
        *,
        step_index: int,
        step_started_at: str,
        planning_outcome: str,
        selected_step_id: str | None,
        task_id: str | None,
        task_key: str | None,
        execution_run_id: str | None,
        validation_status: str | None,
        error_message: str | None,
        final_status: WorkflowRunStatus,
        stop_reason: str,
        summary: str,
    ) -> None:
        run = self._runtime.get_workflow_run(workspace, run_id)
        if run is None:
            return
        new_step = WorkflowStepRecord(
            sequence=step_index,
            task_id=task_id,
            task_key=task_key,
            selected_step_id=selected_step_id,
            planning_outcome=planning_outcome,
            validation_status=validation_status,
            execution_run_id=execution_run_id,
            started_at=step_started_at,
            finished_at=utc_now_iso(),
            error_message=error_message,
        )
        updated = replace(
            run,
            status=final_status,
            current_step=step_index,
            total_steps_completed=run.total_steps_completed
            + (1 if final_status == "completed" and stop_reason != "validation_failed" else 0),
            finished_at=utc_now_iso(),
            last_step_summary=summary,
            stop_reason=stop_reason,
            error_message=error_message,
            steps=run.steps + (new_step,),
        )
        self._runtime.update_workflow_run(workspace, updated)

    @staticmethod
    def _step_summary_from_result(step_index: int, max_steps: int, result) -> str:
        """Короткая человеко-читаемая сводка шага для last_step_summary.

        Раньше префикс был «Шаг N/M: …» — индикатор max_steps в UI был
        убран как нерелевантный (workflow не имеет фиксированной длины,
        max_steps — это sanity ceiling). Сводка теперь несёт только смысл
        события без счётчиков.
        """
        del step_index, max_steps  # не используем — UI считает сам
        if result.planning_outcome == "objective_completed":
            return "Цель проекта достигнута."
        if result.planning_outcome not in {"selected", "retried"}:
            return f"Планировщик заблокирован ({result.planning_outcome})."
        title = result.selected_step_id or (result.task_id or "")
        if result.validation_status == "passed":
            return f"Завершено: {title}"
        return f"Проблема: {title} ({result.validation_status})"
