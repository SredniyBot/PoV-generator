"""Тесты для B5 — Prompt Authority Layer.

Это не «добавили данных в контекст», это «сказали LLM как с этими
данными работать». Без этого слоя decision от пользователя — просто
ещё один кусок текста среди прочих; LLM может проигнорировать.

Слои защиты:
1. system_prompt содержит явную иерархию источников (USER DECISIONS
   обязательны, ASSUMPTIONS можно override, и т.д.)
2. Project state в user_prompt маркируется значками 🟢🟡🔵⚫ — визуальная
   сигнализация уровней доверия.
3. Negative space: при ответе пользователя ранее принятая по тому же
   вопросу assumption удаляется из ProblemState; при reopen ранее
   зафиксированное decision удаляется.
4. Reasoning artifact содержит applied_decisions — список ответов,
   повлиявших на reasoning.
5. Token budget cap на project_state — защита от outsize.
"""

from __future__ import annotations

from pathlib import Path

from pov_generator.application.clarification_service import ClarificationService
from pov_generator.application.context_service import ContextService
from pov_generator.application.execution_service import ExecutionService
from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.domain.clarifications import ClarificationOption
from pov_generator.domain.positions import Position
from pov_generator.domain.project_knowledge import (
    RejectPositionPatch,
    UpsertPositionPatch,
)
from pov_generator.domain.registry import ObjectRef


def _make_position(
    identifier: str,
    position_type: str,
    statement: str,
    *,
    source: str = "system",
    visibility: str = "architectural",
    scope: str = "global",
    tags: tuple[str, ...] = (),
) -> Position:
    return Position(
        identifier=identifier,
        type=position_type,  # type: ignore[arg-type]
        statement=statement,
        visibility=visibility,  # type: ignore[arg-type]
        scope=scope,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        taken_by="test",
        taken_at="2026-05-13T10:00:00+00:00",
        tags=tags,
    )
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
        name="prompt authority",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="Запрос на разработку сервиса с интеграцией.",
        domain_packs=(),
    )
    planning_service.expand_graph(workspace, snapshot)
    return workspace, bootstrap.manifest.project_id, runtime, clar_service, registry_service


def _first_leaf_no_input(runtime, workspace, snapshot):
    for t in runtime.list_tasks(workspace):
        if t.template_type != "leaf":
            continue
        tpl = snapshot.resolve_template(t.template_ref)
        if not tpl.inputs.required_artifact_roles:
            return t
    raise AssertionError("нужна хотя бы одна leaf без required artifacts")


# ---------------------------------------------------------------------------
# B5-1: system_prompt содержит иерархию источников
# ---------------------------------------------------------------------------


def test_system_prompt_contains_source_hierarchy() -> None:
    """system_prompt должен явно объяснять LLM что user decisions обязательны.

    Это вытаскивается из execution_service._build_prompt напрямую — не
    нужен полный pipeline.
    """
    # Воспроизводим вызов _build_prompt с минимальным mock context_manifest
    from pov_generator.application import execution_service as exec_mod

    class MockCtx:
        items: list = []

    svc = exec_mod.ExecutionService.__new__(exec_mod.ExecutionService)
    system_prompt, _user = svc._build_prompt(
        template_name="dummy",
        task_summary="dummy",
        artifact_role="goal_hypothesis",
        domain_pack_refs=(),
        current_step_title="dummy",
        context_manifest=MockCtx(),
    )
    # Ключевые маркеры иерархии должны присутствовать (новый формат:
    # XML-блоки <source_hierarchy>, <writing_principles>, и т.д.).
    assert "source_hierarchy" in system_prompt.lower() or "ИЕРАРХИЯ ИСТОЧНИКОВ" in system_prompt
    assert "🟢" in system_prompt and "РЕШЕНИЯ ПОЛЬЗОВАТЕЛЯ" in system_prompt
    assert "🟡" in system_prompt and "ДОПУЩЕНИЯ" in system_prompt
    assert "⚫" in system_prompt
    # Явные правила: решения пользователя обязательны (формат может быть
    # «не оспаривай» или «не подлежат пересмотру»).
    assert (
        "не подлежат пересмотру" in system_prompt.lower()
        or "не оспаривай" in system_prompt.lower()
        or "обязательные ограничения" in system_prompt.lower()
    )
    assert "blocking_questions" in system_prompt


