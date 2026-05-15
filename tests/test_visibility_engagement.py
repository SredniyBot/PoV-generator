"""Тесты Этапа 3 — алинейка visibility ↔ engagement-режим.

Покрывает:
    * **3.1**: матрица решений ask/assume/defer для всех комбинаций
      `(mode, visibility)`. Engagement-режим — порог автономного решения,
      а не право доступа.
    * **3.2**: универсальное право оспорить — операции
      `reopen_clarification`, `RejectPositionPatch`,
      `ElevateVisibilityPatch` работают в любом режиме (в т.ч. autopilot).
    * **role → default visibility**: эмиттеры без явной visibility получают
      её из роли через :func:`default_visibility_for_role`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pov_generator.application.clarification_service import (
    ClarificationService,
    default_visibility_for_role,
)
from pov_generator.application.project_service import ProjectService
from pov_generator.domain.clarifications import (
    ClarificationCandidate,
    ClarificationOption,
)
from pov_generator.domain.positions import Position
from pov_generator.domain.process_state import SetClarificationModePatch
from pov_generator.domain.project_knowledge import (
    GOAL_POSITION_ID,
    ElevateVisibilityPatch,
    RejectPositionPatch,
    UpsertPositionPatch,
)
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

OBJECTIVE_REF = "common.requirements_specification@1.0.0"


def _bootstrap(tmp_path: Path) -> tuple[SqliteRuntime, Path, ClarificationService]:
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    workspace = tmp_path / "case"
    project_service.init_project(
        workspace=workspace,
        name="visibility-engagement",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="Этап 3 alignment smoke test.",
        domain_packs=(),
    )
    service = ClarificationService(runtime, provider="stub")
    return runtime, workspace, service


def _make_candidate(
    project_id: str,
    *,
    visibility: str,
    default_assumption: str | None,
    blocking_scope: str = "task",
) -> ClarificationCandidate:
    return ClarificationCandidate(
        candidate_id=f"cand-{visibility}-{default_assumption is not None}-{blocking_scope}",
        project_id=project_id,
        source_type="task",
        source_id=f"task.q.{visibility}.{blocking_scope}",
        need="x",
        question=f"Вопрос уровня {visibility}, scope={blocking_scope}?",
        description="desc",
        rationale="r",
        impact="i",
        severity="medium",
        confidence_without_user=0.4,
        visibility=visibility,  # type: ignore[arg-type]
        default_assumption=default_assumption,
        recommended_answer=None,
        answer_mode="single",
        options=(ClarificationOption(option_id="yes", label="Да"),),
        affected_task_ids=(),
        related_artifact_ids=(),
        blocking_scope=blocking_scope,  # type: ignore[arg-type]
        decision_owner_role="business",
        created_at="",
    )


# --- 3.1. Матрица решений по visibility × mode -----------------------------


_PROACTIVE_TABLE = {
    "autopilot": {"principal"},
    "balanced": {"principal", "architectural"},
    "control": {"principal", "architectural"},
    "expert": {"principal", "architectural", "technical"},
}


@pytest.mark.parametrize(
    "mode,visibility",
    [
        (m, v)
        for m in ("autopilot", "balanced", "control", "expert")
        for v in ("principal", "architectural", "technical")
    ],
)
def test_decision_action_with_default_assumption_follows_proactive_table(
    tmp_path: Path, mode: str, visibility: str
) -> None:
    """С ``default_assumption``:

    * visibility ∈ proactive_set(mode) → ``ask`` (mode хочет surface);
    * иначе → ``assume`` (есть безопасный путь).
    """
    runtime, workspace, service = _bootstrap(tmp_path)
    runtime.apply_process_patch(
        workspace,
        SetClarificationModePatch(mode=mode),  # type: ignore[arg-type]
        actor="test",
        reason="set mode",
    )
    cand = _make_candidate(
        project_id=runtime.load_manifest(workspace).project_id,
        visibility=visibility,
        default_assumption="безопасный default",
    )
    [decision] = service.register_candidates(workspace, (cand,))
    expected = "ask" if visibility in _PROACTIVE_TABLE[mode] else "assume"
    assert decision.action == expected, (
        f"mode={mode}, visibility={visibility}: ожидалось {expected}, получено {decision.action}"
    )


@pytest.mark.parametrize(
    "mode,visibility",
    [
        (m, v)
        for m in ("autopilot", "balanced", "control", "expert")
        for v in ("principal", "architectural", "technical")
    ],
)
def test_decision_action_without_default_assumption_defers_outside_proactive(
    tmp_path: Path, mode: str, visibility: str
) -> None:
    """Без ``default_assumption``:

    * visibility ∈ proactive_set(mode) → ``ask``;
    * иначе → ``defer`` (мягкий skip).
    """
    runtime, workspace, service = _bootstrap(tmp_path)
    runtime.apply_process_patch(
        workspace,
        SetClarificationModePatch(mode=mode),  # type: ignore[arg-type]
        actor="test",
        reason="set mode",
    )
    cand = _make_candidate(
        project_id=runtime.load_manifest(workspace).project_id,
        visibility=visibility,
        default_assumption=None,
    )
    [decision] = service.register_candidates(workspace, (cand,))
    expected = "ask" if visibility in _PROACTIVE_TABLE[mode] else "defer"
    assert decision.action == expected


def test_objective_scope_override_surfaces_even_outside_proactive(tmp_path: Path) -> None:
    """blocking_scope=objective (gate signoff) должен всплыть даже если
    visibility не в proactive set — страховка для emitter'ов."""
    runtime, workspace, service = _bootstrap(tmp_path)
    runtime.apply_process_patch(
        workspace,
        SetClarificationModePatch(mode="autopilot"),
        actor="test",
        reason="set autopilot",
    )
    cand = _make_candidate(
        project_id=runtime.load_manifest(workspace).project_id,
        visibility="technical",  # technical не в proactive autopilot
        default_assumption=None,
        blocking_scope="objective",
    )
    [decision] = service.register_candidates(workspace, (cand,))
    assert decision.action == "ask"


