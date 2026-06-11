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
- `cancel_run(workspace, run_id)` — форсированная остановка: ставит
  persistent-флаг `cancel_requested=1` (ловится между шагами, переживает
  рестарт) И дёргает in-memory `CancellationToken` текущего run'а, который
  прерывает уже идущий шаг — в т.ч. получение ответа LLM. Прерванная
  задача сбрасывается в `ready`, её результаты не коммитятся; следующий
  запуск продолжает с неё.
- `get_run(workspace, run_id)` / `list_runs(workspace, project_id)` —
  чтение state.

## Threading model

Один thread на run, daemon=True (умирает с процессом). SQLite
connection открывается **per-call** в SqliteRuntime, поэтому
thread-safe из коробки. Snapshot реестра фиксируется при старте,
чтобы изменения реестра во время run не влияли на текущий цикл.
"""

from __future__ import annotations

import contextvars
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import (
    ALL_COMPLETED,
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
)
from concurrent.futures import wait as futures_wait
from dataclasses import dataclass, replace
from pathlib import Path

from ..common.cancellation import CancellationError, CancellationToken
from ..common.logging import bind, get_logger
from ..common.serialization import utc_now_iso
from ..domain.registry import RegistrySnapshot
from ..domain.workflow_runs import WorkflowRunRecord, WorkflowRunStatus, WorkflowStepRecord
from ..infrastructure.sqlite_runtime import SqliteRuntime
from .parallel_scheduling import (
    max_concurrency_for,
    select_dispatchable,
    task_write_set,
)
from .planning_service import PlanningService
from .project_lock import ensure_project_unlocked
from .project_registry import ProjectRegistryResolver
from .registry_service import RegistryService
from .workflow_service import WorkflowService

logger = get_logger("runner")


@dataclass(frozen=True)
class _DispatchedStep:
    """Метаданные задачи, запущенной в пул, для записи step'а по завершении."""

    task_id: str
    task_key: str
    step_index: int
    started_at: str
    write_set: frozenset
    started_perf: float = 0.0  # time.perf_counter() на момент диспатча — для тайминга шага


