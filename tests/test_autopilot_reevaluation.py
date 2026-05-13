"""Тесты для B1 — autopilot re-evaluation (фикс W6 жалобы #1).

Жалоба: «переключился в autopilot, но открытые вопросы остались блокировать».

Причина: `_decide_action` без `default_assumption` всегда возвращал "ask"
независимо от режима, ПЛЮС смена режима не пере-оценивала уже-открытые
candidates.

Закрываем:
1. На autopilot без default_assumption и blocking_scope=task → теперь
   решение "defer" (мягкий skip), а не "ask".
2. На autopilot с blocking_scope=objective (гейт human_approval) →
   остаётся "ask" — клиентское согласование нельзя auto-resolve.
3. На autopilot с default_assumption → "assume" (без изменений).
4. set_mode() пере-оценивает все open candidates и возвращает
   ReevaluationSummary с counts.
"""

from __future__ import annotations

from pathlib import Path

from pov_generator.application.clarification_service import ClarificationService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.domain.clarifications import ClarificationCandidate, ClarificationOption
from pov_generator.domain.process_state import SetClarificationModePatch
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE_REF = "common.requirements_specification@1.0.0"


def _bootstrap(tmp_path: Path):
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    service = ClarificationService(runtime, provider="stub")
    snapshot, _ = registry_service.validate()
    workspace = tmp_path / "case"
    bootstrap = project_service.init_project(
        workspace=workspace,
        name="autopilot test",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="t",
        domain_packs=(),
    )
    return workspace, bootstrap.manifest.project_id, runtime, service


def _make_candidate(
    service: ClarificationService,
    project_id: str,
    *,
    question: str,
    source_id: str,
    blocking_scope: str = "task",
    default_assumption: str | None = None,
    severity: str = "medium",
) -> ClarificationCandidate:
    cand = service.candidate_from_question(
        project_id=project_id,
        source_type="task",
        source_id=source_id,
        question=question,
        affected_task_ids=(),
        related_artifact_ids=(),
        severity=severity,
        confidence_without_user=0.3,
        default_assumption=default_assumption,
        options=(
            ClarificationOption(option_id="yes", label="Да"),
            ClarificationOption(option_id="no", label="Нет"),
        ),
        decision_owner_role="business",
        # форсируем minimum=balanced так autopilot НЕ allows
        visibility="architectural",
    )
    # candidate_from_question по умолчанию blocking_scope=task; для теста
    # objective создаём вручную.
    if blocking_scope != "task":
        cand = ClarificationCandidate(
            **{**cand.__dict__, "blocking_scope": blocking_scope},
        )
    return cand


def test_autopilot_defers_task_scope_candidate_without_assumption(tmp_path: Path) -> None:
    """В autopilot, blocking_scope=task, нет default_assumption →
    раньше "ask" (блокировал planner), теперь "defer" (мягкий skip)."""
    workspace, project_id, runtime, service = _bootstrap(tmp_path)
    runtime.apply_process_patch(
        workspace, SetClarificationModePatch(mode="autopilot"),
        actor="test", reason="switch to autopilot",
    )
    candidate = _make_candidate(
        service, project_id,
        question="Нужен ли мониторинг?",
        source_id="task.q1",
        blocking_scope="task",
        default_assumption=None,
    )
    [decision] = service.register_candidates(workspace, (candidate,))
    assert decision.action == "defer"
    request = runtime.get_clarification_request(workspace, decision.request_id)
    assert request.status == "deferred"


def test_autopilot_still_asks_objective_scope_candidate(tmp_path: Path) -> None:
    """Для blocking_scope=objective (гейт human_approval) autopilot НЕ
    auto-resolve: клиентское согласование требует явного решения."""
    workspace, project_id, runtime, service = _bootstrap(tmp_path)
    runtime.apply_process_patch(
        workspace, SetClarificationModePatch(mode="autopilot"),
        actor="test", reason="switch to autopilot",
    )
    candidate = _make_candidate(
        service, project_id,
        question="Согласовать ТЗ с клиентом?",
        source_id="gate.client_signoff",
        blocking_scope="objective",
        default_assumption=None,
    )
    [decision] = service.register_candidates(workspace, (candidate,))
    assert decision.action == "ask"


