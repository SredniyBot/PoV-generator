"""Движок ролбека (Ф3b): откат реального stub-прогона к раннему шагу.

Прогоняем несколько шагов стабом (создаются чекпоинты, артефакты, изменения
состояния), откатываемся к раннему шагу и проверяем: целевой шаг и зависимые
инвалидированы, их артефакты архивированы (не видны в активных, видны в архиве),
задачи сброшены, состояние читается без ошибок.
"""

from __future__ import annotations

from pathlib import Path

from test_m9_api import OBJECTIVE_REF, build_services  # type: ignore

from pov_generator.application.rollback_service import RollbackService
from pov_generator.domain.registry import ObjectRef


def _run_partial(tmp_path: Path):
    registry_service, runtime, project_service, planning_service, workflow_service = build_services()
    snapshot, report = registry_service.validate()
    assert report.is_valid
    ws = tmp_path / "ws"
    project_service.init_project(
        workspace=ws,
        name="T",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="Нужно ТЗ для CRM-интеграции.",
        domain_packs=(),
    )
    planning_service.expand_graph(ws, snapshot)
    workflow_service.run_until_blocked(ws, snapshot, provider="stub", max_steps=4)
    return ws, snapshot, runtime


def test_rollback_reverts_target_and_archives_artifacts(tmp_path: Path) -> None:
    ws, snapshot, runtime = _run_partial(tmp_path)

    checkpoints = runtime.list_step_checkpoints(ws)
    assert len(checkpoints) >= 2, "должно выполниться несколько шагов"
    target = checkpoints[0].task_id  # самый ранний выполненный шаг

    active_before = {a.artifact_id for a in runtime.list_artifacts(ws)}
    assert active_before, "до отката есть активные артефакты"

    result = RollbackService(runtime).rollback_to(ws, snapshot, target, actor="user", reason="t")

    # целевой шаг — в множестве откаченных
    assert target in result.reverted_task_ids
    # его артефакты были активны и теперь архивированы
    assert result.archived_artifact_ids
    assert set(result.archived_artifact_ids) <= active_before
    active_after = {a.artifact_id for a in runtime.list_artifacts(ws)}
    assert not (set(result.archived_artifact_ids) & active_after)
    # но видны в архив-вьюхе
    archived_view = {a.artifact_id for a in runtime.list_artifacts(ws, include_rolled_back=True)}
    assert set(result.archived_artifact_ids) <= archived_view

    # целевая задача сброшена в исходный статус (лист → candidate)
    assert runtime.get_task(ws, target).status == "candidate"

    # состояние читается без ошибок после реконструкции
    state = runtime.load_project_state(ws)
    assert state.knowledge.version >= 1


def test_rollback_to_unexecuted_step_raises(tmp_path: Path) -> None:
    import pytest

    from pov_generator.common.errors import ConflictError

    ws, snapshot, runtime = _run_partial(tmp_path)
    # задача без чекпоинта (ещё не исполнялась) — откатывать нечего
    not_run = next(
        t
        for t in runtime.list_tasks(ws)
        if runtime.load_latest_step_checkpoint(ws, t.task_id) is None
    )
    with pytest.raises(ConflictError):
        RollbackService(runtime).rollback_to(ws, snapshot, not_run.task_id)
