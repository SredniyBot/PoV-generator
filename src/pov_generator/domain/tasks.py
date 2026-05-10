from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..common.errors import ConflictError
from ..common.serialization import utc_now_iso
from .registry import ObjectRef


TaskStatus = Literal[
    "candidate",
    "ready",
    "blocked",
    "in_progress",
    "waiting_for_children",
    "completed",
    "failed",
    "skipped",
    "obsolete",
]
TaskCommand = Literal["start", "complete", "fail", "retry", "obsolete", "skip", "mark_ready", "mark_blocked"]
TaskOriginKind = Literal["objective_root", "base_child", "domain_contribution", "repair", "user_request", "system"]


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    project_id: str
    objective_ref: str
    parent_task_id: str | None
    template_ref: str
    template_type: str
    title: str
    status: TaskStatus
    origin_kind: TaskOriginKind
    origin_ref: str
    stable_key: str
    depth: int
    slot_id: str | None
    attempt: int
    error_message: str | None
    created_at: str
    updated_at: str

    @property
    def template_id(self) -> str:
        return ObjectRef.parse(self.template_ref).identifier

    @property
    def template_version(self) -> str:
        return ObjectRef.parse(self.template_ref).version

    @property
    def task_key(self) -> str:
        return self.stable_key.rsplit(":", 1)[-1]


@dataclass(frozen=True)
class TaskEvent:
    task_id: str
    event_type: str
    from_status: str | None
    to_status: str | None
    payload: dict[str, object]
    created_at: str


def initial_task_status(template_type: str) -> TaskStatus:
    if template_type == "composite":
        return "waiting_for_children"
    return "candidate"


def apply_task_command(task: TaskRecord, command: TaskCommand, *, error_message: str | None = None) -> TaskRecord:
    now = utc_now_iso()
    current = task.status
    if command == "mark_ready":
        if current in {"completed", "failed", "obsolete", "skipped", "in_progress"}:
            return task
        return TaskRecord(**{**task.__dict__, "status": "ready", "error_message": None, "updated_at": now})
    if command == "mark_blocked":
        if current in {"completed", "failed", "obsolete", "skipped", "in_progress"}:
            return task
        return TaskRecord(**{**task.__dict__, "status": "blocked", "error_message": error_message, "updated_at": now})
    if command == "start":
        if current not in {"ready", "candidate"}:
            raise ConflictError(f"Cannot start task from status '{current}'.")
        return TaskRecord(**{**task.__dict__, "status": "in_progress", "error_message": None, "updated_at": now})
    if command == "complete":
        if current not in {"ready", "candidate", "in_progress", "waiting_for_children"}:
            raise ConflictError(f"Cannot complete task from status '{current}'.")
        return TaskRecord(**{**task.__dict__, "status": "completed", "error_message": None, "updated_at": now})
    if command == "fail":
        if current in {"completed", "obsolete", "skipped"}:
            raise ConflictError(f"Cannot fail task from status '{current}'.")
        return TaskRecord(**{**task.__dict__, "status": "failed", "error_message": error_message, "updated_at": now})
    if command == "retry":
        if current != "failed":
            raise ConflictError(f"Cannot retry task from status '{current}'.")
        return TaskRecord(
            **{**task.__dict__, "status": "ready", "attempt": task.attempt + 1, "error_message": None, "updated_at": now}
        )
    if command == "obsolete":
        if current in {"completed", "obsolete"}:
            raise ConflictError(f"Cannot obsolete task from status '{current}'.")
        return TaskRecord(**{**task.__dict__, "status": "obsolete", "updated_at": now})
    if command == "skip":
        if current in {"completed", "obsolete"}:
            raise ConflictError(f"Cannot skip task from status '{current}'.")
        return TaskRecord(**{**task.__dict__, "status": "skipped", "error_message": error_message, "updated_at": now})
    raise TypeError(f"Unsupported task command: {command}")
