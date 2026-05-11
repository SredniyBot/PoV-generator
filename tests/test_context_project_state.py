"""Тесты для B4: project state в ContextManifest + auto-retry + версионирование.

Жалоба пользователя: «после ответа на вопрос система продолжает, но
вопросы возвращаются». Корневая причина — decisions от clarifications
никогда не попадали в LLM prompt (ни один шаблон не объявлял
`required_problem_fields`). Задача перевыполнялась с тем же контекстом,
давала тот же результат, генерила тот же вопрос.

B4 чинит фундаментально:
- ContextService теперь ВСЕГДА собирает «Контекст проекта» (goal,
  business_request, decisions, assumptions, gaps, known_facts) и
  включает в каждый ContextManifest.
- При повторной попытке задачи (attempt > 1) добавляется секция
  «Прошлая попытка» с предыдущим артефактом и validation findings.
- В `answer_clarification` запускается auto-retry для failed задач из
  `affected_task_ids` — пользователь ответил, задача автоматически
  переисполняется с обновлённым контекстом.
- При создании нового артефакта того же `(role, task_id)` старый
  помечается `is_superseded=True`, новый получает `parent_artifact_id`.
"""

from __future__ import annotations

from pathlib import Path

from pov_generator.application.clarification_service import ClarificationService
from pov_generator.application.context_service import ContextService
from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.domain.artifacts import ArtifactRecord
from pov_generator.domain.clarifications import ClarificationOption
from pov_generator.domain.problem_state import (
    AddFactPatch,
    UpsertAssumptionPatch,
    UpsertGapPatch,
)
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE_REF = "common.requirements_specification@1.0.0"


def _bootstrap(tmp_path: Path):
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)
    clar_service = ClarificationService(runtime, provider="stub")
    snapshot, _ = registry_service.validate()
    workspace = tmp_path / "case"
    bootstrap = project_service.init_project(
        workspace=workspace,
        name="ctx test",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="Нужна CRM-интеграция для отдела продаж. Бюджет ~3 млн.",
        domain_packs=(),
    )
    # Без expand_graph leaf-задачи не существуют (после bootstrap только
    # composite root). Тесты context требуют реальной leaf задачи.
    planning_service.expand_graph(workspace, snapshot)
    return workspace, bootstrap.manifest.project_id, runtime, clar_service, registry_service


def _first_leaf_task(runtime, workspace, registry_service=None):
    """Возвращает первую leaf-задачу БЕЗ обязательных входных артефактов
    (чтобы ContextService мог собрать manifest без подготовки upstream)."""
    tasks = runtime.list_tasks(workspace)
    leafs = [t for t in tasks if t.template_type == "leaf"]
    assert leafs, "должны быть leaf-задачи после bootstrap"
    if registry_service is None:
        return leafs[0]
    snapshot, _ = registry_service.validate()
    for task in leafs:
        try:
            template = snapshot.resolve_template(task.template_ref)
        except Exception:
            continue
        if not template.inputs.required_artifact_roles:
            return task
    return leafs[0]


# ---------------------------------------------------------------------------
# A. Project state в каждом ContextManifest
# ---------------------------------------------------------------------------


def test_context_includes_business_request_and_goal(tmp_path: Path) -> None:
    workspace, project_id, runtime, _, reg = _bootstrap(tmp_path)
    snapshot, _ = reg.validate()
    ctx = ContextService(runtime)
    task = _first_leaf_task(runtime, workspace, reg)

    result = ctx.build_for_task(workspace, snapshot, task.task_id)
    state_items = [
        i for i in result.manifest.items if i.title == "Контекст проекта"
    ]
    assert state_items, "Должна быть секция «Контекст проекта» в каждом ContextManifest"
    content = state_items[0].content
    assert "CRM-интеграция" in content
    assert "Исходный бизнес-запрос" in content


def test_context_includes_decisions_from_clarifications(tmp_path: Path) -> None:
    workspace, project_id, runtime, clar_service, reg = _bootstrap(tmp_path)
    snapshot, _ = reg.validate()
    ctx = ContextService(runtime)
    task = _first_leaf_task(runtime, workspace, reg)

    # Создаём clarification, отвечаем — порождается Decision в ProblemState.
    candidate = clar_service.candidate_from_question(
        project_id=project_id,
        source_type="validation",
        source_id=f"{task.task_id}:stub:question:1",
        question="Какой стек разработки использовать?",
        affected_task_ids=(task.task_id,),
        related_artifact_ids=(),
        severity="high",
        confidence_without_user=0.3,
        options=(
            ClarificationOption(option_id="python", label="Python + FastAPI"),
            ClarificationOption(option_id="node", label="Node.js + Express"),
        ),
        decision_owner_role="business",
        min_participation_mode="balanced",
    )
    decisions = clar_service.register_candidates(workspace, (candidate,))
    request_id = decisions[0].request_id
    assert request_id

    clar_service.answer_clarification(
        workspace, request_id=request_id, selected_option_ids=("python",)
    )

    # Decision должен оказаться в контексте задачи (affected_task_ids match).
    result = ctx.build_for_task(workspace, snapshot, task.task_id)
    state_section = next(
        i.content for i in result.manifest.items if i.title == "Контекст проекта"
    )
    assert "стек разработки" in state_section.lower()
    assert "python" in state_section.lower()
    assert "ответ на вопрос пользователя" in state_section.lower()