# ---------------------------------------------------------------------------
# B5-2: project state с visual markers + дисклеймером
# ---------------------------------------------------------------------------


def test_project_state_uses_visual_markers(tmp_path: Path) -> None:
    workspace, project_id, runtime, clar_service, reg = _bootstrap(tmp_path)
    snapshot, _ = reg.validate()
    ctx = ContextService(runtime)
    task = _first_leaf_no_input(runtime, workspace, snapshot)

    # Создаём 1 decision + 1 assumption + 1 gap
    candidate = clar_service.candidate_from_question(
        project_id=project_id,
        source_type="validation",
        source_id=f"{task.task_id}:stub:question:1",
        question="Какой целевой объём пользователей?",
        affected_task_ids=(task.task_id,),
        related_artifact_ids=(),
        severity="high",
        confidence_without_user=0.3,
        options=(ClarificationOption(option_id="opt", label="100К"),),
        decision_owner_role="business",
        visibility="architectural",
    )
    [d] = clar_service.register_candidates(workspace, (candidate,))
    clar_service.answer_clarification(workspace, request_id=d.request_id, selected_option_ids=("opt",))

    runtime.apply_knowledge_patch(
        workspace,
        UpsertPositionPatch(
            position=_make_position("a1", "assumption", "Бэкап ежедневный"),
        ),
        actor="test",
        reason="add assumption",
    )

    result = ctx.build_for_task(workspace, snapshot, task.task_id)
    state_content = next(i.content for i in result.manifest.items if i.title == "Контекст проекта")
    # Visual markers
    assert "🟢" in state_content
    assert "🟡" in state_content
    # Disclaimer iерархии в самом intro
    assert "иерархи" in state_content.lower()
    # Decision явно маркирован "ОБЯЗАТЕЛЬНО"
    assert "ОБЯЗАТЕЛЬНО" in state_content


# ---------------------------------------------------------------------------
# B5-3: дедупликация task_summary
# ---------------------------------------------------------------------------


def test_task_summary_not_duplicated_in_user_prompt(tmp_path: Path) -> None:
    """task_summary раньше включался в user_prompt как "Что нужно сделать"
    И ОДНОВРЕМЕННО как context_manifest item "Что должна сделать задача".
    Теперь только через context_manifest — никакого дубля."""
    from pov_generator.application import execution_service as exec_mod
    from pov_generator.domain.artifacts import ContextItem

    summary = "Сформулировать гипотезу цели"
    fake_item = ContextItem(
        item_id="x",
        item_type="instruction",
        source_ref="t",
        title="Что должна сделать задача",
        content=summary,
        token_estimate=10,
        required=True,
        priority=1000,
    )

    class MockCtx:
        items = [fake_item]

    svc = exec_mod.ExecutionService.__new__(exec_mod.ExecutionService)
    _, user_prompt = svc._build_prompt(
        template_name="dummy",
        task_summary=summary,
        artifact_role="goal_hypothesis",
        domain_pack_refs=(),
        current_step_title="title",
        context_manifest=MockCtx(),
    )
    # summary должен встретиться в prompt ровно один раз (внутри context).
    assert user_prompt.count(summary) == 1
    # И «Что нужно сделать:» не должен присутствовать как отдельная строка.
    assert "Что нужно сделать:" not in user_prompt


# ---------------------------------------------------------------------------
# B5-4: negative space — assumption удаляется при answer; decision при reopen
# ---------------------------------------------------------------------------


