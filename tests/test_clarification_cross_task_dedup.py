"""Тесты для B3 (систематический фикс «вопросы возвращаются после ответа»).

Жалоба пользователя: ответ на вопрос не предотвращает появление того же
вопроса от ДРУГОЙ задачи. B2 чинил один частный случай (нестабильный
artifact_id в source_id для re-run одной задачи); B3 чинит системный
случай — несколько разных задач независимо ловят один и тот же
бизнес-пробел и задают семантически одинаковый вопрос.

Layer 1: register_candidates должен находить existing request с тем же
текстом вопроса даже когда source_type/source_id отличаются (cross-task
dedup на уровне проекта).

Layer 2: при ответе на один request все остальные OPEN-дубли с тем же
вопросом должны автоматически закрываться как deferred с пометкой
"resolved_via:{request_id}".
"""

from __future__ import annotations

from pathlib import Path

from pov_generator.application.clarification_service import ClarificationService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.domain.clarifications import ClarificationCandidate, ClarificationOption
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
    workspace = tmp_path / "case"
    bootstrap = project_service.init_project(
        workspace=workspace,
        name="cross-task dedup",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="t",
        domain_packs=(),
    )
    return workspace, bootstrap.manifest.project_id, runtime, service


def _make_candidate(
    service: ClarificationService,
    project_id: str,
    *,
    source_type: str,
    source_id: str,
    question: str,
) -> ClarificationCandidate:
    return service.candidate_from_question(
        project_id=project_id,
        source_type=source_type,  # type: ignore[arg-type]
        source_id=source_id,
        question=question,
        affected_task_ids=(),
        related_artifact_ids=(),
        severity="high",
        confidence_without_user=0.4,
        options=(
            ClarificationOption(option_id="opt_a", label="Вариант A"),
            ClarificationOption(option_id="opt_b", label="Вариант B"),
        ),
        decision_owner_role="business",
        # balanced — режим по умолчанию в bootstrap, _decide_action даст "ask".
        min_participation_mode="balanced",
    )


# ---------------------------------------------------------------------------
# Layer 1 — cross-task dedup при регистрации новых кандидатов
# ---------------------------------------------------------------------------


def test_cross_task_dedup_reuses_existing_open_request(tmp_path: Path) -> None:
    """Когда две разные задачи (с разными source_id) задают семантически
    одинаковый вопрос — второй candidate должен переиспользовать первый
    request, а не создавать дубль."""
    workspace, project_id, runtime, service = _bootstrap(tmp_path)

    cand_from_task_1 = _make_candidate(
        service, project_id,
        source_type="validation",
        source_id="task-AAA:requirements_spec:question:hash1",
        question="Какие данные из 1С будут доступны в реальном времени?",
    )
    cand_from_task_2 = _make_candidate(
        service, project_id,
        source_type="validation",
        source_id="task-BBB:other_role:question:hash2",
        # Тот же вопрос — другая формулировка регистра/пробелов
        question="  Какие данные из 1С будут доступны в реальном времени?  ",
    )

    decisions_1 = service.register_candidates(workspace, (cand_from_task_1,))
    decisions_2 = service.register_candidates(workspace, (cand_from_task_2,))

    assert len(decisions_1) == 1
    assert decisions_1[0].action in {"ask", "assume", "defer"}
    first_request_id = decisions_1[0].request_id

    assert len(decisions_2) == 1
    assert decisions_2[0].action == "reuse_existing"
    assert decisions_2[0].request_id == first_request_id

    # В базе должен быть только один request с этим вопросом.
    all_requests = runtime.list_clarification_requests(workspace)
    matching = [
        r for r in all_requests
        if "данные из 1с" in r.question.casefold()
    ]
    assert len(matching) == 1


def test_cross_task_dedup_prefers_answered_over_open(tmp_path: Path) -> None:
    """Если уже есть answered request — новый candidate с тем же вопросом
    переиспользует именно его (а не создаёт open для другой задачи)."""
    workspace, project_id, runtime, service = _bootstrap(tmp_path)

    cand_1 = _make_candidate(
        service, project_id,
        source_type="validation",
        source_id="task-AAA:role-x:question:h1",
        question="Какой объём миграции из старой CRM?",
    )
    decisions_1 = service.register_candidates(workspace, (cand_1,))
    first_request_id = decisions_1[0].request_id
    assert first_request_id is not None

    # Пользователь отвечает.
    service.answer_clarification(
        workspace,
        request_id=first_request_id,
        selected_option_ids=("opt_a",),
    )

    # Другая задача задаёт тот же вопрос.
    cand_2 = _make_candidate(
        service, project_id,
        source_type="validation",
        source_id="task-BBB:role-y:question:h9",
        question="Какой объём миграции из старой CRM?",
    )
    decisions_2 = service.register_candidates(workspace, (cand_2,))

    assert decisions_2[0].action == "reuse_existing"
    assert decisions_2[0].request_id == first_request_id
    reused = runtime.get_clarification_request(workspace, first_request_id)
    assert reused.status == "answered"


# ---------------------------------------------------------------------------
# Layer 2 — propagation ответа на дубли
# ---------------------------------------------------------------------------


