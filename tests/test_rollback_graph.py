"""Граф зависимостей ролбека (Ф2): транзитивное замыкание + ключи патчей."""

from __future__ import annotations

from pov_generator.application.rollback_graph import (
    StepFootprint,
    _patch_read_keys,
    _patch_write_keys,
    compute_rollback_set,
)


def _fp(task_id, seq, writes=(), reads=()):
    return StepFootprint(task_id, seq, frozenset(writes), frozenset(reads))


# --- транзитивное замыкание -------------------------------------------------


def test_chain_rollback_includes_all_downstream():
    # A → B → C (через артефакты)
    fps = [
        _fp("A", 1, writes={("artifact", "a1")}),
        _fp("B", 2, writes={("artifact", "a2")}, reads={("artifact", "a1")}),
        _fp("C", 3, reads={("artifact", "a2")}),
    ]
    assert compute_rollback_set("A", fps) == {"A", "B", "C"}
    assert compute_rollback_set("B", fps) == {"B", "C"}
    assert compute_rollback_set("C", fps) == {"C"}


def test_independent_branch_survives():
    # A → B; C независим (читает другой артефакт), выполнен после A
    fps = [
        _fp("A", 1, writes={("artifact", "a1")}),
        _fp("B", 2, reads={("artifact", "a1")}),
        _fp("C", 3, writes={("artifact", "a3")}, reads={("artifact", "x")}),
    ]
    assert compute_rollback_set("A", fps) == {"A", "B"}  # C выживает


def test_readiness_dependency_via_contract():
    # A выставил readiness R; B потребовал R (контракт) → зависит
    fps = [
        _fp("A", 1, writes={("readiness", "goal_clarity")}),
        _fp("B", 2, reads={("readiness", "goal_clarity")}),
    ]
    assert compute_rollback_set("A", fps) == {"A", "B"}


def test_order_matters_earlier_does_not_depend_on_later():
    # B выполнен раньше A — даже при пересечении B не зависит от A
    fps = [
        _fp("B", 1, reads={("artifact", "a1")}),
        _fp("A", 2, writes={("artifact", "a1")}),
    ]
    assert compute_rollback_set("A", fps) == {"A"}  # B раньше → не затронут


def test_diamond_closure():
    # A → B, A → C, B&C → D
    fps = [
        _fp("A", 1, writes={("artifact", "a")}),
        _fp("B", 2, writes={("artifact", "b")}, reads={("artifact", "a")}),
        _fp("C", 3, writes={("artifact", "c")}, reads={("artifact", "a")}),
        _fp("D", 4, reads={("artifact", "b"), ("artifact", "c")}),
    ]
    assert compute_rollback_set("A", fps) == {"A", "B", "C", "D"}
    assert compute_rollback_set("B", fps) == {"B", "D"}


def test_target_without_footprint_returns_singleton():
    assert compute_rollback_set("Z", [_fp("A", 1)]) == {"Z"}


# --- ключи патчей -----------------------------------------------------------


def test_patch_write_keys():
    assert _patch_write_keys("UpsertReadinessPatch", {"dimension": "goal_clarity"}) == {
        ("readiness", "goal_clarity")
    }
    assert _patch_write_keys("CloseGapPatch", {"gap_id": "g1"}) == {("gap", "g1")}
    assert _patch_write_keys("UpsertPositionPatch", {"position": {"identifier": "p1"}}) == {
        ("position", "p1")
    }
    assert _patch_write_keys(
        "SupersedePositionPatch", {"new_position": {"identifier": "p2"}, "old_position_id": "p1"}
    ) == {("position", "p2")}
    assert _patch_write_keys("SetClarificationModePatch", {"mode": "balanced"}) == set()


def test_patch_read_keys_supersede_reads_old():
    assert _patch_read_keys(
        "SupersedePositionPatch", {"new_position": {"identifier": "p2"}, "old_position_id": "p1"}
    ) == {("position", "p1")}
    assert _patch_read_keys("UpsertReadinessPatch", {"dimension": "x"}) == set()