def test_assumption_removed_when_user_answers_same_question(tmp_path: Path) -> None:
    """Сценарий: система приняла assumption по вопросу X.
    Потом другая задача задала тот же вопрос X (cross-task duplicate).
    Пользователь ответил явно. Старая assumption должна уйти из state.

    Этап 3: чтобы кандидат стал ``assumed``, нужно либо подобрать
    ``visibility=technical`` (на balanced/control не выводится), либо
    переключить mode в autopilot. Берём technical — это деталь стека.
    """
    workspace, project_id, runtime, clar_service, reg = _bootstrap(tmp_path)

    # 1) Создаём candidate, который превратится в assumption.
    # visibility=technical → balanced (default mode) не proactive → есть
    # default_assumption → assume.
    cand_for_assumption = clar_service.candidate_from_question(
        project_id=project_id,
        source_type="validation",
        source_id="task-A:role-x:question:hash1",
        question="Какой стек разработки?",
        affected_task_ids=("task-A",),
        related_artifact_ids=(),
        severity="medium",
        confidence_without_user=0.85,
        options=(ClarificationOption(option_id="o", label="default"),),
        decision_owner_role="architect",
        visibility="technical",
        default_assumption="Python + FastAPI (по умолчанию)",
    )
    [d_assume] = clar_service.register_candidates(workspace, (cand_for_assumption,))
    assume_req_id = d_assume.request_id
    assume_req = runtime.get_clarification_request(workspace, assume_req_id)
    assert assume_req.status == "assumed"

    # Подтверждаем что assumption в Layer A
    knowledge_before = runtime.load_knowledge(workspace)
    position_id = f"clarification.{assume_req_id}"
    pos = knowledge_before.get(position_id)
    assert pos is not None and pos.type == "assumption" and pos.status == "active"

    # 2) Другая задача (cross-task) задаёт тот же вопрос — будет
    # reuse_existing assumed request (мой B3 cross-task dedup).
    cand_open = clar_service.candidate_from_question(
        project_id=project_id,
        source_type="validation",
        source_id="task-B:role-y:question:hash2",
        question="Какой стек разработки?",
        affected_task_ids=("task-B",),
        related_artifact_ids=(),
        severity="high",
        confidence_without_user=0.3,
        options=(ClarificationOption(option_id="py", label="Python + FastAPI"),),
        decision_owner_role="business",
        visibility="architectural",
    )
    [d2] = clar_service.register_candidates(workspace, (cand_open,))
    # Поскольку assumed уже есть с тем же вопросом — это reuse_existing.
    # Но нам нужен явный path "пользователь даёт явный ответ" для assume_req.
    # Делаем answer прямо на assumed request — это валидный flow? Нет,
    # answer требует status open/deferred. Симулируем через reopen → answer.
    clar_service.reopen_clarification(workspace, request_id=assume_req_id)
    clar_service.answer_clarification(
        workspace,
        request_id=assume_req_id,
        selected_option_ids=(),
        free_text="Python + FastAPI",
    )

    # После reopen + answer assumption должна быть superseded/rejected
    # (через reopen path) и заменена decision (same identifier, новый type).
    knowledge_after = runtime.load_knowledge(workspace)
    final = knowledge_after.get(position_id)
    assert final is not None
    assert final.type == "decision", "после answer положение должно быть decision"
    assert final.status == "active"


def test_reopen_clarification_removes_previous_decision(tmp_path: Path) -> None:
    workspace, project_id, runtime, clar_service, reg = _bootstrap(tmp_path)
    cand = clar_service.candidate_from_question(
        project_id=project_id,
        source_type="validation",
        source_id="task-A:role-x:question:hash",
        question="Сколько у нас бюджет на инфраструктуру?",
        affected_task_ids=("task-A",),
        related_artifact_ids=(),
        severity="high",
        confidence_without_user=0.3,
        options=(ClarificationOption(option_id="o", label="3 млн"),),
        decision_owner_role="business",
        visibility="architectural",
    )
    [d] = clar_service.register_candidates(workspace, (cand,))
    request_id = d.request_id
    clar_service.answer_clarification(workspace, request_id=request_id, selected_option_ids=("o",))

    knowledge_after_answer = runtime.load_knowledge(workspace)
    position_id = f"clarification.{request_id}"
    after_answer = knowledge_after_answer.get(position_id)
    assert after_answer is not None and after_answer.type == "decision"
    assert after_answer.status == "active"

    clar_service.reopen_clarification(workspace, request_id=request_id)
    knowledge_after_reopen = runtime.load_knowledge(workspace)
    after_reopen = knowledge_after_reopen.get(position_id)
    assert after_reopen is not None and after_reopen.status == "rejected", (
        "при reopen положение должно быть отвергнуто, чтобы не противоречить будущему ответу"
    )


# ---------------------------------------------------------------------------
# B5-5: applied_decisions в reasoning payload
# ---------------------------------------------------------------------------


