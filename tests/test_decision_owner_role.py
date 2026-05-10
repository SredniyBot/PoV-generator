"""Тесты для ortho-оси `decision_owner_role` (W1.2).

Engagement-level менеджера управляется двумя осями:
- `clarification_mode` (autopilot / balanced / control / expert) — частота;
- `decision_owner_role` (business / client / methodologist / architect /
  data_owner / security) — кому адресован вопрос.

Этот файл проверяет, что:
1. Сорсы кандидатов проставляют корректную роль (methodology → methodologist,
   gate с approver_role=client → client).
2. ClarificationService._decide_action учитывает «role floor»: вопрос с
   ролью methodologist на autopilot/balanced принимается допущением, а на
   control/expert — показывается.
3. Поле `decision_owner_role` доходит до `ClarificationRequest` и до
   проекции (`ClarificationItemView`) — UI сможет группировать.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pov_generator.application.clarification_service import ClarificationService
from pov_generator.application.context_service import ContextService
from pov_generator.application.execution_service import ExecutionService
from pov_generator.application.methodology_rules import evaluate_methodology_rules
from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.application.validation_service import (
    ValidationService,
    _normalize_decision_owner_role,
)
from pov_generator.application.workflow_service import WorkflowService
from pov_generator.domain.problem_state import SetClarificationModePatch
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime


REPO_ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE_REF = "common.requirements_specification@1.0.0"


# --- Source emitters --------------------------------------------------------


def test_methodology_rule_candidates_have_methodologist_role() -> None:
    """Кандидат, эмиттированный правилом methodology_pack, должен получить
    `decision_owner_role="methodologist"` — это технический выбор «как
    думаем», не вопрос для бизнес-менеджера."""
    snapshot, _ = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates")).validate()
    methodology = snapshot.resolve_methodology_pack("process.lean_jtbd@1.0.0")

    evaluation = evaluate_methodology_rules(
        methodology=methodology,
        complexity="standard",
        reasoning={"stages": [{"stage_id": "goal_framing", "outputs": {"declared_goal": None}}]},
        project_id="proj-1",
        task_id="task-1",
    )

    assert evaluation.candidates, "пустое правило empty_goal — должно сработать"
    assert all(c.decision_owner_role == "methodologist" for c in evaluation.candidates)


def test_methodology_rule_candidates_have_safe_default_assumption() -> None:
    """Каждый методологический кандидат должен нести `default_assumption`,
    чтобы coordinator на низком engagement мог его тихо принять.
    Без этого role-floor бесполезен — нет другого пути, кроме «спросить»."""
    snapshot, _ = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates")).validate()
    methodology = snapshot.resolve_methodology_pack("process.lean_jtbd@1.0.0")

    reasoning = {
        "stages": [
            {"stage_id": "goal_framing", "outputs": {"declared_goal": None}},
            {
                "stage_id": "option_generation",
                "outputs": {
                    "options": [
                        {"label": "X", "confidence": 0.5},
                        {"label": "Y", "confidence": 0.45},
                    ]
                },
            },
            {"stage_id": "decision", "outputs": {"chosen_option_id": None}},
        ]
    }

    evaluation = evaluate_methodology_rules(
        methodology=methodology,
        complexity="standard",
        reasoning=reasoning,
        project_id="proj-1",
        task_id="task-1",
    )

    fired = [c for c in evaluation.candidates]
    assert fired, "должны сработать empty_goal и ambiguous_choice"
    assert all(c.default_assumption for c in fired)
    # ambiguous_choice должен сослаться на конкретный label с лучшей уверенностью.
    ambiguous = next(c for c in fired if c.source_id.endswith("ambiguous_choice"))
    assert "X" in (ambiguous.default_assumption or "")


@pytest.mark.parametrize(
    "approver_role, expected",
    [
        ("client", "client"),
        ("methodologist", "methodologist"),
        ("architect", "architect"),
        ("dpo", "security"),
        ("ciso", "security"),
        (None, "client"),  # нет роли → default для human_approval = client (внешнее согласование)
        ("какая-то экзотика", "client"),  # неизвестная роль для human_approval gate
    ],
)
def test_normalize_decision_owner_role_maps_gate_approvers(
    approver_role: str | None, expected: str
) -> None:
    """gate.approver_role — свободный словарь spec/02. Маппим к каноничному
    DecisionOwnerRole. Известные алиасы (dpo→security, ciso→security)
    нормализуются; неизвестные роли уходят в `client` (внешнее согласование)."""
    assert _normalize_decision_owner_role(approver_role) == expected


# --- Role floor in _decide_action ------------------------------------------


def _make_runtime_with_workspace(tmp_path: Path) -> tuple[SqliteRuntime, Path]:
    """Минимальный workspace с инициализированным проектом — нужен ради
    `runtime.load_problem_state`, который зовётся `_enrich_candidate`."""
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    workspace = tmp_path / "case"
    project_service.init_project(
        workspace=workspace,
        name="role test",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="role-floor smoke",
        domain_packs=(),
    )
    return runtime, workspace


@pytest.mark.parametrize(
    "mode, expected_action",
    [
        ("autopilot", "assume"),  # methodologist floor=control, mode<floor → assume (есть default)
        ("balanced", "assume"),
        ("control", "ask"),  # mode достиг floor → surface
        ("expert", "ask"),
    ],
)
def test_methodologist_role_floor_filters_by_engagement(
    tmp_path: Path, mode: str, expected_action: str
) -> None:
    """Менеджер на autopilot/balanced не должен получать методологические
    развилки — coordinator принимает default_assumption тихо. На
    control/expert вопрос показывается."""
    runtime, workspace = _make_runtime_with_workspace(tmp_path)
    runtime.apply_problem_patch(
        workspace,
        SetClarificationModePatch(mode=mode),  # type: ignore[arg-type]
        actor="test",
        reason="set engagement level",
    )
    service = ClarificationService(runtime, provider="stub")

    snapshot, _ = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates")).validate()
    methodology = snapshot.resolve_methodology_pack("process.lean_jtbd@1.0.0")
    candidates = evaluate_methodology_rules(
        methodology=methodology,
        complexity="standard",
        reasoning={"stages": [{"stage_id": "goal_framing", "outputs": {"declared_goal": None}}]},
        project_id="proj-1",
        task_id="task-1",
    ).candidates
    assert candidates

    decisions = service.register_candidates(workspace, candidates)
    assert all(d.action == expected_action for d in decisions)


def test_business_role_surfaces_at_any_engagement(tmp_path: Path) -> None:
    """Бизнес-вопросы (роль `business`) должны показываться на любом режиме
    с автопилотом включительно — это вопросы прямо для менеджера."""
    runtime, workspace = _make_runtime_with_workspace(tmp_path)
    runtime.apply_problem_patch(
        workspace,
        SetClarificationModePatch(mode="autopilot"),
        actor="test",
        reason="set autopilot",
    )
    service = ClarificationService(runtime, provider="stub")

    candidate = service.candidate_from_question(
        project_id="proj-1",
        source_type="task",
        source_id="task.business_goal",
        question="Какой ключевой бизнес-результат должен подтвердить успех пилота?",
        affected_task_ids=("task-1",),
        related_artifact_ids=(),
        severity="high",
        confidence_without_user=0.2,
        decision_owner_role="business",
    )
    decisions = service.register_candidates(workspace, (candidate,))
    assert decisions[0].action == "ask"


def test_decision_owner_role_persists_to_request_and_view(tmp_path: Path) -> None:
    """`decision_owner_role` должен дойти от candidate до ClarificationRequest
    в SQLite и до ClarificationItemView в проекции — иначе UI не сможет
    группировать вопросы по роли."""
    from pov_generator.application.workspace_catalog import WorkspaceCatalog
    from pov_generator.application.workspace_query_service import WorkspaceQueryService

    runtime, workspace = _make_runtime_with_workspace(tmp_path)
    service = ClarificationService(runtime, provider="stub")
    candidate = service.candidate_from_question(
        project_id="proj-1",
        source_type="validation",
        source_id="task-1:artifact-1:demo",
        question="Подтверждаете ли область PoV?",
        affected_task_ids=("task-1",),
        related_artifact_ids=(),
        severity="high",
        confidence_without_user=0.2,
        decision_owner_role="client",
    )
    service.register_candidates(workspace, (candidate,))

    [request] = runtime.list_clarification_requests(workspace)
    assert request.decision_owner_role == "client"

    catalog = WorkspaceCatalog(workspace.parent, runtime)
    qs = WorkspaceQueryService(
        catalog,
        RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates")),
        runtime,
        PlanningService(runtime),
    )
    pid = runtime.load_manifest(workspace).project_id
    view = qs.project_clarifications(pid)
    assert any(item.decision_owner_role == "client" for item in view.items)


def test_human_approval_gate_candidate_inherits_role_from_approver(tmp_path: Path) -> None:
    """gate `client.requirements_signoff` имеет approver_role=client →
    candidate должен получить `decision_owner_role=client`. Это позволяет
    UI выделить sign-off как «внешнее согласование», а не как методологию."""
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)
    context_service = ContextService(runtime)
    execution_service = ExecutionService(runtime, context_service)
    clarification_service = ClarificationService(runtime, provider="stub")
    validation_service = ValidationService(runtime, clarification_service)
    workflow_service = WorkflowService(runtime, planning_service, execution_service, validation_service)

    snapshot, _ = registry_service.validate()
    workspace = tmp_path / "case"
    project_service.init_project(
        workspace=workspace,
        name="signoff role",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="role smoke",
        domain_packs=(),
    )
    planning_service.expand_graph(workspace, snapshot)
    workflow_service.run_until_blocked(workspace, snapshot, provider="stub", max_steps=50)

    signoff = next(
        req
        for req in runtime.list_clarification_requests(workspace)
        if req.source_type == "quality_gate"
        and req.source_id == "client.requirements_signoff@1.0.0"
    )
    assert signoff.decision_owner_role == "client"