# --- role → default visibility ---------------------------------------------


@pytest.mark.parametrize(
    "role,expected_visibility",
    [
        ("business", "principal"),
        ("client", "principal"),
        ("security", "principal"),
        ("data_owner", "architectural"),
        ("methodologist", "architectural"),
        ("architect", "technical"),
    ],
)
def test_default_visibility_for_role(role: str, expected_visibility: str) -> None:
    assert default_visibility_for_role(role) == expected_visibility  # type: ignore[arg-type]


def test_candidate_from_question_uses_role_default_visibility(tmp_path: Path) -> None:
    """Если visibility не указана явно, кандидат получает дефолт по роли."""
    runtime, workspace, service = _bootstrap(tmp_path)
    candidate = service.candidate_from_question(
        project_id="proj",
        source_type="task",
        source_id="src",
        question="?",
        affected_task_ids=(),
        related_artifact_ids=(),
        decision_owner_role="methodologist",
    )
    assert candidate.visibility == "architectural"


def test_candidate_from_question_explicit_visibility_overrides_role(tmp_path: Path) -> None:
    runtime, workspace, service = _bootstrap(tmp_path)
    candidate = service.candidate_from_question(
        project_id="proj",
        source_type="task",
        source_id="src",
        question="?",
        affected_task_ids=(),
        related_artifact_ids=(),
        decision_owner_role="business",
        visibility="technical",
    )
    assert candidate.visibility == "technical"


# --- 3.2. Универсальное право оспорить -------------------------------------


def _put_position(runtime: SqliteRuntime, workspace: Path, identifier: str) -> None:
    """Записать активное положение типа ``decision`` в Layer A."""
    runtime.apply_knowledge_patch(
        workspace,
        UpsertPositionPatch(
            position=Position(
                identifier=identifier,
                type="decision",
                statement="Какое-то решение.",
                visibility="technical",
                scope="global",
                source="user",
                taken_by="test",
                taken_at="2026-05-13T10:00:00+00:00",
            )
        ),
        actor="test",
        reason="seed for dispute tests",
    )


def test_reject_position_works_in_autopilot_mode(tmp_path: Path) -> None:
    """Этап 3.2: пользователь имеет право оспорить положение в любом режиме —
    включая autopilot. RejectPositionPatch не смотрит на mode."""
    runtime, workspace, _ = _bootstrap(tmp_path)
    runtime.apply_process_patch(
        workspace,
        SetClarificationModePatch(mode="autopilot"),
        actor="test",
        reason="set autopilot",
    )
    _put_position(runtime, workspace, "tech.deep")

    runtime.apply_knowledge_patch(
        workspace,
        RejectPositionPatch(position_id="tech.deep", reason="manager disagrees"),
        actor="user:1",
        reason="dispute",
    )
    knowledge = runtime.load_knowledge(workspace)
    rejected = knowledge.must_get("tech.deep")
    assert rejected.status == "rejected"
    assert rejected.rejection_reason == "manager disagrees"


def test_elevate_visibility_works_in_autopilot_mode(tmp_path: Path) -> None:
    """Этап 3.2: поднять visibility (например, до principal) можно в autopilot.
    Право оспорить — универсально, не зависит от engagement-режима."""
    runtime, workspace, _ = _bootstrap(tmp_path)
    runtime.apply_process_patch(
        workspace,
        SetClarificationModePatch(mode="autopilot"),
        actor="test",
        reason="set autopilot",
    )
    _put_position(runtime, workspace, "tech.deep")

    runtime.apply_knowledge_patch(
        workspace,
        ElevateVisibilityPatch(position_id="tech.deep", new_level="principal"),
        actor="user:1",
        reason="manager dispute raises importance",
    )
    knowledge = runtime.load_knowledge(workspace)
    assert knowledge.must_get("tech.deep").visibility == "principal"


def test_reopen_clarification_works_in_autopilot_mode(tmp_path: Path) -> None:
    """Этап 3.2: пере-открыть уточнение можно в autopilot. Mode не блокирует
    операции с уже существующими запросами."""
    runtime, workspace, service = _bootstrap(tmp_path)
    # Сначала — assumed (technical + balanced default mode + есть default).
    cand = _make_candidate(
        project_id=runtime.load_manifest(workspace).project_id,
        visibility="technical",
        default_assumption="default",
    )
    [decision] = service.register_candidates(workspace, (cand,))
    request_id = decision.request_id
    assert request_id is not None
    assert runtime.get_clarification_request(workspace, request_id).status == "assumed"

    # Переключаем в autopilot и переоткрываем — должно работать.
    runtime.apply_process_patch(
        workspace,
        SetClarificationModePatch(mode="autopilot"),
        actor="test",
        reason="set autopilot",
    )
    reopened = service.reopen_clarification(workspace, request_id=request_id)
    assert reopened.status == "open"
