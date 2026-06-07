"""Доменные модели ролбека шага.

Чекпоинт — снимок состояния (knowledge/process) ПЕРЕД выполнением листового
шага. База для отката: реконструкция состояния реплеем переживших патчей
поверх чекпоинта самого раннего откаченного шага.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StepCheckpoint:
    """Состояние проекта ПЕРЕД выполнением шага (для ролбека)."""

    checkpoint_id: str
    project_id: str
    task_id: str
    attempt: int
    seq: int  # монотонный порядок (autoincrement)
    knowledge_json: str
    knowledge_version: int
    process_json: str
    process_version: int
    objective_ref: str
    created_at: str


@dataclass(frozen=True)
class RollbackRecord:
    """Аудит выполненного отката."""

    rollback_id: str
    project_id: str
    target_task_id: str
    target_seq: int
    reverted_task_ids: tuple[str, ...]
    archived_artifact_ids: tuple[str, ...]
    actor: str
    reason: str
    created_at: str


@dataclass(frozen=True)
class RollbackResult:
    """Итог отката (для API/UI)."""

    rollback_id: str
    target_task_id: str
    reverted_task_ids: tuple[str, ...]
    archived_artifact_ids: tuple[str, ...]
    restored_objective_ref: str
