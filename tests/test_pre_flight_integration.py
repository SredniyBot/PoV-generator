"""End-to-end интеграция pre-flight checkpoint в ExecutionService (v3.0).

Сценарии:
1. Первый запуск задачи в balanced → pre-flight создаёт сессию из
   business-решения → execute_task возвращает paused_for_checkpoint.
2. Пользователь финализирует сессию → задача переходит обратно в ready.
3. Повторный запуск задачи → pre-flight видит finalized session, НЕ
   создаёт новых, locked-in решение попадает в основной промпт.
4. В autopilot pre-flight тоже срабатывает, но решения уходят в silent;
   задача сразу идёт в основную генерацию.

Использует stub LLM (через `provider="stub"`) для основной генерации и
mock-планировщик для pre-flight. Так тест детерминирован и не зависит
от настройки настоящих LLM-провайдеров.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_m9_api import init_project, run_stub_workflow  # type: ignore

from pov_generator.application.checkpoint_service import CheckpointService
from pov_generator.application.context_service import ContextService
from pov_generator.application.decision_planning_service import (
    DecisionPlanningService,
    PlanningResult,
)
from pov_generator.application.execution_service import ExecutionService
from pov_generator.application.registry_service import RegistryService
from pov_generator.application.workflow_service import WorkflowService
from pov_generator.application.planning_service import PlanningService
from pov_generator.application.validation_service import ValidationService
from pov_generator.application.clarification_service import ClarificationService
from pov_generator.domain.checkpoints import CheckpointAnswer
from pov_generator.domain.decisions import Decision, DecisionAlternative
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]


class _StubPlanningService(DecisionPlanningService):
    """DecisionPlanningService, который возвращает фиксированный список
    решений вне зависимости от вызова. Заменяет LLM-вызов.
    """

    def __init__(self, decisions_factory) -> None:
        # Не вызываем super().__init__, llm_registry не нужен — нет LLM-вызовов.
        self._decisions_factory = decisions_factory

    def plan_for_task(
        self,
        *,
        project_id: str,
        task_id: str,
        task_title: str,
        artifact_role: str,
        task_summary: str,
        context_text: str,
        provider: str | None = None,
        model: str | None = None,
    ) -> PlanningResult:
        decisions = self._decisions_factory(
            project_id=project_id, task_id=task_id, artifact_role=artifact_role
        )
        return PlanningResult(
            decisions=decisions,
            provider="stub-planner",
            model="stub-planner",
            raw_response={"decisions": [d.title for d in decisions]},
        )


def _make_business_decision(*, project_id: str, task_id: str, artifact_role: str) -> tuple[Decision, ...]:
    """Фабрика: одно business-решение, surface в balanced/control/expert."""
    return (
        Decision(
            decision_id=f"d-{task_id}",
            project_id=project_id,
            title="Целевая аудитория сервиса",
            description="Кто пользователи: корпорации или розница?",
            chosen_option_id="opt-corp",
            alternatives=(
                DecisionAlternative(
                    option_id="opt-corp",
                    label="Корпоративные клиенты",
                    description="B2B сегмент",
                    pros=("крупные сделки",),
                    cons=("долгий цикл продажи",),
                    confidence=0.7,
                ),
                DecisionAlternative(
                    option_id="opt-retail",
                    label="Розничные клиенты",
                    description="B2C сегмент",
                    pros=("быстрая адаптация",),
                    cons=("низкий чек",),
                    confidence=0.5,
                ),
            ),
            rationale="По контексту проекта — B2B-направление",
            level="business",
            level_rationale="Меняет, ДЛЯ КОГО продукт; видимо заказчику",
            confidence=0.7,
            status="proposed",
            source="pre_flight",
            source_task_id=task_id,
        ),
    )


def _make_detail_decision(*, project_id: str, task_id: str, artifact_role: str) -> tuple[Decision, ...]:
    """Фабрика: одно detail-решение, не surface в balanced."""
    return (
        Decision(
            decision_id=f"d-{task_id}-det",
            project_id=project_id,
            title="Naming convention для API",
            description="snake_case vs camelCase",
            chosen_option_id="opt-snake",
            alternatives=(
                DecisionAlternative(option_id="opt-snake", label="snake_case"),
                DecisionAlternative(option_id="opt-camel", label="camelCase"),
            ),
            rationale="Python-конвенция",
            level="detail",
            level_rationale="Только REST-контракт, легко поменять",
            confidence=0.85,
            status="proposed",
            source="pre_flight",
            source_task_id=task_id,
        ),
    )


def _bootstrap_services(workspace: Path, planning_factory):
    """Собрать минимальный набор сервисов для интеграционного теста.

    `planning_factory` — функция (project_id, task_id, artifact_role) → tuple[Decision].
    """
    runtime = SqliteRuntime()
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    context_service = ContextService(runtime)
    planning = PlanningService(runtime)
    clarification = ClarificationService(runtime, provider="stub")
    checkpoint = CheckpointService(runtime)
    stub_planning = _StubPlanningService(planning_factory)
    execution = ExecutionService(
        runtime,
        context_service,
        decision_planning_service=stub_planning,
        checkpoint_service=checkpoint,
    )
    validation = ValidationService(runtime, clarification)
    workflow = WorkflowService(runtime, planning, execution, validation)
    snapshot, _ = registry_service.validate()
    return runtime, snapshot, workflow, checkpoint, planning, execution


def _first_leaf_task_with_artifact(runtime, workspace, snapshot):
    """Найти первую leaf-задачу с артефактом и БЕЗ обязательных upstream-входов.

    Без второго условия тест натыкается на ConflictError из context_service:
    «отсутствует обязательный входной артефакт» (мы не запускаем workflow
    целиком, только дёргаем одну задачу). Аналог `_first_leaf_no_input`
    из test_prompt_authority.py.
    """
    for task in runtime.list_tasks(workspace):
        if task.template_type != "leaf":
            continue
        try:
            template = snapshot.resolve_template(task.template_ref)
        except Exception:
            continue
        if template.merge is not None and template.merge.strategy == "structural":
            continue
        if not template.outputs.artifact_roles:
            continue
        if template.inputs.required_artifact_roles:
            continue
        return task
    raise AssertionError("Не нашли leaf-задачу с артефактом без required-входов")


# ---------------------------------------------------------------------------
# Сценарий 1: balanced + business decision → pause
# ---------------------------------------------------------------------------


def test_balanced_business_decision_pauses_execution(tmp_path: Path) -> None:
    workspace = tmp_path / "case_pause"
    init_project(workspace, "Бизнес-запрос для теста pre-flight.")

    runtime, snapshot, workflow, checkpoint, planning_svc, _exec = _bootstrap_services(
        workspace, _make_business_decision
    )
    planning_svc.expand_graph(workspace, snapshot)
    task = _first_leaf_task_with_artifact(runtime, workspace, snapshot)
    project_id = runtime.load_project_state(workspace).manifest.project_id

    # Mode по умолчанию — balanced (см. ProcessState).
    bundle = _exec.execute_task(workspace, snapshot, task.task_id, provider="stub")

    assert bundle.result.status == "paused_for_checkpoint"
    assert bundle.result.checkpoint_session_id is not None
    # В реестре появилось решение со статусом proposed
    decisions = runtime.list_decisions(workspace, project_id=project_id)
    assert len(decisions) == 1
    assert decisions[0].status == "proposed"
    # Сессия pending
    session = runtime.get_checkpoint_session(workspace, bundle.result.checkpoint_session_id)
    assert session.status == "pending"
    assert session.task_id == task.task_id


# ---------------------------------------------------------------------------
# Сценарий 2: autopilot → silent accept, без паузы
# ---------------------------------------------------------------------------


def test_autopilot_silent_accepts_and_continues(tmp_path: Path) -> None:
    workspace = tmp_path / "case_auto"
    init_project(workspace, "Бизнес-запрос для autopilot теста.")
    runtime, snapshot, _workflow, _checkpoint, planning_svc, _exec = _bootstrap_services(
        workspace, _make_business_decision
    )
    planning_svc.expand_graph(workspace, snapshot)
    task = _first_leaf_task_with_artifact(runtime, workspace, snapshot)

    # Переводим проект в autopilot
    from pov_generator.domain.process_state import SetClarificationModePatch
    runtime.apply_process_patch(
        workspace,
        SetClarificationModePatch(mode="autopilot"),
        actor="test",
        reason="set autopilot",
    )

    bundle = _exec.execute_task(workspace, snapshot, task.task_id, provider="stub")
    # autopilot → ничего не surfaced → не paused, идём в основную генерацию (stub)
    assert bundle.result.status == "succeeded"
    # Решение всё равно в реестре, но как accepted_default
    project_id = runtime.load_project_state(workspace).manifest.project_id
    decisions = runtime.list_decisions(workspace, project_id=project_id)
    assert len(decisions) == 1
    assert decisions[0].status == "accepted_default"
    assert decisions[0].user_action == "not_shown"
    # И никакой сессии не было создано
    sessions = runtime.list_checkpoint_sessions(workspace, project_id=project_id)
    assert sessions == []


# ---------------------------------------------------------------------------
# Сценарий 3: balanced + detail decision → silent, без паузы
# ---------------------------------------------------------------------------


def test_balanced_detail_decision_silent_no_pause(tmp_path: Path) -> None:
    workspace = tmp_path / "case_detail"
    init_project(workspace, "Бизнес-запрос для detail-only.")
    runtime, snapshot, _wf, _cp, planning_svc, _exec = _bootstrap_services(
        workspace, _make_detail_decision
    )
    planning_svc.expand_graph(workspace, snapshot)
    task = _first_leaf_task_with_artifact(runtime, workspace, snapshot)
    bundle = _exec.execute_task(workspace, snapshot, task.task_id, provider="stub")

    # balanced + detail → silent → задача проходит
    assert bundle.result.status == "succeeded"


# ---------------------------------------------------------------------------
# Сценарий 4: submit + retry → pre-flight НЕ повторяется, locked-in
#             решение попадает в промпт
# ---------------------------------------------------------------------------


def test_submit_then_retry_uses_finalized_session(tmp_path: Path) -> None:
    workspace = tmp_path / "case_resume"
    init_project(workspace, "Бизнес-запрос для resume теста.")
    runtime, snapshot, _wf, checkpoint_svc, planning_svc, _exec = _bootstrap_services(
        workspace, _make_business_decision
    )
    planning_svc.expand_graph(workspace, snapshot)
    task = _first_leaf_task_with_artifact(runtime, workspace, snapshot)
    project_id = runtime.load_project_state(workspace).manifest.project_id

    # 1. Первый запуск: pause
    first = _exec.execute_task(workspace, snapshot, task.task_id, provider="stub")
    assert first.result.status == "paused_for_checkpoint"
    session_id = first.result.checkpoint_session_id

    # 2. Пользователь финализирует — выбирает альтернативу
    checkpoint_svc.submit_answers(
        workspace,
        session_id=session_id,
        answers=(
            CheckpointAnswer(
                decision_id=f"d-{task.task_id}",
                kind="select_alternative",
                selected_option_id="opt-retail",
            ),
        ),
    )

    # 3. Повторный запуск задачи: pre-flight видит finalized session,
    #    НЕ создаёт новых решений, идёт в stub-генерацию
    second = _exec.execute_task(workspace, snapshot, task.task_id, provider="stub")
    assert second.result.status == "succeeded"

    # 4. В реестре по-прежнему одно решение — с обновлённым выбором
    decisions = runtime.list_decisions(workspace, project_id=project_id)
    assert len(decisions) == 1
    assert decisions[0].chosen_option_id == "opt-retail"
    assert decisions[0].status == "user_overridden"


# ---------------------------------------------------------------------------
# Сценарий 5: locked-in декларации действительно в промпте
# ---------------------------------------------------------------------------


def test_locked_in_decisions_appear_in_prompt(tmp_path: Path) -> None:
    """После финализации сессии, при повторном запуске задачи блок
    <locked_in_decisions> должен оказаться в user_prompt (мы видим это
    через execution trace prompt_bundle)."""
    workspace = tmp_path / "case_prompt"
    init_project(workspace, "Бизнес-запрос для проверки промпта.")
    runtime, snapshot, _wf, checkpoint_svc, planning_svc, _exec = _bootstrap_services(
        workspace, _make_business_decision
    )
    planning_svc.expand_graph(workspace, snapshot)
    task = _first_leaf_task_with_artifact(runtime, workspace, snapshot)

    # Запускаем, финализируем, ретраим
    first = _exec.execute_task(workspace, snapshot, task.task_id, provider="stub")
    checkpoint_svc.submit_answers(
        workspace,
        session_id=first.result.checkpoint_session_id,
        answers=(
            CheckpointAnswer(
                decision_id=f"d-{task.task_id}", kind="accept_default"
            ),
        ),
    )
    second = _exec.execute_task(workspace, snapshot, task.task_id, provider="stub")

    # Достаём prompt_bundle trace, проверяем что в user_prompt есть блок
    import json as _json

    trace_ids = second.result.trace_ids
    prompt_trace_content = None
    for trace in second.traces:
        if trace.trace_type == "prompt_bundle":
            prompt_trace_content = trace.content
            break
    assert prompt_trace_content is not None
    data = _json.loads(prompt_trace_content)
    assert "<locked_in_decisions>" in data["user_prompt"]
    assert "Целевая аудитория сервиса" in data["user_prompt"]
    assert "Корпоративные клиенты" in data["user_prompt"]


# ---------------------------------------------------------------------------
# Сценарий 6: full workflow via WorkflowService — задача переходит в failed
# ---------------------------------------------------------------------------


def test_workflow_service_marks_task_failed_on_pause(tmp_path: Path) -> None:
    """Через WorkflowService.run_next, задача с pause переходит в failed
    (с error_type='paused_for_checkpoint'). Это даёт UI возможность
    показать «приостановлено», и после submit_answers transition_task
    переведёт её обратно в ready."""
    workspace = tmp_path / "case_wf"
    init_project(workspace, "Workflow-уровень теста.")
    runtime, snapshot, workflow_svc, checkpoint_svc, planning_svc, _exec = _bootstrap_services(
        workspace, _make_business_decision
    )
    planning_svc.expand_graph(workspace, snapshot)
    project_id = runtime.load_project_state(workspace).manifest.project_id

    step = workflow_svc.run_next(workspace, snapshot, provider="stub")
    assert step.validation_status == "paused_for_checkpoint"
    assert step.checkpoint_session_id is not None
    # Task в failed
    task = runtime.get_task(workspace, step.task_id)
    assert task.status == "failed"

    # submit_answers → авто-resume: задача обратно в ready/blocked
    checkpoint_svc.submit_answers(
        workspace,
        session_id=step.checkpoint_session_id,
        answers=(
            CheckpointAnswer(
                decision_id=f"d-{step.task_id}", kind="accept_default"
            ),
        ),
    )
    task_after = runtime.get_task(workspace, step.task_id)
    assert task_after.status in {"ready", "blocked"}
