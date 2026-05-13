"""Тесты ProcessState — Layer B в новой архитектуре состояния.

Покрывает: патчи слоя процесса, проекции активных паков, алинейку
engagement-видимости.
"""

from __future__ import annotations

import pytest

from pov_generator.common.errors import ConflictError, NotFoundError
from pov_generator.domain.process_state import (
    ActivateDomainPackPatch,
    ActivateMethodologyPackPatch,
    CloseGapPatch,
    DetectDomainSignalPatch,
    DisableDomainPackPatch,
    DisableMethodologyPackPatch,
    ProcessState,
    SetClarificationModePatch,
    SetRootTaskPatch,
    UpsertGapPatch,
    UpsertReadinessPatch,
    apply_process_patch,
    proactive_ask_levels,
)


class TestEmptyState:
    def test_default_process_state(self) -> None:
        state = ProcessState()
        assert state.root_task_id is None
        assert state.active_gaps == {}
        assert state.readiness == {}
        assert state.clarification_mode == "balanced"
        assert state.version == 0


class TestEngagementVisibilityAlignment:
    @pytest.mark.parametrize(
        "mode,expected_ask",
        [
            ("autopilot", {"principal"}),
            ("balanced", {"principal", "architectural"}),
            ("control", {"principal", "architectural"}),
            ("expert", {"principal", "architectural", "technical"}),
        ],
    )
    def test_proactive_ask_levels_per_mode(self, mode, expected_ask) -> None:
        assert set(proactive_ask_levels(mode)) == expected_ask

    def test_autopilot_asks_principal_only(self) -> None:
        state = ProcessState(clarification_mode="autopilot")
        assert state.should_ask_user_for("principal") is True
        assert state.should_ask_user_for("architectural") is False
        assert state.should_ask_user_for("technical") is False

    def test_expert_asks_all_levels(self) -> None:
        state = ProcessState(clarification_mode="expert")
        assert state.should_ask_user_for("principal") is True
        assert state.should_ask_user_for("architectural") is True
        assert state.should_ask_user_for("technical") is True


class TestRootTaskPatch:
    def test_set_root_task(self) -> None:
        state = ProcessState()
        state = apply_process_patch(state, SetRootTaskPatch(task_id="task-1"))
        assert state.root_task_id == "task-1"
        assert state.version == 1


class TestGapPatches:
    def test_upsert_gap_opens_record(self) -> None:
        state = ProcessState()
        state = apply_process_patch(
            state,
            UpsertGapPatch(
                gap_id="gap.scope",
                title="Не определена область",
                description="...",
                severity="high",
            ),
        )
        assert "gap.scope" in state.active_gaps
        assert state.active_gaps["gap.scope"].severity == "high"
        assert state.active_gaps["gap.scope"].blocking is True
        assert "gap.scope" in state.blocking_gaps

    def test_close_gap_removes_record(self) -> None:
        state = ProcessState()
        state = apply_process_patch(
            state,
            UpsertGapPatch(gap_id="g1", title="t", description="d"),
        )
        state = apply_process_patch(state, CloseGapPatch(gap_id="g1"))
        assert state.active_gaps == {}

    def test_close_missing_gap_raises_not_found(self) -> None:
        state = ProcessState()
        with pytest.raises(NotFoundError):
            apply_process_patch(state, CloseGapPatch(gap_id="missing"))


class TestReadinessPatch:
    def test_upsert_readiness(self) -> None:
        state = ProcessState()
        state = apply_process_patch(
            state,
            UpsertReadinessPatch(
                dimension="scope",
                status="ready",
                blocking=False,
                confidence=0.9,
                evidence=("artifact:scope_matrix",),
            ),
        )
        assert state.readiness["scope"].status == "ready"
        assert state.readiness["scope"].confidence == 0.9

    def test_invalid_confidence_raises_conflict(self) -> None:
        state = ProcessState()
        with pytest.raises(ConflictError):
            apply_process_patch(
                state,
                UpsertReadinessPatch(
                    dimension="x", status="partial", blocking=False, confidence=1.5
                ),
            )


class TestDomainPackPatches:
    def test_activate_domain_pack(self) -> None:
        state = ProcessState()
        state = apply_process_patch(
            state,
            ActivateDomainPackPatch(
                pack_ref="ml.predictive_analytics@1.0.0",
                domain="ml",
                rationale="brief mentions forecasting",
            ),
        )
        active = state.active_domain_pack_records
        assert "ml.predictive_analytics@1.0.0" in active
        assert active["ml.predictive_analytics@1.0.0"].status == "active"

    def test_disable_domain_pack_keeps_record_with_disabled_status(self) -> None:
        state = ProcessState()
        state = apply_process_patch(
            state,
            ActivateDomainPackPatch(pack_ref="ml.x@1.0.0", domain="ml"),
        )
        state = apply_process_patch(state, DisableDomainPackPatch(pack_ref="ml.x@1.0.0"))

        # Запись осталась со статусом disabled, но не в проекции активных.
        assert state.active_domain_packs["ml.x@1.0.0"].status == "disabled"
        assert state.active_domain_pack_records == {}

    def test_disable_unknown_pack_raises_not_found(self) -> None:
        state = ProcessState()
        with pytest.raises(NotFoundError):
            apply_process_patch(state, DisableDomainPackPatch(pack_ref="missing"))


class TestMethodologyPackPatches:
    def test_activate_methodology(self) -> None:
        state = ProcessState()
        state = apply_process_patch(
            state,
            ActivateMethodologyPackPatch(pack_ref="process.lean_jtbd@1.0.0"),
        )
        assert state.active_methodology_pack_records["process.lean_jtbd@1.0.0"].status == "active"

    def test_disable_methodology_unknown_raises_not_found(self) -> None:
        state = ProcessState()
        with pytest.raises(NotFoundError):
            apply_process_patch(state, DisableMethodologyPackPatch(pack_ref="missing"))


class TestDomainSignalAndMode:
    def test_detect_signal_records_with_compound_key(self) -> None:
        state = ProcessState()
        state = apply_process_patch(
            state,
            DetectDomainSignalPatch(
                domain="ml", signal="прогноз", source="llm_detector", confidence=0.8
            ),
        )
        assert "ml:прогноз" in state.domain_signals

    def test_set_clarification_mode(self) -> None:
        state = ProcessState(clarification_mode="balanced")
        state = apply_process_patch(state, SetClarificationModePatch(mode="autopilot"))
        assert state.clarification_mode == "autopilot"


class TestVersionAndImmutability:
    def test_version_increments_per_patch(self) -> None:
        state = ProcessState()
        state = apply_process_patch(state, SetRootTaskPatch(task_id="t1"))
        state = apply_process_patch(state, SetClarificationModePatch(mode="autopilot"))
        assert state.version == 2

    def test_original_state_is_not_mutated(self) -> None:
        state = ProcessState()
        apply_process_patch(state, SetRootTaskPatch(task_id="t1"))
        assert state.root_task_id is None
        assert state.version == 0


class TestUnknownPatch:
    def test_unknown_patch_raises_type_error(self) -> None:
        state = ProcessState()
        with pytest.raises(TypeError):
            apply_process_patch(state, object())  # type: ignore[arg-type]
