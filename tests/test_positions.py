"""Тесты Position и связанных типов.

Покрывает инварианты создания положения и порядок уровней видимости.
"""

from __future__ import annotations

import pytest

from pov_generator.domain.positions import (
    Position,
    PositionAlternative,
    visibility_rank,
)


def _make_position(**overrides) -> Position:
    """Минимальный валидный Position с возможностью переопределить поля."""
    defaults = dict(
        identifier="pos.test",
        type="fact",
        statement="Test statement",
        visibility="architectural",
        scope="global",
        source="system",
        taken_by="system",
        taken_at="2026-05-12T10:00:00+00:00",
    )
    defaults.update(overrides)
    return Position(**defaults)


class TestPositionValidation:
    def test_minimal_position_can_be_created(self) -> None:
        position = _make_position()
        assert position.identifier == "pos.test"
        assert position.confidence == 1.0
        assert position.status == "active"
        assert position.tags == ()

    def test_confidence_must_be_in_unit_interval(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            _make_position(confidence=1.5)

        with pytest.raises(ValueError, match="confidence"):
            _make_position(confidence=-0.1)

    def test_superseded_position_requires_superseded_at(self) -> None:
        with pytest.raises(ValueError, match="superseded_at"):
            _make_position(status="superseded")

    def test_rejected_position_requires_rejection_reason(self) -> None:
        with pytest.raises(ValueError, match="rejection_reason"):
            _make_position(status="rejected")

    def test_position_is_frozen(self) -> None:
        position = _make_position()
        with pytest.raises(Exception):
            position.statement = "mutated"  # type: ignore[misc]

    def test_alternative_is_frozen(self) -> None:
        alt = PositionAlternative(label="A", rationale="r", rejected_reason="x")
        with pytest.raises(Exception):
            alt.label = "B"  # type: ignore[misc]


class TestVisibilityRank:
    def test_principal_is_highest(self) -> None:
        assert visibility_rank("principal") > visibility_rank("architectural")
        assert visibility_rank("architectural") > visibility_rank("technical")

    def test_all_levels_have_distinct_ranks(self) -> None:
        ranks = {
            visibility_rank("principal"),
            visibility_rank("architectural"),
            visibility_rank("technical"),
        }
        assert len(ranks) == 3