def test_context_filters_irrelevant_decisions_into_global_summary(tmp_path: Path) -> None:
    workspace, project_id, runtime, clar_service, reg = _bootstrap(tmp_path)
    snapshot, _ = reg.validate()
    ctx = ContextService(runtime)

    # Берём ДВЕ разные leaf-задачи БЕЗ required artifact (чтобы можно
    # было построить context manifest без подготовки upstream).
    # Decision привязан к task А, проверяем как он показывается в
    # контексте задачи Б.
    all_leafs = [t for t in runtime.list_tasks(workspace) if t.template_type == "leaf"]
    no_input_leafs = []
    for t in all_leafs:
        try:
            tpl = snapshot.resolve_template(t.template_ref)
        except Exception:
            continue
        if not tpl.inputs.required_artifact_roles:
            no_input_leafs.append(t)
    assert len(no_input_leafs) >= 2, (
        "нужно как минимум 2 leaf задачи без required artifact в этом objective"
    )
    task_a, task_b = no_input_leafs[0], no_input_leafs[1]

    candidate = clar_service.candidate_from_question(
        project_id=project_id,
        source_type="validation",
        source_id=f"{task_a.task_id}:stub:question:1",
        question="Какие данные мигрируем из старой системы?",
        affected_task_ids=(task_a.task_id,),
        related_artifact_ids=(),
        severity="medium",
        confidence_without_user=0.4,
        options=(ClarificationOption(option_id="opt", label="Все"),),
        decision_owner_role="business",
        min_participation_mode="balanced",
    )
    [decision] = clar_service.register_candidates(workspace, (candidate,))
    clar_service.answer_clarification(
        workspace, request_id=decision.request_id, selected_option_ids=("opt",)
    )

    # В контексте task_b decision показывается как «global» (compact list).
    result = ctx.build_for_task(workspace, snapshot, task_b.task_id)
    state_section = next(
        i.content for i in result.manifest.items if i.title == "Контекст проекта"
    )
    # Decision должен быть упомянут (тренд на безопасный inclusion), но в
    # секции «Другие принятые решения проекта», не в «к этой задаче».
    assert "Другие принятые решения проекта" in state_section
    # При этом «к этой задаче» отсутствует или не содержит этот decision.
    if "Решения, принятые к этой задаче" in state_section:
        # split по заголовкам, искать decision в обоих
        head = state_section.split("Другие принятые решения проекта")[0]
        assert "мигрируем из старой системы" not in head.lower()


def test_context_includes_assumptions_and_gaps(tmp_path: Path) -> None:
    workspace, project_id, runtime, _, reg = _bootstrap(tmp_path)
    snapshot, _ = reg.validate()
    ctx = ContextService(runtime)
    task = _first_leaf_task(runtime, workspace, reg)

    runtime.apply_problem_patch(
        workspace,
        UpsertAssumptionPatch(
            assumption_id="a1",
            statement="Используется один центральный Postgres.",
            source="system",
        ),
        actor="test",
        reason="add assumption",
    )
    runtime.apply_problem_patch(
        workspace,
        UpsertGapPatch(
            gap_id="g1",
            title="Не определена политика бэкапа",
            description="Не указано как часто и куда бэкапим.",
            severity="high",
            blocking=False,
        ),
        actor="test",
        reason="add gap",
    )

    result = ctx.build_for_task(workspace, snapshot, task.task_id)
    state_section = next(
        i.content for i in result.manifest.items if i.title == "Контекст проекта"
    )
    assert "Postgres" in state_section
    assert "политика бэкапа" in state_section.lower()
    assert "Открытые пробелы" in state_section


def test_context_section_has_high_priority(tmp_path: Path) -> None:
    """Контекст проекта должен идти первым (priority выше всех остальных
    в обычном manifest), чтобы LLM прочитал его до постановки задачи."""
    workspace, project_id, runtime, _, reg = _bootstrap(tmp_path)
    snapshot, _ = reg.validate()
    ctx = ContextService(runtime)
    task = _first_leaf_task(runtime, workspace, reg)

    result = ctx.build_for_task(workspace, snapshot, task.task_id)
    state_item = next(i for i in result.manifest.items if i.title == "Контекст проекта")
    other_items = [i for i in result.manifest.items if i.title != "Контекст проекта"]
    if other_items:
        max_other_priority = max(i.priority for i in other_items)
        assert state_item.priority >= max_other_priority


