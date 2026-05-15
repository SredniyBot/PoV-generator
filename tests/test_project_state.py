"""Тесты ProjectManifest + ProjectState — композиции состояния проекта."""

from __future__ import annotations

import pytest

from pov_generator.domain.positions import Position
from pov_generator.domain.process_state import (
    ProcessState,
    SetClarificationModePatch,
    SetRootTaskPatch,
    apply_process_patch,
)
from pov_generator.domain.project_knowledge import (
    GOAL_POSITION_ID,
    ProjectKnowledge,
    UpsertPositionPatch,
    apply_knowledge_patch,
)
from pov_generator.domain.project_state import ProjectManifest, ProjectState, StateEvent


def _manifest() -> ProjectManifest:
    return ProjectManifest(
        project_id="proj-1",
        name="Demo",
        objective_ref="common.requirements_specification@1.0.0",
        business_request="Make a service for X.",
        created_at="2026-05-12T10:00:00+00:00",
    )


def _empty_state() -> ProjectState:
    return ProjectState(
        manifest=_manifest(),
        knowledge=ProjectKnowledge(),
        process=ProcessState(),
    )


def _goal_position() -> Position:
    return Position(
        identifier=GOAL_POSITION_ID,
        type="fact",
        statement="Validate hypothesis H.",
        visibility="principal",
        scope="global",
        source="user",
        taken_by="user:1",
        taken_at="2026-05-12T10:00:00+00:00",
    )


class TestManifest:
    def test_manifest_is_frozen(self) -> None:
        manifest = _manifest()
        with pytest.raises(Exception):
            manifest.project_id = "other"  # type: ignore[misc]


class TestStateEvent:
    def test_event_carries_layer_and_payload(self) -> None:
        event = StateEvent(
            layer="knowledge",
            version=1,
            patch_type="UpsertPositionPatch",
            payload={"position_id": "p1"},
            actor="system",
            reason="bootstrap",
            created_at="2026-05-12T10:00:00+00:00",
        )
        assert event.layer == "knowledge"
        assert event.payload["position_id"] == "p1"


class TestComposition:
    def test_snapshot_starts_at_zero_versions(self) -> None:
        state = _empty_state()
        assert state.snapshot_version == (0, 0)

    def test_knowledge_change_does_not_bump_process_version(self) -> None:
        state = _empty_state()
        new_knowledge = apply_knowledge_patch(
            state.knowledge, UpsertPositionPatch(_goal_position())
        )
        new_state = state.with_knowledge(new_knowledge)
        assert new_state.snapshot_version == (1, 0)

    def test_process_change_does_not_bump_knowledge_version(self) -> None:
        state = _empty_state()
        new_process = apply_process_patch(state.process, SetRootTaskPatch(task_id="root-1"))
        new_state = state.with_process(new_process)
        assert new_state.snapshot_version == (0, 1)

    def test_independent_layer_evolutions(self) -> None:
        state = _empty_state()
        new_knowledge = apply_knowledge_patch(
            state.knowledge, UpsertPositionPatch(_goal_position())
        )
        new_process = apply_process_patch(state.process, SetClarificationModePatch(mode="autopilot"))

        composed = state.with_knowledge(new_knowledge).with_process(new_process)

        assert composed.snapshot_version == (1, 1)
        assert composed.knowledge.goal_statement() == "Validate hypothesis H."
        assert composed.process.clarification_mode == "autopilot"
        assert composed.manifest == state.manifest

    def test_with_helpers_do_not_mutate_original(self) -> None:
        state = _empty_state()
        new_knowledge = apply_knowledge_patch(
            state.knowledge, UpsertPositionPatch(_goal_position())
        )
        state.with_knowledge(new_knowledge)

        assert state.snapshot_version == (0, 0)
        assert state.knowledge.get(GOAL_POSITION_ID) is None


class TestGoalConvention:
    def test_goal_accessor_returns_none_when_unset(self) -> None:
        knowledge = ProjectKnowledge()
        assert knowledge.goal() is None
        assert knowledge.goal_statement() is None

    def test_goal_accessor_returns_active_goal(self) -> None:
        knowledge = apply_knowledge_patch(
            ProjectKnowledge(), UpsertPositionPatch(_goal_position())
        )
        assert knowledge.goal_statement() == "Validate hypothesis H."

    def test_rejected_goal_is_not_visible(self) -> None:
        from pov_generator.domain.project_knowledge import RejectPositionPatch

        knowledge = apply_knowledge_patch(
            ProjectKnowledge(), UpsertPositionPatch(_goal_position())
        )
        knowledge = apply_knowledge_patch(
            knowledge, RejectPositionPatch(position_id=GOAL_POSITION_ID, reason="wrong direction")
        )
        assert knowledge.goal() is None
        assert knowledge.goal_statement() is None
