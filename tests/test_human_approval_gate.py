"""Тесты для `human_approval` quality gate `client.requirements_signoff@1.0.0`.

Покрывают полный цикл от завершения review-задачи до закрытия objective
через ответ заказчика на gate.
"""

from __future__ import annotations

from pathlib import Path

from pov_generator.application.clarification_service import ClarificationService
from pov_generator.application.context_service import ContextService
from pov_generator.application.execution_service import ExecutionService
from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.application.validation_service import ValidationService
from pov_generator.application.workflow_service import WorkflowService
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE_REF = "common.requirements_specification@1.0.0"
SIGNOFF_GATE_REF = "client.requirements_signoff@1.0.0"


def _bootstrap(tmp_path: Path):
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)
    context_service = ContextService(runtime)
    execution_service = ExecutionService(runtime, context_service)
    clarification_service = ClarificationService(runtime, provider="stub")
    validation_service = ValidationService(runtime, clarification_service)
    workflow_service = WorkflowService(runtime, planning_service, execution_service, validation_service)

    snapshot, report = registry_service.validate()
    assert report.is_valid

    workspace = tmp_path / "case"
    project_service.init_project(
        workspace=workspace,
        name="Signoff demo",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="PoV: подготовить структурированное ТЗ.",
        domain_packs=(),
    )
    planning_service.expand_graph(workspace, snapshot)

    return (
        workspace,
        snapshot,
        runtime,
        planning_service,
        workflow_service,
        clarification_service,
    )


def _find_signoff_request(runtime: SqliteRuntime, workspace: Path):
    return next(
        req
        for req in runtime.list_clarification_requests(workspace)
        if req.source_type == "quality_gate" and req.source_id == SIGNOFF_GATE_REF
    )


def test_human_approval_gate_blocks_then_approves(tmp_path: Path) -> None:
    """Полный цикл human_approval gate в одном тесте:

    1. Workflow открывает gate; objective не завершён.
    2. ClarificationRequest от gate имеет правильную структуру.
    3. Ответ ``rejected`` не закрывает objective.
    4. Перезаписанный ответ ``approved`` закрывает objective.

    Раньше эти проверки жили в двух отдельных тестах с одинаковым setup —
    объединены ради скорости.
    """
    (
        workspace,
        snapshot,
        runtime,
        planning_service,
        workflow_service,
        clarification_service,
    ) = _bootstrap(tmp_path)

    # 1. Workflow доходит до открытия gate.
    result = workflow_service.run_until_blocked(workspace, snapshot, provider="stub", max_steps=50)
    assert result.stopped_reason == "planner_blocked"
    assert planning_service._objective_completed(workspace, snapshot) is False

    # 2. Уточнение от gate существует и имеет правильную структуру.
    signoff = _find_signoff_request(runtime, workspace)
    assert signoff.source_type == "quality_gate"
    assert signoff.source_id == SIGNOFF_GATE_REF
    assert signoff.blocking_scope == "objective"
    assert signoff.status == "open"
    assert signoff.decision_owner_role == "client"
    option_ids = {option.option_id for option in signoff.options}
    assert "approved" in option_ids
    assert {"approved_with_comments", "rejected"}.issubset(option_ids)

    # 3. Reject не закрывает objective.
    clarification_service.answer_clarification(
        workspace, request_id=signoff.request_id, selected_option_ids=("rejected",)
    )
    assert planning_service._objective_completed(workspace, snapshot) is False

    # 4. Reopen + approve закрывает objective.
    clarification_service.reopen_clarification(workspace, request_id=signoff.request_id)
    clarification_service.answer_clarification(
        workspace, request_id=signoff.request_id, selected_option_ids=("approved",)
    )
    assert planning_service._objective_completed(workspace, snapshot) is True
    next_run = workflow_service.run_until_blocked(workspace, snapshot, provider="stub", max_steps=2)
    assert next_run.stopped_reason == "objective_completed"


def test_human_approval_gate_idempotent_request_creation(tmp_path: Path) -> None:
    """Повторный запуск workflow не должен создавать второй
    ClarificationRequest для того же gate — иначе UI/счётчики поедут."""
    (
        workspace,
        snapshot,
        runtime,
        _planning_service,
        workflow_service,
        _clarification_service,
    ) = _bootstrap(tmp_path)

    workflow_service.run_until_blocked(workspace, snapshot, provider="stub", max_steps=50)
    workflow_service.run_until_blocked(workspace, snapshot, provider="stub", max_steps=5)

    signoff_requests = [
        req
        for req in runtime.list_clarification_requests(workspace)
        if req.source_type == "quality_gate" and req.source_id == SIGNOFF_GATE_REF
    ]
    assert len(signoff_requests) == 1