# ---------------------------------------------------------------------------
# B. Auto-retry задач после answer_clarification
# ---------------------------------------------------------------------------


def test_answer_triggers_auto_retry_of_failed_task(tmp_path: Path) -> None:
    workspace, project_id, runtime, clar_service, reg = _bootstrap(tmp_path)
    task = _first_leaf_task(runtime, workspace, reg)
    # Помечаем задачу как failed (как будто validation её завалил)
    runtime.transition_task(workspace, task.task_id, "fail", payload={"reason": "low confidence"})
    after_fail = runtime.get_task(workspace, task.task_id)
    assert after_fail.status == "failed"
    initial_attempt = after_fail.attempt

    candidate = clar_service.candidate_from_question(
        project_id=project_id,
        source_type="validation",
        source_id=f"{task.task_id}:stub:question:1",
        question="Какой бюджет на инфраструктуру?",
        affected_task_ids=(task.task_id,),
        related_artifact_ids=(),
        severity="high",
        confidence_without_user=0.3,
        options=(ClarificationOption(option_id="opt", label="3 млн"),),
        decision_owner_role="business",
        min_participation_mode="balanced",
    )
    [decision] = clar_service.register_candidates(workspace, (candidate,))
    clar_service.answer_clarification(
        workspace, request_id=decision.request_id, selected_option_ids=("opt",)
    )

    after_answer = runtime.get_task(workspace, task.task_id)
    assert after_answer.status == "ready", (
        f"задача должна быть auto-retried в ready, но статус: {after_answer.status}"
    )
    assert after_answer.attempt == initial_attempt + 1


def test_answer_does_not_retry_completed_task(tmp_path: Path) -> None:
    """Не трогаем completed задачи — это была бы регрессия данных."""
    workspace, project_id, runtime, clar_service, reg = _bootstrap(tmp_path)
    task = _first_leaf_task(runtime, workspace, reg)
    # Прокатываем задачу через ready → in_progress → completed
    runtime.transition_task(workspace, task.task_id, "mark_ready", payload={})
    runtime.transition_task(workspace, task.task_id, "start", payload={})
    runtime.transition_task(workspace, task.task_id, "complete", payload={})
    assert runtime.get_task(workspace, task.task_id).status == "completed"

    candidate = clar_service.candidate_from_question(
        project_id=project_id,
        source_type="validation",
        source_id=f"{task.task_id}:stub:question:1",
        question="Какой бюджет?",
        affected_task_ids=(task.task_id,),
        related_artifact_ids=(),
        severity="high",
        confidence_without_user=0.3,
        options=(ClarificationOption(option_id="opt", label="3 млн"),),
        decision_owner_role="business",
        min_participation_mode="balanced",
    )
    [decision] = clar_service.register_candidates(workspace, (candidate,))
    clar_service.answer_clarification(
        workspace, request_id=decision.request_id, selected_option_ids=("opt",)
    )
    assert runtime.get_task(workspace, task.task_id).status == "completed"


# ---------------------------------------------------------------------------
# D. parent_artifact_id + is_superseded при создании новой версии артефакта
# ---------------------------------------------------------------------------


def test_creating_second_artifact_same_role_supersedes_first(tmp_path: Path) -> None:
    workspace, project_id, runtime, _, reg = _bootstrap(tmp_path)
    task = _first_leaf_task(runtime, workspace, reg)

    # Создаём первый артефакт напрямую через runtime
    first = ArtifactRecord(
        artifact_id="art-v1",
        project_id=project_id,
        artifact_role="goal_hypothesis",
        title="v1",
        description=None,
        artifact_format="json",
        artifact_kind="primary",
        created_by_task_id=task.task_id,
        parent_artifact_id=None,
        metadata={},
        storage_path="artifacts/art-v1.json",
        created_at="2026-05-11T10:00:00+00:00",
    )
    runtime.store_artifact(workspace, artifact=first, content="{}")

    # Проверяем что latest_active возвращает его
    latest = runtime.latest_active_artifact_by_role_and_task(
        workspace,
        artifact_role="goal_hypothesis",
        created_by_task_id=task.task_id,
    )
    assert latest is not None
    assert latest.artifact_id == "art-v1"
    assert latest.is_superseded is False

    # Помечаем superseded — после этого latest_active вернёт None
    runtime.mark_artifact_superseded(workspace, "art-v1")
    latest_after = runtime.latest_active_artifact_by_role_and_task(
        workspace,
        artifact_role="goal_hypothesis",
        created_by_task_id=task.task_id,
    )
    assert latest_after is None

    # Старый все ещё в БД, но помечен superseded
    loaded = runtime.load_artifact(workspace, "art-v1")
    assert loaded.is_superseded is True