def test_answer_propagates_to_open_duplicates(tmp_path: Path) -> None:
    """Когда уже есть 2 open requests с одним вопросом (старые данные до
    B3 layer 1 — или баг в источнике), ответ на один из них должен
    закрыть второй как deferred с пометкой resolved_via."""
    workspace, project_id, runtime, service = _bootstrap(tmp_path)

    # Создаём два отдельных request с одним вопросом, обходя dedup —
    # симулируем "старые" дубли. Делаем это прямо через runtime.
    # Сначала один candidate.
    cand_a = _make_candidate(
        service, project_id,
        source_type="validation",
        source_id="task-AAA:role-1:question:h1",
        question="Какой стек разработки?",
    )
    service.register_candidates(workspace, (cand_a,))

    # Дальше — сразу создаём второй request на низком уровне runtime,
    # чтобы воспроизвести "уже накопленный дубль".
    from pov_generator.domain.clarifications import ClarificationRequest
    duplicate = ClarificationRequest(
        request_id="dup-fixed-id-1",
        project_id=project_id,
        status="open",
        priority="high",
        title="Какой стек разработки?",
        question="Какой стек разработки?",
        description="дубль",
        reason="legacy",
        impact="legacy",
        answer_mode="single",
        options=(
            ClarificationOption(option_id="opt_a", label="A", description="", effect_preview="", confidence=0.5),
        ),
        recommended_option_id=None,
        min_participation_mode="balanced",
        default_assumption=None,
        affected_task_ids=("task-BBB",),
        related_artifact_ids=(),
        blocking_scope="task",
        decision_owner_role="business",
        source_type="validation",
        source_id="task-BBB:role-2:question:h7",  # другой source_id
        created_from_candidate_ids=(),
        selected_option_ids=(),
        free_text=None,
        resolution_summary=None,
        auto_resolved=False,
        created_at="",
        updated_at="",
    )
    runtime.create_clarification_request(workspace, duplicate)

    open_before = [r for r in runtime.list_clarification_requests(workspace) if r.status == "open"]
    assert len(open_before) == 2

    # Отвечаем на первый.
    first_id = [r for r in open_before if r.request_id != "dup-fixed-id-1"][0].request_id
    service.answer_clarification(
        workspace,
        request_id=first_id,
        selected_option_ids=("opt_a",),
    )

    # Второй должен стать deferred с reason resolved_via:{first_id}.
    after = {r.request_id: r for r in runtime.list_clarification_requests(workspace)}
    assert after[first_id].status == "answered"
    assert after["dup-fixed-id-1"].status == "deferred"

    # Проверяем audit-event про авто-deferral.
    events = runtime.list_clarification_events(workspace, "dup-fixed-id-1")
    deferred_events = [e for e in events if e["event_type"] == "deferred"]
    assert deferred_events, "должен быть audit event для авто-deferred дубля"
    payload = deferred_events[-1]["payload"]
    assert payload.get("auto") is True
    assert payload.get("via_request_id") == first_id


def test_answer_does_not_touch_different_questions(tmp_path: Path) -> None:
    """Pripаgation НЕ должен трогать requests с другими вопросами."""
    workspace, project_id, runtime, service = _bootstrap(tmp_path)

    cand_q1 = _make_candidate(
        service, project_id,
        source_type="validation",
        source_id="task-AAA:role-1:question:h1",
        question="Какой стек разработки?",
    )
    cand_q2 = _make_candidate(
        service, project_id,
        source_type="validation",
        source_id="task-CCC:role-2:question:h2",
        question="Какой объём миграции данных?",
    )
    d1 = service.register_candidates(workspace, (cand_q1,))
    d2 = service.register_candidates(workspace, (cand_q2,))
    req1_id = d1[0].request_id
    req2_id = d2[0].request_id
    assert req1_id and req2_id and req1_id != req2_id

    service.answer_clarification(
        workspace,
        request_id=req1_id,
        selected_option_ids=("opt_a",),
    )

    req2_after = runtime.get_clarification_request(workspace, req2_id)
    assert req2_after.status == "open"  # не тронут


def test_answer_does_not_touch_other_project(tmp_path: Path) -> None:
    """Propagation работает только внутри одного project_id."""
    workspace, project_id, runtime, service = _bootstrap(tmp_path)

    # Второй проект в отдельном workspace.
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    project_service = ProjectService(runtime)
    workspace_other = tmp_path / "other"
    bootstrap_other = project_service.init_project(
        workspace=workspace_other,
        name="other-project",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="t",
        domain_packs=(),
    )
    other_id = bootstrap_other.manifest.project_id

    same_question = "Какой объём миграции?"
    cand_p1 = _make_candidate(
        service, project_id,
        source_type="validation",
        source_id="task-AAA:role-1:question:h1",
        question=same_question,
    )
    cand_p2 = _make_candidate(
        service, other_id,
        source_type="validation",
        source_id="task-XXX:role-1:question:h1",
        question=same_question,
    )
    d1 = service.register_candidates(workspace, (cand_p1,))
    d2 = service.register_candidates(workspace_other, (cand_p2,))
    req_p1 = d1[0].request_id
    req_p2 = d2[0].request_id

    service.answer_clarification(
        workspace,
        request_id=req_p1,
        selected_option_ids=("opt_a",),
    )

    other_request = runtime.get_clarification_request(workspace_other, req_p2)
    assert other_request.status == "open"  # не тронут — другой проект
