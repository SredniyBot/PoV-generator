"""Оркестрация ролбека (Ф4): замок проекта + остановка прогона + движок.

Конкуррентность отделена от движка (SRP): RollbackService делает чистую работу
по состоянию, а координатор обеспечивает безопасность — берёт эксклюзивный
замок проекта (мутации начинают отказывать), форсированно гасит активный
прогон и дожидается его оседания, затем выполняет откат и снимает замок
(в finally — даже при ошибке).
"""

from __future__ import annotations

import uuid
from pathlib import Path

from ..common.errors import ConflictError
from ..domain.registry import RegistrySnapshot
from ..domain.rollback import RollbackResult
from ..infrastructure.sqlite_runtime import SqliteRuntime
from .rollback_service import RollbackService
from .workflow_runner_service import WorkflowRunnerService

# Запас на оседание прогона: форсированная отмена рвёт идущий шаг (в т.ч.
# LLM-вызов) между чекпоинтами; на практике поток оседает за секунды.
_RUN_SETTLE_TIMEOUT_S = 60.0


class RollbackCoordinator:
    def __init__(
        self,
        runtime: SqliteRuntime,
        runner: WorkflowRunnerService,
        rollback_service: RollbackService,
    ) -> None:
        self._runtime = runtime
        self._runner = runner
        self._rollback = rollback_service

    def preview(
        self, workspace: Path, snapshot: RegistrySnapshot, target_task_id: str
    ) -> set[str]:
        """Что будет инвалидировано (для превью до подтверждения)."""
        return self._rollback.preview(workspace, snapshot, target_task_id)

    def rollback_step(
        self,
        workspace: Path,
        snapshot: RegistrySnapshot,
        project_id: str,
        target_task_id: str,
        *,
        actor: str = "user",
        reason: str = "",
    ) -> RollbackResult:
        holder = f"rollback:{uuid.uuid4()}"
        if not self._runtime.acquire_project_lock(workspace, "rollback", holder):
            raise ConflictError(
                "Проект сейчас занят другой операцией — повторите немного позже."
            )
        try:
            # Авто-отмена активного прогона + ожидание полного оседания, чтобы
            # никакой шаг не писал состояние во время реконструкции.
            active = self._runner.latest_active_run(workspace, project_id)
            if active is not None:
                self._runner.cancel_run(workspace, active.run_id)
                self._runner.wait_until_idle(active.run_id, timeout_s=_RUN_SETTLE_TIMEOUT_S)
            return self._rollback.rollback_to(
                workspace, snapshot, target_task_id, actor=actor, reason=reason
            )
        finally:
            self._runtime.release_project_lock(workspace, holder)
