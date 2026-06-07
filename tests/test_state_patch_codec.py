"""Кодек патчей + реконструкция (Ф3a): golden-реплей и селективное исключение.

Главный риск ролбека — корректность реконструкции состояния. Тест гарантирует:
1. реплей всех патчей поверх базы даёт состояние, идентичное прямому применению
   (golden round-trip);
2. исключение патчей шага убирает ровно его эффекты, сохраняя остальные;
3. неизвестный патч → явная ошибка (не «тихо»).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pov_generator.application.project_service import ProjectService
from pov_generator.application.state_patch_codec import decode_patch, reconstruct_layers
from pov_generator.common.serialization import to_primitive
from pov_generator.domain.positions import Position
from pov_generator.domain.process_state import UpsertReadinessPatch
from pov_generator.domain.project_knowledge import UpsertPositionPatch
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime


def _setup(tmp_path: Path):
    runtime = SqliteRuntime()
    ws = tmp_path / "ws"
    ProjectService(runtime).init_project(
        workspace=ws,
        name="T",
        objective_ref=ObjectRef.parse("common.requirements_specification@1.0.0"),
        request_text="req",
        domain_packs=(),
    )
    return ws, runtime


def _apply_sample_patches(ws, runtime):
    """A: позиция x.p1 + readiness dim_a; B: readiness dim_b."""
    pos = Position(
        identifier="x.p1",
        type="fact",
        statement="s",
        visibility="technical",
        scope="global",
        source="artifact",
        taken_by="task:A",
        taken_at="2026-06-07T00:00:00+00:00",
    )
    runtime.apply_knowledge_patch(ws, UpsertPositionPatch(position=pos), actor="t", reason="r", task_id="A")
    runtime.apply_process_patch(
        ws, UpsertReadinessPatch(dimension="rb_dim_a", status="ready", blocking=False),
        actor="t", reason="r", task_id="A",
    )
    runtime.apply_process_patch(
        ws, UpsertReadinessPatch(dimension="rb_dim_b", status="ready", blocking=False),
        actor="t", reason="r", task_id="B",
    )


def test_reconstruct_replays_to_identical_state(tmp_path: Path) -> None:
    ws, runtime = _setup(tmp_path)
    base_k = runtime.load_knowledge(ws)
    base_p = runtime.load_process_state(ws)
    before = runtime.list_state_events(ws)
    base_seq = before[-1].seq if before else 0

    _apply_sample_patches(ws, runtime)
    live_k = runtime.load_knowledge(ws)
    live_p = runtime.load_process_state(ws)

    events = [e for e in runtime.list_state_events(ws) if e.seq and e.seq > base_seq]
    rk, rp = reconstruct_layers(base_k, base_p, events)
    assert to_primitive(rk) == to_primitive(live_k)
    assert to_primitive(rp) == to_primitive(live_p)


def test_reconstruct_excluding_task_drops_only_its_effects(tmp_path: Path) -> None:
    ws, runtime = _setup(tmp_path)
    base_k = runtime.load_knowledge(ws)
    base_p = runtime.load_process_state(ws)
    base_seq = (runtime.list_state_events(ws)[-1].seq if runtime.list_state_events(ws) else 0)

    _apply_sample_patches(ws, runtime)
    events = [e for e in runtime.list_state_events(ws) if e.seq and e.seq > base_seq]

    survivors = [e for e in events if e.task_id != "A"]
    rk, rp = reconstruct_layers(base_k, base_p, survivors)
    assert "x.p1" not in rk.positions          # позиция шага A ушла
    assert "rb_dim_a" not in rp.readiness        # readiness шага A ушёл
    assert "rb_dim_b" in rp.readiness            # readiness шага B сохранился


def test_decode_unknown_patch_raises() -> None:
    with pytest.raises(ValueError):
        decode_patch("process", "NopePatch", {})
    with pytest.raises(ValueError):
        decode_patch("weird_layer", "X", {})


def test_decode_roundtrip_readiness() -> None:
    patch = UpsertReadinessPatch(dimension="d", status="ready", blocking=True, evidence=("e1",))
    decoded = decode_patch("process", "UpsertReadinessPatch", to_primitive(patch))
    assert decoded.dimension == "d"
    assert decoded.status == "ready"
    assert tuple(decoded.evidence) == ("e1",)
