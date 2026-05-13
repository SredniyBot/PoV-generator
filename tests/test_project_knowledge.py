"""Тесты ProjectKnowledge — Layer A в новой архитектуре состояния.

Покрывает: проекции, патчи (upsert/supersede/reject/elevate),
инкремент версии, иммутабельность, идемпотентность reject.
"""

from __future__ import annotations

import pytest

from pov_generator.common.errors import ConflictError, NotFoundError
from pov_generator.domain.positions import Position
from pov_generator.domain.project_knowledge import (
    ElevateVisibilityPatch,
    ProjectKnowledge,
    RejectPositionPatch,
    SupersedePositionPatch,
    UpsertPositionPatch,
    apply_knowledge_patch,
)


def _pos(identifier: str, **overrides) -> Position:
    defaults = dict(
        identifier=identifier,
        type="fact",
        statement=f"Statement for {identifier}",
        visibility="architectural",
        scope="global",
        source="system",
        taken_by="system",
        taken_at="2026-05-12T10:00:00+00:00",
    )
    defaults.update(overrides)
    return Position(**defaults)


class TestProjections:
    def test_empty_knowledge_has_no_positions(self) -> None:
        knowledge = ProjectKnowledge()
        assert list(knowledge.active()) == []
        assert knowledge.get("missing") is None

    def test_must_get_raises_when_missing(self) -> None:
        knowledge = ProjectKnowledge()
        with pytest.raises(NotFoundError):
            knowledge.must_get("missing")

    def test_by_type_filters_by_type_and_status(self) -> None:
        knowledge = _build_knowledge(
            _pos("f1", type="fact"),
            _pos("a1", type="assumption"),
            _pos("d1", type="decision"),
        )
        facts = list(knowledge.by_type("fact"))
        assert [p.identifier for p in facts] == ["f1"]

    def test_by_visibility_filters_by_level(self) -> None:
        knowledge = _build_knowledge(
            _pos("p1", visibility="principal"),
            _pos("a1", visibility="architectural"),
            _pos("t1", visibility="technical"),
        )
        principals = list(knowledge.by_visibility("principal"))
        assert [p.identifier for p in principals] == ["p1"]

    def test_by_scope_filters_by_scope(self) -> None:
        knowledge = _build_knowledge(
            _pos("g1", scope="global"),
            _pos("d1", scope="domain"),
            _pos("l1", scope="local"),
        )
        globals_ = list(knowledge.by_scope("global"))
        assert [p.identifier for p in globals_] == ["g1"]

    def test_by_tag_matches_membership(self) -> None:
        knowledge = _build_knowledge(
            _pos("t1", tags=("business", "kpi")),
            _pos("t2", tags=("tech_stack",)),
        )
        business = list(knowledge.by_tag("business"))
        assert [p.identifier for p in business] == ["t1"]


class TestUpsertPatch:
    def test_upsert_adds_new_position(self) -> None:
        knowledge = ProjectKnowledge()
        position = _pos("pos.a")

        updated = apply_knowledge_patch(knowledge, UpsertPositionPatch(position))

        assert updated.version == 1
        assert updated.must_get("pos.a") is position

    def test_upsert_replaces_existing_position(self) -> None:
        knowledge = _build_knowledge(_pos("pos.a", statement="v1"))
        updated_position = _pos("pos.a", statement="v2")

        updated = apply_knowledge_patch(knowledge, UpsertPositionPatch(updated_position))

        assert updated.must_get("pos.a").statement == "v2"
        assert updated.version == knowledge.version + 1

    def test_upsert_does_not_mutate_input(self) -> None:
        knowledge = ProjectKnowledge()
        apply_knowledge_patch(knowledge, UpsertPositionPatch(_pos("pos.a")))
        assert knowledge.version == 0
        assert knowledge.get("pos.a") is None