def test_autopilot_assumes_when_default_assumption_provided(tmp_path: Path) -> None:
    """С default_assumption autopilot тихо принимает допущение."""
    workspace, project_id, runtime, service = _bootstrap(tmp_path)
    runtime.apply_process_patch(
        workspace, SetClarificationModePatch(mode="autopilot"),
        actor="test", reason="switch to autopilot",
    )
    candidate = _make_candidate(
        service, project_id,
        question="Какая частота обновления?",
        source_id="task.q2",
        default_assumption="Раз в сутки.",
    )
    [decision] = service.register_candidates(workspace, (candidate,))
    assert decision.action == "assume"


def test_set_mode_reevaluates_existing_open_candidates(tmp_path: Path) -> None:
    """Сценарий из реальной жалобы: создать open в balanced, переключиться
    на autopilot → open candidate должен авто-перейти в deferred (или
    assumed если есть default)."""
    workspace, project_id, runtime, service = _bootstrap(tmp_path)
    # balanced (default) — candidate должен открыться
    candidate = _make_candidate(
        service, project_id,
        question="Нужно ли логирование запросов?",
        source_id="task.q3",
        default_assumption=None,
    )
    [decision] = service.register_candidates(workspace, (candidate,))
    assert decision.action == "ask"
    pre = runtime.get_clarification_request(workspace, decision.request_id)
    assert pre.status == "open"

    # переключаемся в autopilot
    summary = service.set_mode(workspace, "autopilot")
    assert summary.auto_deferred == 1
    assert summary.auto_assumed == 0
    assert summary.kept_open == 0

    post = runtime.get_clarification_request(workspace, decision.request_id)
    assert post.status == "deferred"


def test_set_mode_assumes_existing_open_when_default_assumption_available(tmp_path: Path) -> None:
    """Если у open candidate есть default_assumption — авто-assume при
    смене на autopilot."""
    workspace, project_id, runtime, service = _bootstrap(tmp_path)
    candidate = _make_candidate(
        service, project_id,
        question="Сколько серверов?",
        source_id="task.q4",
        default_assumption="3 сервера по умолчанию.",
    )
    [decision] = service.register_candidates(workspace, (candidate,))
    # Note: confidence_without_user=0.3 < 0.72 → не accept'нется сразу,
    # но т.к. balanced разрешает показ — это "ask".
    pre = runtime.get_clarification_request(workspace, decision.request_id)
    assert pre.status == "open"

    summary = service.set_mode(workspace, "autopilot")
    assert summary.auto_assumed == 1
    post = runtime.get_clarification_request(workspace, decision.request_id)
    assert post.status == "assumed"


def test_set_mode_keeps_objective_scope_open_on_autopilot(tmp_path: Path) -> None:
    """blocking_scope=objective не авто-решается даже после смены на autopilot."""
    workspace, project_id, runtime, service = _bootstrap(tmp_path)
    candidate = _make_candidate(
        service, project_id,
        question="Согласовать ТЗ?",
        source_id="gate.q5",
        blocking_scope="objective",
        default_assumption=None,
    )
    [decision] = service.register_candidates(workspace, (candidate,))
    assert decision.action == "ask"

    summary = service.set_mode(workspace, "autopilot")
    assert summary.kept_open == 1
    assert summary.auto_assumed == 0
    assert summary.auto_deferred == 0

    post = runtime.get_clarification_request(workspace, decision.request_id)
    assert post.status == "open"


def test_audit_log_records_deferred_auto_event(tmp_path: Path) -> None:
    """Авто-defer при создании пишет event_type='deferred_auto'."""
    workspace, project_id, runtime, service = _bootstrap(tmp_path)
    runtime.apply_process_patch(
        workspace, SetClarificationModePatch(mode="autopilot"),
        actor="test", reason="switch to autopilot",
    )
    candidate = _make_candidate(
        service, project_id,
        question="X?",
        source_id="task.q6",
        default_assumption=None,
    )
    [decision] = service.register_candidates(workspace, (candidate,))
    events = service.list_events(workspace, decision.request_id)
    assert events[0]["event_type"] == "deferred_auto"
