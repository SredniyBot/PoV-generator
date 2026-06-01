"""Тесты CheckpointService (v3.0).

Логика:
- process_planned_decisions: разделение по уровню, создание сессии или
  тихий accept_default
- submit_answers: применение разных типов ответов, валидация, финализация

Не покрывают (другие фазы / другие тесты):
- DecisionPlanningService с реальным LLM (mock через test_decision_planning_service.py)
- REST endpoints (test_checkpoints_api.py)
- Интеграцию в ExecutionService (test_pre_flight_integration.py — будущий)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pov_generator.application.checkpoint_service import CheckpointService
from pov_generator.common.errors import ConflictError
from pov_generator.domain.checkpoints import CheckpointAnswer
from pov_generator.domain.decisions import Decision, DecisionAlternative, DecisionInput
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime


def _alt(option_id: str, label: str = "label") -> DecisionAlternative:
    return DecisionAlternative(
        option_id=option_id,
        label=label,
        description=f"desc for {option_id}",
        pros=("pro",),
        cons=("con",),
        confidence=0.7,
    )


def _make_decision(
    *,
    decision_id: str,
    level: str,
    project_id: str = "p-1",
    chosen: str = "opt-a",
    confidence: float = 0.8,
) -> Decision:
    return Decision(
        decision_id=decision_id,
        project_id=project_id,
        title=f"Decision {decision_id}",
        description="...",
        chosen_option_id=chosen,
        alternatives=(_alt("opt-a", "Option A"), _alt("opt-b", "Option B")),
        rationale="rationale",
        level=level,  # type: ignore[arg-type]
        level_rationale="...",
        confidence=confidence,
        status="proposed",
        source="identification",
        source_task_id="task-1",
    )


@pytest.fixture
def service(tmp_path: Path) -> tuple[CheckpointService, SqliteRuntime, Path]:
    runtime = SqliteRuntime()
    return CheckpointService(runtime), runtime, tmp_path / "ws"


# ---------------------------------------------------------------------------
# process_planned_decisions — фильтрация по уровню
# ---------------------------------------------------------------------------


def test_autopilot_silent_accept_all_no_session(service) -> None:
    """В autopilot все решения тихо принимаются, сессия не создаётся."""
    svc, runtime, ws = service
    decisions = (
        _make_decision(decision_id="d-1", level="business"),
        _make_decision(decision_id="d-2", level="architecture"),
        _make_decision(decision_id="d-3", level="detail"),
    )
    result = svc.process_planned_decisions(
        ws,
        project_id="p-1",
        task_id="task-1",
        task_title="Test task",
        artifact_role="test_artifact",
        decisions=decisions,
        mode="autopilot",
    )
    assert result.session is None
    assert result.surfaced_count == 0
    assert result.silent_count == 3
    # Все решения сохранились в реестре как accepted_default
    saved = runtime.list_decisions(ws, project_id="p-1")
    assert len(saved) == 3
    for d in saved:
        assert d.status == "accepted_default"
        assert d.user_action == "not_shown"


def test_register_decision_inputs_accepts_optional_category(service) -> None:
    svc, runtime, ws = service
    created = svc.register_decision_inputs(
        ws,
        project_id="p-1",
        decision_inputs=(
            DecisionInput(
                title="Выбор БД",
                description="Какую БД использовать для MVP",
                alternatives=(_alt("postgres", "PostgreSQL"), _alt("mysql", "MySQL")),
                recommended_option_id="postgres",
                rationale="PostgreSQL лучше ложится на требования к данным",
                level="architecture",
                source_task_id=None,
                category="tech_stack",
            ),
        ),
    )

    assert len(created) == 1
    saved = runtime.get_decision(ws, created[0].decision_id)
    assert saved.category == "tech_stack"
    assert saved.normalized_category == "tech_stack"
    assert saved.description == "Какую БД использовать для MVP"


def test_register_decision_inputs_uses_legacy_category_prefix(service) -> None:
    svc, runtime, ws = service
    created = svc.register_decision_inputs(
        ws,
        project_id="p-1",
        decision_inputs=(
            DecisionInput(
                title="Граница MVP",
                description="[scope] Что включить в первый релиз",
                alternatives=(_alt("narrow", "Узкий MVP"), _alt("wide", "Широкий MVP")),
                recommended_option_id="narrow",
                rationale="Узкий MVP снижает риск поставки",
                level="business",
                source_task_id=None,
            ),
        ),
    )

    assert len(created) == 1
    saved = runtime.get_decision(ws, created[0].decision_id)
    assert saved.category == "scope"
    assert saved.normalized_category == "scope"
    assert saved.description == "Что включить в первый релиз"


def test_balanced_surfaces_only_business(service) -> None:
    """В balanced — только business идёт в checkpoint, остальное тихо."""
    svc, runtime, ws = service
    decisions = (
        _make_decision(decision_id="d-biz", level="business"),
        _make_decision(decision_id="d-arch", level="architecture"),
        _make_decision(decision_id="d-det", level="detail"),
    )
    result = svc.process_planned_decisions(
        ws,
        project_id="p-1",
        task_id="task-1",
        task_title="Test",
        artifact_role="role",
        decisions=decisions,
        mode="balanced",
    )
    assert result.session is not None
    assert result.surfaced_count == 1
    assert result.silent_count == 2
    assert result.session.decision_ids == ("d-biz",)
    # Surfaced остался proposed
    biz = runtime.get_decision(ws, "d-biz")
    assert biz.status == "proposed"
    # Silent ушли в accepted_default
    arch = runtime.get_decision(ws, "d-arch")
    det = runtime.get_decision(ws, "d-det")
    assert arch.status == "accepted_default"
    assert det.status == "accepted_default"


def test_expert_surfaces_all_three_levels(service) -> None:
    svc, runtime, ws = service
    decisions = (
        _make_decision(decision_id="d-biz", level="business"),
        _make_decision(decision_id="d-arch", level="architecture"),
        _make_decision(decision_id="d-det", level="detail"),
    )
    result = svc.process_planned_decisions(
        ws,
        project_id="p-1",
        task_id="task-1",
        task_title="Test",
        artifact_role="role",
        decisions=decisions,
        mode="expert",
    )
    assert result.surfaced_count == 3
    assert result.silent_count == 0
    assert set(result.session.decision_ids) == {"d-biz", "d-arch", "d-det"}


def test_empty_decisions_yields_no_session(service) -> None:
    svc, _runtime, ws = service
    result = svc.process_planned_decisions(
        ws,
        project_id="p-1",
        task_id="task-1",
        task_title="Test",
        artifact_role="role",
        decisions=(),
        mode="expert",
    )
    assert result.session is None
    assert result.surfaced_count == 0
    assert result.silent_count == 0


# ---------------------------------------------------------------------------
# submit_answers — применение разных типов ответов
# ---------------------------------------------------------------------------


def _seed_session_with_two_decisions(svc: CheckpointService, ws: Path):
    """Подготовить сессию с 2 business-decisions для balanced mode."""
    decisions = (
        _make_decision(decision_id="d-1", level="business"),
        _make_decision(decision_id="d-2", level="business"),
    )
    result = svc.process_planned_decisions(
        ws,
        project_id="p-1",
        task_id="task-1",
        task_title="Test",
        artifact_role="role",
        decisions=decisions,
        mode="balanced",
    )
    assert result.session is not None
    return result.session


def test_submit_accept_default_marks_decision(service) -> None:
    svc, runtime, ws = service
    session = _seed_session_with_two_decisions(svc, ws)
    finalized = svc.submit_answers(
        ws,
        session_id=session.session_id,
        answers=(
            CheckpointAnswer(decision_id="d-1", kind="accept_default"),
            CheckpointAnswer(decision_id="d-2", kind="accept_default"),
        ),
    )
    assert finalized.status == "finalized"
    assert finalized.finalized_by == "user"
    d1 = runtime.get_decision(ws, "d-1")
    assert d1.status == "accepted_default"
    assert d1.user_action == "accepted_default"


def test_submit_select_alternative_changes_choice(service) -> None:
    svc, runtime, ws = service
    session = _seed_session_with_two_decisions(svc, ws)
    svc.submit_answers(
        ws,
        session_id=session.session_id,
        answers=(
            CheckpointAnswer(
                decision_id="d-1",
                kind="select_alternative",
                selected_option_id="opt-b",
            ),
        ),
    )
    d1 = runtime.get_decision(ws, "d-1")
    assert d1.chosen_option_id == "opt-b"
    assert d1.original_chosen_option_id == "opt-a"
    assert d1.status == "user_overridden"
    assert d1.was_user_modified is True


def test_submit_free_text_stores_answer(service) -> None:
    svc, runtime, ws = service
    session = _seed_session_with_two_decisions(svc, ws)
    svc.submit_answers(
        ws,
        session_id=session.session_id,
        answers=(
            CheckpointAnswer(
                decision_id="d-1",
                kind="free_text",
                free_text="My own choice",
            ),
        ),
    )
    d1 = runtime.get_decision(ws, "d-1")
    assert d1.user_free_text_answer == "My own choice"
    assert d1.status == "user_overridden"


def test_submit_defer_marks_for_review(service) -> None:
    svc, runtime, ws = service
    session = _seed_session_with_two_decisions(svc, ws)
    svc.submit_answers(
        ws,
        session_id=session.session_id,
        answers=(
            CheckpointAnswer(decision_id="d-1", kind="defer"),
        ),
    )
    d1 = runtime.get_decision(ws, "d-1")
    assert d1.status == "deferred"
    assert d1.user_action == "deferred"


def test_unanswered_decisions_default_on_finalize(service) -> None:
    """Если пользователь ответил только на часть — оставшиеся
    автоматически уходят в accept_default. Это семантика «закрыл сессию,
    с остальным согласен»."""
    svc, runtime, ws = service
    session = _seed_session_with_two_decisions(svc, ws)
    svc.submit_answers(
        ws,
        session_id=session.session_id,
        answers=(
            CheckpointAnswer(decision_id="d-1", kind="accept_default"),
            # d-2 не ответили
        ),
    )
    d2 = runtime.get_decision(ws, "d-2")
    assert d2.status == "accepted_default"
    assert d2.user_action == "accepted_default"


def test_explicit_answer_clears_low_confidence_flag(service) -> None:
    """v3.9: явный ответ пользователя (включая accept_default низкоуверенного
    дефолта) = подтверждение → user_verified=True → is_low_confidence гаснет.
    Незатронутые в той же сессии тоже помечаются (кнопка «ответить на все»).
    defer — НЕ подтверждение, флаг остаётся."""
    svc, runtime, ws = service
    low = (
        _make_decision(decision_id="d-1", level="business", confidence=0.2),
        _make_decision(decision_id="d-2", level="business", confidence=0.2),
        _make_decision(decision_id="d-3", level="business", confidence=0.2),
    )
    result = svc.process_planned_decisions(
        ws,
        project_id="p-1",
        task_id="task-1",
        task_title="Test",
        artifact_role="role",
        decisions=low,
        mode="balanced",
    )
    assert result.session is not None
    # До ответа все три «низкоуверенные».
    assert runtime.get_decision(ws, "d-1").is_low_confidence is True

    svc.submit_answers(
        ws,
        session_id=result.session.session_id,
        answers=(
            CheckpointAnswer(decision_id="d-1", kind="accept_default"),  # явный accept
            CheckpointAnswer(decision_id="d-2", kind="defer"),           # отложил
            # d-3 не тронут → массовый accept_default
        ),
    )

    d1 = runtime.get_decision(ws, "d-1")
    assert d1.user_verified is True
    assert d1.is_low_confidence is False  # ← починка: флаг снят

    d3 = runtime.get_decision(ws, "d-3")
    assert d3.user_verified is True
    assert d3.is_low_confidence is False  # незатронутый, но «ответил на все»

    d2 = runtime.get_decision(ws, "d-2")
    assert d2.user_verified is False
    assert d2.is_low_confidence is True   # defer ≠ подтверждение


# ---------------------------------------------------------------------------
# submit_answers — валидация
# ---------------------------------------------------------------------------


def test_submit_to_finalized_session_raises(service) -> None:
    svc, _runtime, ws = service
    session = _seed_session_with_two_decisions(svc, ws)
    svc.submit_answers(
        ws,
        session_id=session.session_id,
        answers=(CheckpointAnswer(decision_id="d-1", kind="accept_default"),),
    )
    # Второй submit на уже финализированную — ошибка
    with pytest.raises(ConflictError, match="в статусе finalized"):
        svc.submit_answers(
            ws,
            session_id=session.session_id,
            answers=(CheckpointAnswer(decision_id="d-2", kind="accept_default"),),
        )


def test_submit_answer_for_unknown_decision_raises(service) -> None:
    svc, _runtime, ws = service
    session = _seed_session_with_two_decisions(svc, ws)
    with pytest.raises(ConflictError, match="не принадлежит сессии"):
        svc.submit_answers(
            ws,
            session_id=session.session_id,
            answers=(CheckpointAnswer(decision_id="d-fake", kind="accept_default"),),
        )


def test_submit_duplicate_answer_in_one_batch_raises(service) -> None:
    svc, _runtime, ws = service
    session = _seed_session_with_two_decisions(svc, ws)
    with pytest.raises(ConflictError, match="повторный ответ"):
        svc.submit_answers(
            ws,
            session_id=session.session_id,
            answers=(
                CheckpointAnswer(decision_id="d-1", kind="accept_default"),
                CheckpointAnswer(decision_id="d-1", kind="defer"),
            ),
        )


def test_select_alternative_with_invalid_option_raises(service) -> None:
    svc, _runtime, ws = service
    session = _seed_session_with_two_decisions(svc, ws)
    with pytest.raises(ConflictError, match="нет среди альтернатив"):
        svc.submit_answers(
            ws,
            session_id=session.session_id,
            answers=(
                CheckpointAnswer(
                    decision_id="d-1",
                    kind="select_alternative",
                    selected_option_id="opt-does-not-exist",
                ),
            ),
        )


def test_free_text_requires_non_empty_text(service) -> None:
    svc, _runtime, ws = service
    session = _seed_session_with_two_decisions(svc, ws)
    with pytest.raises(ConflictError, match="требует непустой free_text"):
        svc.submit_answers(
            ws,
            session_id=session.session_id,
            answers=(
                CheckpointAnswer(
                    decision_id="d-1",
                    kind="free_text",
                    free_text=None,
                ),
            ),
        )
