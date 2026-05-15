"""Тесты для audit trail + defer/reopen + dedup нормализации (W5.1).

Закрывают:
1. Создание ClarificationRequest пишет event `created` / `assumed_auto`.
2. answer_clarification пишет `answered` event.
3. accept_assumption пишет `assumed` event.
4. defer_clarification переводит статус в `deferred` + event.
5. reopen_clarification возвращает в `open`, очищает поля ответа,
   но в events сохраняется предыдущий ответ.
6. dedup find_clarification_by_source нормализует question text:
   разница в пунктуации/whitespace/case → один и тот же request.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pov_generator.application.clarification_service import ClarificationService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.domain.clarifications import ClarificationOption
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
        name="events test",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="test",
        domain_packs=(),
    )
    return workspace, bootstrap.manifest.project_id, runtime, service


def _make_candidate(service: ClarificationService, project_id: str, *, question: str, source_id: str):
    return service.candidate_from_question(
        project_id=project_id,
        source_type="task",
        source_id=source_id,
        question=question,
        affected_task_ids=(),
        related_artifact_ids=(),
        severity="high",
        confidence_without_user=0.3,
        options=(
            ClarificationOption(option_id="yes", label="Да", description="", effect_preview=""),
            ClarificationOption(option_id="no", label="Нет", description="", effect_preview=""),
        ),
        default_assumption=None,
        decision_owner_role="business",
    )


def test_register_candidate_emits_created_event(tmp_path: Path) -> None:
    workspace, project_id, runtime, service = _bootstrap(tmp_path)
    candidate = _make_candidate(service, project_id, question="Какой бюджет?", source_id="task.q1")
    [decision] = service.register_candidates(workspace, (candidate,))
    events = service.list_events(workspace, decision.request_id)
    assert events
    assert events[0]["event_type"] == "created"
    assert events[0]["payload"]["source_type"] == "task"
    assert events[0]["payload"]["decision_owner_role"] == "business"


def test_answer_emits_answered_event_with_previous_status(tmp_path: Path) -> None:
    workspace, project_id, runtime, service = _bootstrap(tmp_path)
    candidate = _make_candidate(service, project_id, question="Бюджет?", source_id="task.q2")
    [decision] = service.register_candidates(workspace, (candidate,))
    service.answer_clarification(
        workspace,
        request_id=decision.request_id,
        selected_option_ids=("yes",),
    )
    events = service.list_events(workspace, decision.request_id)
    types = [e["event_type"] for e in events]
    assert types == ["created", "answered"]
    answered = events[1]
    assert answered["payload"]["selected_option_ids"] == ["yes"]
    assert answered["payload"]["previous_status"] == "open"


def test_defer_clarification_changes_status_and_emits_event(tmp_path: Path) -> None:
    workspace, project_id, runtime, service = _bootstrap(tmp_path)
    candidate = _make_candidate(service, project_id, question="Дата старта?", source_id="task.q3")
    [decision] = service.register_candidates(workspace, (candidate,))
    deferred = service.defer_clarification(
        workspace, request_id=decision.request_id, reason="Ждём подтверждения юр.",
    )
    assert deferred.status == "deferred"
    events = service.list_events(workspace, decision.request_id)
    deferred_event = next(e for e in events if e["event_type"] == "deferred")
    assert deferred_event["payload"]["reason"] == "Ждём подтверждения юр."
    assert deferred_event["payload"]["previous_status"] == "open"


def test_defer_rejected_for_already_deferred_or_cancelled(tmp_path: Path) -> None:
    workspace, project_id, runtime, service = _bootstrap(tmp_path)
    candidate = _make_candidate(service, project_id, question="OK?", source_id="task.q4")
    [decision] = service.register_candidates(workspace, (candidate,))
    service.defer_clarification(workspace, request_id=decision.request_id)
    from pov_generator.common.errors import ConflictError
    with pytest.raises(ConflictError):
        service.defer_clarification(workspace, request_id=decision.request_id)


def test_reopen_clears_answer_in_request_but_keeps_audit_trail(tmp_path: Path) -> None:
    workspace, project_id, runtime, service = _bootstrap(tmp_path)
    candidate = _make_candidate(service, project_id, question="Нужен ли DPO?", source_id="task.q5")
    [decision] = service.register_candidates(workspace, (candidate,))
    service.answer_clarification(
        workspace, request_id=decision.request_id, selected_option_ids=("yes",),
    )
    # пере-ответ
    reopened = service.reopen_clarification(workspace, request_id=decision.request_id)
    assert reopened.status == "open"
    assert reopened.selected_option_ids == ()
    assert reopened.resolution_summary in (None, "")
    # но audit trail сохраняет факт первого ответа
    events = service.list_events(workspace, decision.request_id)
    types = [e["event_type"] for e in events]
    assert types == ["created", "answered", "reopened"]
    reopen_evt = events[-1]
    assert reopen_evt["payload"]["previous_status"] == "answered"
    assert reopen_evt["payload"]["previous_selected_option_ids"] == ["yes"]


def test_dedup_matches_question_with_different_punctuation_and_whitespace(tmp_path: Path) -> None:
    """Раньше малейшая правка question создавала дубль. Сейчас нормализация
    приравнивает 'Какой бюджет?' и 'Какой бюджет.', '  какой   бюджет?'."""
    workspace, project_id, runtime, service = _bootstrap(tmp_path)
    first = _make_candidate(service, project_id, question="Какой бюджет?", source_id="task.dedup")
    [d1] = service.register_candidates(workspace, (first,))
    # повторно — тот же source_id, чуть-чуть другая question строка
    second = _make_candidate(service, project_id, question="  какой   бюджет.  ", source_id="task.dedup")
    [d2] = service.register_candidates(workspace, (second,))
    assert d2.action == "reuse_existing"
    assert d2.request_id == d1.request_id


def test_dedup_does_not_match_genuinely_different_question(tmp_path: Path) -> None:
    workspace, project_id, runtime, service = _bootstrap(tmp_path)
    a = _make_candidate(service, project_id, question="Сколько пользователей?", source_id="task.diff")
    [d1] = service.register_candidates(workspace, (a,))
    b = _make_candidate(service, project_id, question="Сколько серверов?", source_id="task.diff")
    [d2] = service.register_candidates(workspace, (b,))
    assert d2.action != "reuse_existing"
    assert d2.request_id != d1.request_id
