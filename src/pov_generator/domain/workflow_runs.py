"""Domain-модель для асинхронных workflow runs (W4.1 / R1).

`WorkflowService.run_until_blocked` исторически был синхронным: одна
HTTP-сессия отвечала за всю цепочку run_next до блокировки. При
openrouter+deepseek-flash один шаг занимает ~100 секунд, что превращало
`run_until_blocked` в 5-30 минутный блокирующий запрос с полным
отсутствием прогресса в UI.

`WorkflowRunRecord` — состояние асинхронного запуска, persistent в
SQLite. Фоновый thread пишет в эту запись прогресс между каждым
шагом; UI наблюдает изменения через стандартный realtime_token
(`run_status_changed` теперь часть mtime БД).

Цикл состояний:

```
pending → running → completed
                  → failed
                  → cancelled
```

`cancel_requested = True` — флаг, который пользователь может
выставить через `POST /commands/cancel-workflow`; runner смотрит его
между каждым шагом и завершает с `cancelled`, если установлен.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

WorkflowRunStatus = Literal["pending", "running", "completed", "failed", "cancelled"]


@dataclass(frozen=True)
class WorkflowStepRecord:
    """Один шаг внутри workflow_run — компактный summary для UI/audit."""

    sequence: int
    task_id: str | None
    task_key: str | None
    selected_step_id: str | None
    planning_outcome: str
    validation_status: str | None
    execution_run_id: str | None
    started_at: str
    finished_at: str
    error_message: str | None = None


@dataclass(frozen=True)
class WorkflowRunRecord:
    """Persistent state одного асинхронного workflow run.

    Не путать с `ExecutionRequest` — то описывает один LLM-вызов в leaf-задаче.
    `WorkflowRunRecord` — это весь цикл `run_until_blocked`, который может
    включать множество ExecutionRequest'ов.
    """

    run_id: str
    project_id: str
    status: WorkflowRunStatus
    provider: str | None
    model: str | None
    max_steps: int
    current_step: int
    total_steps_completed: int
    started_at: str
    finished_at: str | None
    last_step_summary: str
    stop_reason: str | None
    error_message: str | None
    cancel_requested: bool
    steps: tuple[WorkflowStepRecord, ...] = field(default_factory=tuple)
