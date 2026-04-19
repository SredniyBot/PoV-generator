from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pov_generator.common.errors import ConflictError
from pov_generator.common.serialization import utc_now_iso


TaskStatus = Literal["queued", "in_progress", "waiting_for_children", "completed", "failed", "obsolete"]
TaskCommand = Literal["start", "complete", "fail", "retry", "obsolete", "mark_waiting", "requeue_finalization"]
RecipeProgressStatus = Literal["pending", "materialized", "in_progress", "completed", "failed", "obsolete"]


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    project_id: str
    template_id: str
    template_version: str
    template_type: str
    template_role: str
    recipe_id: str
    recipe_version: str
    recipe_step_id: str
    task_family_key: str
    status: TaskStatus
    attempt: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TaskEvent:
    task_id: str
    event_type: str
    from_status: str | None
    to_status: str | None
    payload: dict[str, object]
    created_at: str


@dataclass(frozen=True)
class TaskRecipeProgress:
    project_id: str
    recipe_id: str
    recipe_version: str
    recipe_step_id: str
    status: RecipeProgressStatus
    task_id: str | None
    updated_at: str
    note: str | None = None


def initial_task_status(template_type: str) -> TaskStatus:
    if template_type == "composite":
        return "waiting_for_children"
    return "queued"


def apply_task_command(task: TaskRecord, command: TaskCommand) -> TaskRecord:
    now = utc_now_iso()
    current = task.status
    if command == "start":
        if current != "queued":
            raise ConflictError(f"Cannot start task from status '{current}'.")
        return TaskRecord(**{**task.__dict__, "status": "in_progress", "updated_at": now})
    if command == "complete":
        if current not in {"queued", "in_progress", "waiting_for_children"}:
            raise ConflictError(f"Cannot complete task from status '{current}'.")
        return TaskRecord(**{**task.__dict__, "status": "completed", "updated_at": now})
    if command == "fail":
        if current not in {"queued", "in_progress", "waiting_for_children"}:
            raise ConflictError(f"Cannot fail task from status '{current}'.")
        return TaskRecord(**{**task.__dict__, "status": "failed", "updated_at": now})
    if command == "retry":
        if current != "failed":
            raise ConflictError(f"Cannot retry task from status '{current}'.")
        return TaskRecord(
            **{**task.__dict__, "status": "queued", "attempt": task.attempt + 1, "updated_at": now}
        )
    if command == "obsolete":
        if current in {"completed", "obsolete"}:
            raise ConflictError(f"Cannot obsolete task from status '{current}'.")
        return TaskRecord(**{**task.__dict__, "status": "obsolete", "updated_at": now})
    if command == "mark_waiting":
        if current != "in_progress" or task.template_type != "dynamic":
            raise ConflictError("Only dynamic in-progress tasks can move to waiting_for_children.")
        return TaskRecord(**{**task.__dict__, "status": "waiting_for_children", "updated_at": now})
    if command == "requeue_finalization":
        if current != "waiting_for_children" or task.template_type != "dynamic":
            raise ConflictError("Only dynamic waiting tasks can requeue for finalization.")
        return TaskRecord(**{**task.__dict__, "status": "queued", "updated_at": now})
    raise TypeError(f"Unsupported task command: {command}")


def recipe_progress_status_for_task(status: TaskStatus) -> RecipeProgressStatus:
    if status == "queued":
        return "materialized"
    if status == "in_progress":
        return "in_progress"
    if status == "waiting_for_children":
        return "in_progress"
    if status == "completed":
        return "completed"
    if status == "failed":
        return "failed"
    return "obsolete"
