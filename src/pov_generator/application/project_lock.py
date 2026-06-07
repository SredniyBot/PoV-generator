"""Шлюз проекта: общая проверка эксклюзивного замка (Ф4 ролбека).

Пока держится замок (идёт откат), любые мутации проекта (запуск шагов,
активация objective, ответы на решения, повторный откат) отказывают. Один
маленький хелпер — единая точка проверки для всех точек входа (DRY).
"""

from __future__ import annotations

from pathlib import Path

from ..common.errors import ConflictError
from ..infrastructure.sqlite_runtime import SqliteRuntime


def ensure_project_unlocked(runtime: SqliteRuntime, workspace: Path) -> None:
    """Бросить ConflictError, если проект занят критической операцией."""
    lock = runtime.active_project_lock(workspace)
    if lock is not None:
        raise ConflictError(
            "Сейчас идёт откат шага — операции с проектом приостановлены. "
            "Дождитесь завершения отката."
        )
