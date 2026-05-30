"""Тесты конкурентной записи в SqliteRuntime (фундамент параллельных шагов).

Параллельные воркеры мутируют один workspace одновременно. Эти тесты
доказывают, что per-workspace write-coordinator (@_serialized_write) делает
read-modify-write мутации атомарными — без потери обновлений, на которые
гонка обрекла бы apply_*_patch (load → compute → commit без блокировки).
"""

from __future__ import annotations

import threading
from pathlib import Path

from test_m9_api import build_services, init_project

from pov_generator.common.serialization import utc_now_iso
from pov_generator.domain.positions import Position
from pov_generator.domain.process_state import UpsertReadinessPatch
from pov_generator.domain.project_knowledge import UpsertPositionPatch


def _position(identifier: str) -> Position:
    return Position(
        identifier=identifier,
        type="fact",
        statement=f"statement for {identifier}",
        visibility="principal",
        scope="global",
        source="artifact",
        taken_by="test",
        taken_at=utc_now_iso(),
        tags=(),
    )


def test_concurrent_knowledge_patches_have_no_lost_updates(tmp_path: Path) -> None:
    workspace = tmp_path / "runtime" / "case"
    init_project(workspace, "Запрос для теста конкурентной записи знаний.")
    _registry, runtime, _ps, _pl, _ws = build_services()

    n = 24

    def apply(i: int) -> None:
        runtime.apply_knowledge_patch(
            workspace,
            UpsertPositionPatch(position=_position(f"test.pos.{i}")),
            actor="test",
            reason="concurrency",
        )

    threads = [threading.Thread(target=apply, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    knowledge = runtime.load_knowledge(workspace)
    # Все N позиций должны присутствовать — ни одна не «перетёрта» гонкой.
    for i in range(n):
        assert f"test.pos.{i}" in knowledge.positions, f"потеряна позиция {i}"


def test_concurrent_process_patches_have_no_lost_updates(tmp_path: Path) -> None:
    workspace = tmp_path / "runtime" / "case"
    init_project(workspace, "Запрос для теста конкурентной записи процесса.")
    _registry, runtime, _ps, _pl, _ws = build_services()

    n = 24

    def apply(i: int) -> None:
        runtime.apply_process_patch(
            workspace,
            UpsertReadinessPatch(
                dimension=f"test_dim_{i}",
                status="ready",
                blocking=False,
                confidence=1.0,
            ),
            actor="test",
            reason="concurrency",
        )

    threads = [threading.Thread(target=apply, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    process = runtime.load_process_state(workspace)
    for i in range(n):
        assert f"test_dim_{i}" in process.readiness, f"потеряна readiness-dimension {i}"