class TestSupersedePatch:
    def test_supersede_marks_old_and_links_new(self) -> None:
        knowledge = _build_knowledge(_pos("pos.a", statement="v1"))
        new_position = _pos("pos.a", statement="v2")

        updated = apply_knowledge_patch(
            knowledge, SupersedePositionPatch(old_position_id="pos.a", new_position=new_position)
        )

        new_active = updated.must_get("pos.a")
        assert new_active.statement == "v2"
        assert new_active.status == "active"
        assert new_active.supersedes == "pos.a"

    def test_supersede_missing_old_raises_not_found(self) -> None:
        knowledge = ProjectKnowledge()
        with pytest.raises(NotFoundError):
            apply_knowledge_patch(
                knowledge,
                SupersedePositionPatch(old_position_id="missing", new_position=_pos("missing")),
            )

    def test_supersede_non_active_raises_conflict(self) -> None:
        # Сначала отклонить положение, затем попытаться superseded.
        knowledge = _build_knowledge(_pos("pos.a"))
        knowledge = apply_knowledge_patch(
            knowledge, RejectPositionPatch(position_id="pos.a", reason="wrong")
        )
        with pytest.raises(ConflictError):
            apply_knowledge_patch(
                knowledge,
                SupersedePositionPatch(old_position_id="pos.a", new_position=_pos("pos.a")),
            )


class TestRejectPatch:
    def test_reject_marks_position(self) -> None:
        knowledge = _build_knowledge(_pos("pos.a"))

        updated = apply_knowledge_patch(
            knowledge, RejectPositionPatch(position_id="pos.a", reason="contradicts brief")
        )

        rejected = updated.must_get("pos.a")
        assert rejected.status == "rejected"
        assert rejected.rejection_reason == "contradicts brief"

    def test_reject_missing_raises_not_found(self) -> None:
        knowledge = ProjectKnowledge()
        with pytest.raises(NotFoundError):
            apply_knowledge_patch(
                knowledge, RejectPositionPatch(position_id="missing", reason="x")
            )

    def test_reject_is_idempotent_for_same_reason(self) -> None:
        knowledge = _build_knowledge(_pos("pos.a"))
        knowledge = apply_knowledge_patch(
            knowledge, RejectPositionPatch(position_id="pos.a", reason="r")
        )
        version_after_first = knowledge.version

        knowledge = apply_knowledge_patch(
            knowledge, RejectPositionPatch(position_id="pos.a", reason="r")
        )

        assert knowledge.version == version_after_first  # без инкремента

    def test_reject_with_different_reason_raises_conflict(self) -> None:
        knowledge = _build_knowledge(_pos("pos.a"))
        knowledge = apply_knowledge_patch(
            knowledge, RejectPositionPatch(position_id="pos.a", reason="r1")
        )
        with pytest.raises(ConflictError):
            apply_knowledge_patch(
                knowledge, RejectPositionPatch(position_id="pos.a", reason="r2")
            )


class TestElevateVisibilityPatch:
    def test_elevate_raises_visibility(self) -> None:
        knowledge = _build_knowledge(_pos("pos.a", visibility="technical"))

        updated = apply_knowledge_patch(
            knowledge,
            ElevateVisibilityPatch(position_id="pos.a", new_level="architectural"),
        )

        assert updated.must_get("pos.a").visibility == "architectural"

    def test_elevate_to_same_level_raises_conflict(self) -> None:
        knowledge = _build_knowledge(_pos("pos.a", visibility="architectural"))
        with pytest.raises(ConflictError, match="elevation"):
            apply_knowledge_patch(
                knowledge,
                ElevateVisibilityPatch(position_id="pos.a", new_level="architectural"),
            )

    def test_elevate_to_lower_level_raises_conflict(self) -> None:
        knowledge = _build_knowledge(_pos("pos.a", visibility="principal"))
        with pytest.raises(ConflictError):
            apply_knowledge_patch(
                knowledge,
                ElevateVisibilityPatch(position_id="pos.a", new_level="architectural"),
            )

    def test_elevate_missing_raises_not_found(self) -> None:
        knowledge = ProjectKnowledge()
        with pytest.raises(NotFoundError):
            apply_knowledge_patch(
                knowledge,
                ElevateVisibilityPatch(position_id="missing", new_level="principal"),
            )


class TestUnknownPatch:
    def test_unknown_patch_raises_type_error(self) -> None:
        knowledge = ProjectKnowledge()
        with pytest.raises(TypeError):
            apply_knowledge_patch(knowledge, object())  # type: ignore[arg-type]


# --- helpers -----------------------------------------------------------------


def _build_knowledge(*positions: Position) -> ProjectKnowledge:
    """Собрать ProjectKnowledge с набором положений (через последовательные upsert'ы)."""
    knowledge = ProjectKnowledge()
    for position in positions:
        knowledge = apply_knowledge_patch(knowledge, UpsertPositionPatch(position))
    return knowledge