class WorkflowRunnerService:
    def __init__(
        self,
        runtime: SqliteRuntime,
        registry_service: RegistryService,
        workflow_service: WorkflowService,
        planning_service: PlanningService,
        *,
        concurrency_resolver: Callable[[str | None], int] | None = None,
        registry_resolver: "ProjectRegistryResolver | None" = None,
    ) -> None:
        self._runtime = runtime
        self._registry_service = registry_service
        self._workflow_service = workflow_service
        self._planning_service = planning_service
        # Закреплённый граф проекта. Если внедрён — прогон идёт на снимке
        # реестра проекта, а не на живом templates/. None — fallback на живой
        # реестр (тесты/CLI без резолвера).
        self._registry_resolver = registry_resolver
        # Резолвер max_concurrency по провайдеру. По умолчанию — provider-aware
        # дефолты (parallel_scheduling.max_concurrency_for). В проде инжектится
        # из api.py резолвер, читающий per-provider настройку из UI
        # (ProviderConnection.extras["max_concurrency"]).
        self._concurrency_resolver: Callable[[str | None], int] = (
            concurrency_resolver or max_concurrency_for
        )
        # In-memory реестр токенов отмены активных run'ов. Позволяет
        # `cancel_run` форсированно прервать идущий LLM-вызов того же
        # процесса (в дополнение к persistent-флагу в БД, который ловится
        # между шагами и переживает рестарт). Доступ из потока runner'а и
        # HTTP-потока — под локом.
        self._tokens: dict[str, CancellationToken] = {}
        self._tokens_lock = threading.Lock()

    def _snapshot_for(self, workspace: Path) -> RegistrySnapshot:
        """Снимок реестра для прогона: закреплённый граф проекта или живой."""
        if self._registry_resolver is not None:
            return self._registry_resolver.snapshot_for(workspace)
        snapshot, report = self._registry_service.validate()
        if not report.is_valid:
            raise RuntimeError("Registry invalid; cannot start workflow run.")
        return snapshot

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
        # Шлюз: во время отката новые прогоны не стартуют.
        ensure_project_unlocked(self._runtime, workspace)
        snapshot = self._snapshot_for(workspace)

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

        token = CancellationToken()
        with self._tokens_lock:
            self._tokens[run_id] = token

        thread = threading.Thread(
            target=self._run_loop,
            args=(
                workspace,
                snapshot,
                run_id,
                provider,
                model,
                int(max_steps),
                token,
                bool(continue_past_validation_failure),
            ),
            daemon=True,
            name=f"workflow-run-{run_id[:8]}",
        )
        thread.start()
        return record

    def cancel_run(self, workspace: Path, run_id: str) -> bool:
        """Идемпотентная принудительная остановка run'а.

        Двухуровневый сигнал:

        1. **Persistent-флаг в БД** (``request_workflow_cancel``) — ловится
           runner'ом между шагами и переживает рестарт процесса (startup-
           recovery подхватит зомби-run).
        2. **In-memory токен** — если run идёт в этом же процессе,
           форсированно прерывает уже идущий шаг (в т.ч. получение ответа
           от LLM). Текущая задача сбрасывается в ``ready`` и продолжится
           со следующего запуска.

        Возвращает True, если запись run'а существует ИЛИ есть активный
        токен (т.е. отмена кому-то адресована).
        """
        flagged = self._runtime.request_workflow_cancel(workspace, run_id)
        with self._tokens_lock:
            token = self._tokens.get(run_id)
        if token is not None:
            token.cancel()
        return flagged or token is not None

    def wait_until_idle(self, run_id: str, *, timeout_s: float = 10.0, poll_s: float = 0.05) -> bool:
        """Дождаться, пока daemon-поток run'а полностью завершится.

        Токен снимается из реестра в ``_run_loop``'s finally — строго после
        того, как loop вышел и все воркеры отписали свои финальные транзакции.
        Поэтому отсутствие токена = «больше никто не пишет в этот workspace».
        Используется перед удалением проекта, чтобы `_connect` не пере-создал
        папку поверле rmtree. Возвращает True, если поток остановился в срок.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._tokens_lock:
                if run_id not in self._tokens:
                    return True
            time.sleep(poll_s)
        with self._tokens_lock:
            return run_id not in self._tokens

    def get_run(self, workspace: Path, run_id: str) -> WorkflowRunRecord | None:
        return self._runtime.get_workflow_run(workspace, run_id)

    def list_runs(
        self, workspace: Path, *, project_id: str | None = None, limit: int = 50
    ) -> list[WorkflowRunRecord]:
        return self._runtime.list_workflow_runs(workspace, project_id=project_id, limit=limit)

    def latest_active_run(self, workspace: Path, project_id: str) -> WorkflowRunRecord | None:
        return self._runtime.latest_active_workflow_run(workspace, project_id)

    def retry_task(
        self,
        workspace: Path,
        project_id: str,
        task_id: str,
        *,
        provider: str | None,
        model: str | None,
        max_steps: int = 1000,
    ) -> WorkflowRunRecord:
        """Повтор задачи — ЧЕРЕЗ оркестратор, а не в обход него.

        Раньше retry исполнялся синхронно в HTTP-потоке, мимо runner'а: не
        учитывался в concurrency и не продолжал конвейер. Теперь retry =
        «сбросить упавшую задачу в ready + обеспечить активный прогон»:

        * упавшую задачу возвращаем в ``ready`` (планировщик снова её допустит);
        * если прогон УЖЕ идёт — он подхватит готовую задачу на следующем
          раунде, строго в пределах concurrency (двух пулов не бывает);
        * если активного прогона нет — стартуем новый, который выполнит задачу
          и продолжит пайплайн дальше.

        Так задача всегда исполняется внутри пула runner'а (учтена в
        параллелизме), статус становится «идёт прогон», а после успеха конвейер
        продолжается сам. Возвращает активный/новый прогон.
        """
        ensure_project_unlocked(self._runtime, workspace)
        task = self._runtime.get_task(workspace, task_id)
        # failed → ready (attempt += 1). Только из failed: для прочих статусов
        # retry бессмыслен (UI предлагает повтор лишь у упавших), а ре-планирование
        # обеспечит активный прогон ниже.
        if task is not None and task.status == "failed":
            self._planning_service.transition_task(workspace, task_id, "retry")
        active = self.latest_active_run(workspace, project_id)
        if active is not None:
            # Идущий runner сам подхватит готовую задачу в пределах concurrency.
            return active
        return self.start_run_until_blocked(
            workspace,
            project_id,
            provider=provider,
            model=model,
            max_steps=max_steps,
        )

    # ---- internals -------------------------------------------------------

    def _run_loop(
        self,
        workspace: Path,
        snapshot: RegistrySnapshot,
        run_id: str,
        provider: str | None,
        model: str | None,
        max_steps: int,
        token: CancellationToken,
        continue_past_validation_failure: bool = False,
    ) -> None:
        """Тонкая обёртка: гарантированно снимает токен из реестра в finally,
        чтобы он не утёк после завершения run'а (любым исходом). Здесь же —
        run-scoped контекст логирования (run_id/project_id протекают во все
        нижележащие логи потока) и финальная запись о завершении с таймингом."""
        started = time.perf_counter()
        rec = self._runtime.get_workflow_run(workspace, run_id)
        project_id = rec.project_id if rec is not None else None
        with bind(run_id=run_id, project_id=project_id):
            try:
                self._run_loop_inner(
                    workspace,
                    snapshot,
                    run_id,
                    provider,
                    model,
                    max_steps,
                    token,
                    continue_past_validation_failure,
                )
            finally:
                with self._tokens_lock:
                    self._tokens.pop(run_id, None)
                final = self._runtime.get_workflow_run(workspace, run_id)
                dur = round((time.perf_counter() - started) * 1000)
                if final is not None:
                    logger.info(
                        "прогон завершён",
                        status=final.status,
                        reason=final.stop_reason,
                        steps=len(final.steps),
                        duration_ms=dur,
                    )

    def _run_step_with_context(
        self,
        workspace: Path,
        snapshot: RegistrySnapshot,
        candidate,
        provider: str | None,
        model: str | None,
        token: CancellationToken,
    ):
        """Выполнить шаг в воркере пула, добавив task в лог-контекст.

        Запускается уже внутри скопированного run-контекста (copy_context),
        поэтому run_id/project_id уже привязаны — добиваем task_id.
        """
        with bind(task_id=candidate.task_key):
            return self._workflow_service.execute_step(
                workspace,
                snapshot,
                task_id=candidate.task_id,
                selected_step_id=candidate.task_key,
                provider=provider,
                model=model,
                cancellation=token,
            )

    def _run_loop_inner(
        self,
        workspace: Path,
        snapshot: RegistrySnapshot,
        run_id: str,
        provider: str | None,
        model: str | None,
        max_steps: int,
        token: CancellationToken,
        continue_past_validation_failure: bool = False,
    ) -> None:
        """Параллельный шедулер шагов workflow.

        Динамически держит пул воркеров (cap = provider-aware), на каждом
        раунде пере-вычисляет admissible-набор, отбирает непересекающиеся по
        write-set задачи и диспатчит их, пополняя по мере завершения.
        ``max_concurrency=1`` воспроизводит прежнее последовательное поведение.

        Политики:
          * ошибка шага — fail-soft: упавшая задача не валит сиблингов
            (continue_past_validation_failure в параллельном режиме всегда
            подразумевается — не выбрасываем уже сделанную работу);
          * checkpoint — естественный гейт: приостановленная задача
            (paused_for_checkpoint → status failed) не ре-диспатчится,
            независимые ветки продолжают; run завершается awaiting_checkpoint,
            когда исчерпаны runnable;
          * отмена — общий токен рвёт все in-flight LLM-вызовы, прерванные
            задачи сбрасываются в ready (логика в WorkflowService).
        """
        del continue_past_validation_failure  # параллельный режим = fail-soft
        self._mutate(workspace, run_id, status="running", last_step_summary="Запуск шагов...")
        concurrency = max(1, self._concurrency_resolver(provider))
        logger.info(
            "запуск прогона",
            provider=provider or "авто",
            concurrency=concurrency,
        )

        def write_set_of(candidate) -> frozenset:
            try:
                return task_write_set(snapshot.resolve_template(candidate.template_ref))
            except Exception:  # noqa: BLE001 — нет шаблона не должно ронять шедулинг
                return frozenset()

        in_flight: dict[Future, _DispatchedStep] = {}
        dispatched = 0
        saw_validation_failure = False
        awaiting_checkpoint = False

        with ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix=f"wf-{run_id[:8]}"
        ) as pool:
            while True:
                run = self._runtime.get_workflow_run(workspace, run_id)
                if run is None:
                    return  # запись удалили извне — корректно молча выйти

                # --- отмена -------------------------------------------------
                if run.cancel_requested or token.is_cancelled:
                    # token уже дёрнут (cancel_run): in-flight воркеры получат
                    # CancellationError и сбросят свои задачи в ready. Ждём их
                    # завершения и финализируем run как cancelled.
                    logger.warning(
                        "останавливаю прогон: прерываю текущие шаги",
                        in_flight=len(in_flight),
                    )
                    if in_flight:
                        futures_wait(set(in_flight), return_when=ALL_COMPLETED)
                    self._finalize(
                        workspace,
                        run_id,
                        status="cancelled",
                        stop_reason="cancelled_by_user",
                        summary="Прервано пользователем: текущие шаги отменены, продолжите запуском.",
                    )
                    return

                # --- диспатч новых задач (conflict-aware, до cap) ----------
                free = concurrency - len(in_flight)
                if free > 0 and dispatched < max_steps:
                    admissible = self._planning_service.admissible_candidates(workspace, snapshot)
                    chosen = select_dispatchable(
                        admissible,
                        write_set_of=write_set_of,
                        in_flight_task_ids=[m.task_id for m in in_flight.values()],
                        in_flight_write_sets=[m.write_set for m in in_flight.values()],
                        free_slots=min(free, max_steps - dispatched),
                    )
                    for candidate in chosen:
                        meta = _DispatchedStep(
                            task_id=candidate.task_id,
                            task_key=candidate.task_key,
                            step_index=dispatched + 1,
                            started_at=utc_now_iso(),
                            write_set=write_set_of(candidate),
                            started_perf=time.perf_counter(),
                        )
                        logger.debug(
                            "шаг запущен",
                            task=candidate.task_key.split("@")[0],
                        )
                        try:
                            # copy_context() переносит run-scoped лог-контекст
                            # (run_id/project_id) в поток пула — иначе ThreadPool
                            # стартует воркер с пустым контекстом и логи шага
                            # теряют трассировку.
                            ctx = contextvars.copy_context()
                            future = pool.submit(
                                ctx.run,
                                self._run_step_with_context,
                                workspace,
                                snapshot,
                                candidate,
                                provider,
                                model,
                                token,
                            )
                        except RuntimeError:
                            # "cannot schedule new futures after shutdown" —
                            # пул гасится интерпретатором/atexit (остановка
                            # процесса или teardown теста). Daemon-поток ранера
                            # просто корректно завершается, не пытаясь писать в
                            # БД при умирающем процессе.
                            return
                        dispatched += 1
                        in_flight[future] = meta

                # --- терминал: ничего не бежит и нечего запускать ----------
                if not in_flight:
                    self._finalize_drained(
                        workspace,
                        run_id,
                        snapshot,
                        dispatched=dispatched,
                        max_steps=max_steps,
                        saw_validation_failure=saw_validation_failure,
                        awaiting_checkpoint=awaiting_checkpoint,
                    )
                    return

                # --- ждём завершения хотя бы одной задачи ------------------
                done, _pending = futures_wait(set(in_flight), return_when=FIRST_COMPLETED)
                for future in done:
                    meta = in_flight.pop(future)
                    try:
                        result = future.result()
                    except CancellationError:
                        # задача отменена и сброшена в ready; на следующей
                        # итерации поймаем cancel-флаг и финализируем cancelled.
                        continue
                    except Exception as exc:  # noqa: BLE001 — воркер не валит run
                        saw_validation_failure = True
                        dur = round((time.perf_counter() - meta.started_perf) * 1000)
                        logger.error(
                            "шаг упал с ошибкой",
                            task=meta.task_key.split("@")[0],
                            duration_ms=dur,
                            error=str(exc).strip() or type(exc).__name__,
                            exc_info=False,
                        )
                        self._append_step(
                            workspace,
                            run_id,
                            meta,
                            validation_status="failed",
                            planning_outcome="error",
                            execution_run_id=None,
                            reasons=(str(exc).strip() or "Ошибка исполнения шага.",),
                        )
                        continue
                    status = result.validation_status
                    if status == "paused_for_checkpoint":
                        awaiting_checkpoint = True
                    elif status != "passed":
                        saw_validation_failure = True
                    dur = round((time.perf_counter() - meta.started_perf) * 1000)
                    # passed/paused → INFO (норма), прочее (failed-валидация) → WARNING.
                    _done = logger.info if status in ("passed", "paused_for_checkpoint") else logger.warning
                    _done(
                        "шаг выполнен",
                        task=meta.task_key.split("@")[0],
                        status=status,
                        duration_ms=dur,
                    )
                    self._append_step(
                        workspace,
                        run_id,
                        meta,
                        validation_status=status,
                        planning_outcome=result.planning_outcome,
                        execution_run_id=result.execution_run_id,
                        reasons=result.reasons,
                    )

    def _finalize_drained(
        self,
        workspace: Path,
        run_id: str,
        snapshot: RegistrySnapshot,
        *,
        dispatched: int,
        max_steps: int,
        saw_validation_failure: bool,
        awaiting_checkpoint: bool,
    ) -> None:
        """Финализация, когда пул опустел и новых задач к запуску нет.

        Приоритет stop_reason: objective_completed → max_steps_reached →
        awaiting_checkpoint → validation_failed → planner_blocked.
        """
        try:
            dry = self._planning_service.plan(workspace, snapshot, mode="dry-run", record=False)
            objective_done = dry.outcome == "objective_completed"
        except Exception:  # noqa: BLE001
            objective_done = False
        if objective_done:
            self._finalize(
                workspace, run_id, status="completed",
                stop_reason="objective_completed", summary="Цель проекта достигнута.",
            )
        elif dispatched >= max_steps:
            self._finalize(
                workspace, run_id, status="completed",
                stop_reason="max_steps_reached",
                summary=f"Достигнут лимит {max_steps} шагов. Workflow можно продолжить новым запуском.",
            )
        elif awaiting_checkpoint:
            self._finalize(
                workspace, run_id, status="completed",
                stop_reason="awaiting_checkpoint",
                summary="Часть шагов ждёт ваших решений — ответьте в открытых решениях и продолжите.",
            )
        elif saw_validation_failure:
            self._finalize(
                workspace, run_id, status="completed",
                stop_reason="validation_failed",
                summary="Некоторые шаги завершились с ошибками валидации — см. детали задач.",
            )
        else:
            self._finalize(
                workspace, run_id, status="completed",
                stop_reason="planner_blocked",
                summary="Нет допустимых задач — workflow остановлен.",
            )

    def _append_step(
        self,
        workspace: Path,
        run_id: str,
        meta: _DispatchedStep,
        *,
        validation_status: str | None,
        planning_outcome: str,
        execution_run_id: str | None,
        reasons: tuple[str, ...],
    ) -> None:
        """Дописать завершённый параллельный step в запись run'а (без финализации)."""
        run = self._runtime.get_workflow_run(workspace, run_id)
        if run is None:
            return
        passed = validation_status == "passed"
        error_message = None if passed else ("; ".join(reasons) or None)
        step = WorkflowStepRecord(
            sequence=meta.step_index,
            task_id=meta.task_id,
            task_key=meta.task_key,
            selected_step_id=meta.task_key,
            planning_outcome=planning_outcome,
            validation_status=validation_status,
            execution_run_id=execution_run_id,
            started_at=meta.started_at,
            finished_at=utc_now_iso(),
            error_message=error_message,
        )
        updated = replace(
            run,
            current_step=meta.step_index,
            total_steps_completed=run.total_steps_completed + (1 if passed else 0),
            last_step_summary=self._step_summary_for(meta.task_key, validation_status),
            steps=run.steps + (step,),
        )
        self._runtime.update_workflow_run(workspace, updated)

    @staticmethod
    def _step_summary_for(task_key: str, validation_status: str | None) -> str:
        if validation_status == "passed":
            return f"Завершено: {task_key}"
        if validation_status == "paused_for_checkpoint":
            return f"Ожидает решений: {task_key}"
        return f"Проблема: {task_key} ({validation_status})"

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