def test_collect_applied_decisions_filters_by_task_id(tmp_path: Path) -> None:
    workspace, project_id, runtime, clar_service, reg = _bootstrap(tmp_path)
    svc = ExecutionService.__new__(ExecutionService)
    svc._runtime = runtime

    # Decision, affecting только task-A
    runtime.apply_knowledge_patch(
        workspace,
        UpsertPositionPatch(
            position=_make_position(
                identifier="clarification.req-A",
                position_type="decision",
                statement="Используем Python",
                source="clarification",
            )
        ),
        actor="test",
        reason="t",
    )
    # Чтобы фильтрация работала, нам нужен mock clarification request
    # с affected_task_ids. Запишем его прямо через runtime.
    from pov_generator.domain.clarifications import ClarificationRequest
    runtime.create_clarification_request(
        workspace,
        ClarificationRequest(
            request_id="req-A",
            project_id=project_id,
            status="answered",
            priority="high",
            title="Q",
            question="Q?",
            description="",
            reason="",
            impact="",
            answer_mode="single",
            options=(),
            recommended_option_id=None,
            visibility="architectural",
            default_assumption=None,
            affected_task_ids=("task-A",),
            related_artifact_ids=(),
            blocking_scope="task",
            decision_owner_role="business",
            source_type="validation",
            source_id="src",
            created_from_candidate_ids=(),
            selected_option_ids=(),
            free_text=None,
            resolution_summary=None,
            auto_resolved=False,
            created_at="",
            updated_at="",
        ),
    )

    applied_for_a = svc._collect_applied_decisions(workspace, "task-A")
    applied_for_b = svc._collect_applied_decisions(workspace, "task-B")

    target_id = "clarification.req-A"
    assert any(d["decision_id"] == target_id for d in applied_for_a)
    assert not any(d["decision_id"] == target_id for d in applied_for_b), (
        "decision affecting только task-A не должен попадать в applied для task-B"
    )


def test_collect_applied_decisions_includes_global(tmp_path: Path) -> None:
    """Decision без clarification источника (system/operator) считается global."""
    workspace, project_id, runtime, _clar, _reg = _bootstrap(tmp_path)
    svc = ExecutionService.__new__(ExecutionService)
    svc._runtime = runtime

    runtime.apply_knowledge_patch(
        workspace,
        UpsertPositionPatch(
            position=_make_position(
                identifier="global_d",
                position_type="decision",
                statement="Используем dark mode по дефолту",
                source="user",
            )
        ),
        actor="test",
        reason="manual decision",
    )
    applied = svc._collect_applied_decisions(workspace, "task-X")
    assert any(d["decision_id"] == "global_d" for d in applied)


# ---------------------------------------------------------------------------
# B5-6: token budget cap
# ---------------------------------------------------------------------------


def test_project_state_truncated_when_huge(tmp_path: Path, monkeypatch) -> None:
    """Cap-механизм должен срабатывать когда даже отфильтрованный набор
    выходит за token budget. Чтобы тест был детерминированным независимо
    от пределов в filter, временно занижаем cap до маленького значения.
    """
    monkeypatch.setattr(ContextService, "_PROJECT_STATE_TOKEN_HARD_CAP", 100)
    workspace, project_id, runtime, _clar, reg = _bootstrap(tmp_path)
    snapshot, _ = reg.validate()
    ctx = ContextService(runtime)
    task = _first_leaf_no_input(runtime, workspace, snapshot)

    # Добавляем много decisions — берём в качестве relevant (clarification
    # без affected_task_ids — попадает в "relevant" по умолчанию через
    # ContextService._is_fact_relevant_to_task, который default-true).
    # Используем длинные statements чтобы наверняка превысить cap.
    long_statement = "x" * 400
    for i in range(50):
        runtime.apply_knowledge_patch(
            workspace,
            UpsertPositionPatch(
                position=_make_position(
                    identifier=f"d{i}",
                    position_type="decision",
                    statement=f"Решение #{i}: " + long_statement,
                    source="user",
                )
            ),
            actor="test",
            reason="bulk",
        )
    result = ctx.build_for_task(workspace, snapshot, task.task_id)
    state_item = next(i for i in result.manifest.items if i.title == "Контекст проекта")
    # Под cap: должно быть обрезано и помечено
    assert state_item.token_estimate <= 100 + 50
    assert "обрезан" in state_item.content.lower()
