"""Движок ролбека шага (Ф3b): откат к состоянию ДО выбранного шага.

Семантика — «только зависимые»: откатывается целевой шаг и все транзитивно
зависящие от него; независимые ветки сохраняются. Состояние реконструируется
реплеем переживших патчей поверх чекпоинта самого раннего откаченного шага;
артефакты/решения откаченных шагов архивируются (не удаляются); задачи
сбрасываются в исходный статус. Шлюз/конкуррентность — отдельно (Ф4).
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

from ..common.errors import ConflictError
from ..common.serialization import utc_now_iso
from ..domain.registry import RegistrySnapshot
from ..domain.rollback import RollbackRecord, RollbackResult
from ..infrastructure.sqlite_runtime import SqliteRuntime
from .rollback_graph import collect_step_footprints, compute_rollback_set
from .state_patch_codec import reconstruct_layers


class RollbackService:
    def __init__(self, runtime: SqliteRuntime) -> None:
        self._runtime = runtime

    def preview(
        self, workspace: Path, snapshot: RegistrySnapshot, target_task_id: str
    ) -> set[str]:
        """Множество шагов, которые будут инвалидированы (для превью до отката)."""
        footprints = collect_step_footprints(self._runtime, workspace, snapshot)
        return compute_rollback_set(target_task_id, footprints)

    def rollback_to(
        self,
        workspace: Path,
        snapshot: RegistrySnapshot,
        target_task_id: str,
        *,
        actor: str = "user",
        reason: str = "",
    ) -> RollbackResult:
        manifest = self._runtime.load_manifest(workspace)

        # Последний чекпоинт на задачу (актуальный pre-state её исполнения).
        checkpoint_by_task = {}
        for checkpoint in self._runtime.list_step_checkpoints(workspace):
            prev = checkpoint_by_task.get(checkpoint.task_id)
            if prev is None or checkpoint.seq > prev.seq:
                checkpoint_by_task[checkpoint.task_id] = checkpoint
        if target_task_id not in checkpoint_by_task:
            raise ConflictError("Шаг ещё не выполнялся — откатывать нечего.")

        footprints = collect_step_footprints(self._runtime, workspace, snapshot)
        reverted = compute_rollback_set(target_task_id, footprints)

        # Чекпоинт самого раннего откаченного шага — база реконструкции.
        reverted_checkpoints = [
            checkpoint_by_task[tid] for tid in reverted if tid in checkpoint_by_task
        ]
        earliest = min(reverted_checkpoints, key=lambda c: c.seq)

        base_knowledge = self._runtime.knowledge_from_json(earliest.knowledge_json)
        base_process = self._runtime.process_from_json(earliest.process_json)

        # Пережившие патчи: ПОСЛЕ базового чекпоинта (по версии своего слоя —
        # счётчики state_events.id и step_checkpoints.seq разные, поэтому
        # сравниваем именно версии knowledge/process), не аннулированные и не из
        # откаченных шагов. Порядок применения — порядок лога (list ordered by id).
        survivors = [
            event
            for event in self._runtime.list_state_events(workspace)
            if event.rolled_back_by is None
            and (event.task_id is None or event.task_id not in reverted)
            and (
                (event.layer == "knowledge" and event.version > earliest.knowledge_version)
                or (event.layer == "process" and event.version > earliest.process_version)
            )
        ]
        knowledge, process = reconstruct_layers(base_knowledge, base_process, survivors)

        rollback_id = str(uuid.uuid4())
        rollback_reason = reason or f"rollback to {target_task_id}"

        # Восстанавливаем состояние, аннулируем патчи откаченных шагов,
        # архивируем их артефакты/решения.
        self._runtime.write_state_snapshots(
            workspace, knowledge, process, actor=actor, reason=rollback_reason
        )
        reverted_tuple = tuple(sorted(reverted))
        self._runtime.void_state_events_for_tasks(workspace, reverted_tuple, rollback_id)
        archived = self._runtime.archive_artifacts_for_tasks(workspace, reverted_tuple, rollback_id)
        self._runtime.archive_decisions_for_tasks(workspace, reverted_tuple, rollback_id)

        # Сброс задач: откаченные + структурные родители (композиты/веера) —
        # чтобы планировщик пере-оценил и перезапустил.
        for task_id in self._tasks_to_reset(workspace, reverted):
            self._runtime.transition_task(workspace, task_id, "rollback_reset")

        # Кросс-objective откат: вернуть активный objective, если он сменился.
        restored_objective = earliest.objective_ref
        if restored_objective != manifest.objective_ref:
            self._runtime.update_manifest(
                workspace, replace(manifest, objective_ref=restored_objective)
            )

        self._runtime.record_rollback(
            workspace,
            RollbackRecord(
                rollback_id=rollback_id,
                project_id=manifest.project_id,
                target_task_id=target_task_id,
                target_seq=checkpoint_by_task[target_task_id].seq,
                reverted_task_ids=reverted_tuple,
                archived_artifact_ids=tuple(archived),
                actor=actor,
                reason=rollback_reason,
                created_at=utc_now_iso(),
            ),
        )
        return RollbackResult(
            rollback_id=rollback_id,
            target_task_id=target_task_id,
            reverted_task_ids=reverted_tuple,
            archived_artifact_ids=tuple(archived),
            restored_objective_ref=restored_objective,
        )

    def _tasks_to_reset(self, workspace: Path, reverted: set[str]) -> list[str]:
        """Откаченные задачи + их структурные родители (по цепочке parent)."""
        tasks = {task.task_id: task for task in self._runtime.list_tasks(workspace)}
        to_reset: set[str] = set(reverted)
        for task_id in tuple(reverted):
            current = tasks.get(task_id)
            while current is not None and current.parent_task_id:
                parent = tasks.get(current.parent_task_id)
                if parent is None:
                    break
                to_reset.add(parent.task_id)
                current = parent
        return [task_id for task_id in to_reset if task_id in tasks]
