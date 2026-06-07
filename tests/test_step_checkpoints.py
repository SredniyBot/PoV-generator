"""Ролбек Ф1: чекпоинты шага + провенанс патчей состояния.

Фундамент отката: перед шагом снимается чекпоинт состояния, а патчи
knowledge/process тегируются id шага-источника (для будущего селективного
реплея). Интеграция (снятие во время stub-прогона) покрыта полным набором.
"""

from __future__ import annotations

import json
from pathlib import Path

from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.domain.process_state import UpsertReadinessPatch
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]


def _setup(tmp_path: Path):
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    snapshot, report = registry_service.validate()
    assert report.is_valid
    runtime = SqliteRuntime()
    ws = tmp_path / "ws"
    ProjectService(runtime).init_project(
        workspace=ws,
        name="T",
        objective_ref=ObjectRef.parse("common.requirements_specification@1.0.0"),
        request_text="req",
        domain_packs=(),
    )
    PlanningService(runtime).expand_graph(ws, snapshot)
    return ws, runtime


def test_capture_step_checkpoint_snapshots_prestate(tmp_path: Path) -> None:
    ws, runtime = _setup(tmp_path)
    leaf = next(t for t in runtime.list_tasks(ws) if t.template_type == "leaf")
    knowledge = runtime.load_knowledge(ws)
    process = runtime.load_process_state(ws)

    cp = runtime.capture_step_checkpoint(ws, leaf.task_id)
    assert cp.task_id == leaf.task_id
    assert cp.attempt == leaf.attempt
    assert cp.knowledge_version == knowledge.version
    assert cp.process_version == process.version
    assert cp.objective_ref == "common.requirements_specification@1.0.0"
    # блоб соответствует текущему снимку
    assert json.loads(cp.knowledge_json)["version"] == knowledge.version

    latest = runtime.load_latest_step_checkpoint(ws, leaf.task_id)
    assert latest is not None and latest.checkpoint_id == cp.checkpoint_id
    assert len(runtime.list_step_checkpoints(ws)) == 1


def test_latest_checkpoint_is_most_recent(tmp_path: Path) -> None:
    ws, runtime = _setup(tmp_path)
    leaf = next(t for t in runtime.list_tasks(ws) if t.template_type == "leaf")
    first = runtime.capture_step_checkpoint(ws, leaf.task_id)
    second = runtime.capture_step_checkpoint(ws, leaf.task_id)
    assert second.seq > first.seq
    assert runtime.load_latest_step_checkpoint(ws, leaf.task_id).checkpoint_id == second.checkpoint_id


def test_state_event_carries_task_id(tmp_path: Path) -> None:
    ws, runtime = _setup(tmp_path)
    runtime.apply_process_patch(
        ws,
        UpsertReadinessPatch(dimension="goal_clarity", status="ready", blocking=False),
        actor="t",
        reason="r",
        task_id="task-xyz",
    )
    events = runtime.list_state_events(ws, layer="process")
    assert events[-1].task_id == "task-xyz"
    assert events[-1].seq is not None


def test_state_event_without_task_id_is_none(tmp_path: Path) -> None:
    ws, runtime = _setup(tmp_path)
    runtime.apply_process_patch(
        ws,
        UpsertReadinessPatch(dimension="goal_clarity", status="ready", blocking=False),
        actor="t",
        reason="r",
    )
    assert runtime.list_state_events(ws, layer="process")[-1].task_id is None
