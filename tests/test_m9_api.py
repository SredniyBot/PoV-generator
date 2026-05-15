from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from pov_generator.application.clarification_service import ClarificationDraft, ClarificationService
from pov_generator.application.context_service import ContextService
from pov_generator.application.execution_service import ExecutionService
from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.application.validation_service import ValidationService
from pov_generator.application.workflow_service import WorkflowService
from pov_generator.common.serialization import utc_now_iso
from pov_generator.domain.clarifications import ClarificationCandidate, ClarificationOption
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime
from pov_generator.interfaces.api import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE_REF = "common.requirements_specification@1.0.0"


class FakeClarificationDraftProvider:
    def build_draft(
        self,
        *,
        candidate: ClarificationCandidate,
        context: dict[str, object],
        fallback: ClarificationDraft,
    ) -> ClarificationDraft:
        assert "business_request" in context
        return ClarificationDraft(
            description=(
                "Система анализирует проектный запрос и обнаружила решение, которое нельзя надежно вывести из текущего текста. "
                "В запросе есть признаки нескольких допустимых трактовок, и каждая из них по-разному влияет на состав будущего ТЗ. "
                "Ответ пользователя нужен, чтобы не подменить бизнес-потребность техническим предположением. "
                "После ответа система сможет зафиксировать решение и продолжить декомпозицию требований."
            ),
            answer_mode="multiple",
            options=(
                ClarificationOption(
                    option_id="strict_poc",
                    label="Сфокусироваться только на PoC",
                    description="Описать минимальный объем работ для проверки гипотезы.",
                    effect_preview="ТЗ будет ограничивать текущий этап проверкой ценности и реализуемости.",
                    confidence=0.68,
                ),
                ClarificationOption(
                    option_id="future_scale",
                    label="Сразу учитывать будущее масштабирование",
                    description="Добавить требования, которые важны после успешного PoC.",
                    effect_preview="ТЗ будет содержать отдельные требования к развитию решения.",
                    confidence=0.42,
                ),
            ),
            recommended_option_id="strict_poc",
            visibility="architectural",
        )


def build_services():
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)
    context_service = ContextService(runtime)
    execution_service = ExecutionService(runtime, context_service)
    validation_service = ValidationService(runtime, ClarificationService(runtime, provider="stub"))
    workflow_service = WorkflowService(runtime, planning_service, execution_service, validation_service)
    return registry_service, runtime, project_service, planning_service, workflow_service


def init_project(workspace: Path, request_text: str, domain_packs: tuple[str, ...] = ()) -> str:
    registry_service, _runtime, project_service, planning_service, _workflow_service = build_services()
    snapshot, report = registry_service.validate()
    assert report.is_valid
    packs = tuple(snapshot.resolve_domain_pack(pack_ref) for pack_ref in domain_packs)
    bootstrap = project_service.init_project(
        workspace=workspace,
        name="API Demo",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text=request_text,
        domain_packs=packs,
    )
    planning_service.expand_graph(workspace, snapshot)
    return bootstrap.manifest.project_id


def run_stub_workflow(workspace: Path) -> None:
    registry_service, runtime, _project_service, _planning_service, workflow_service = build_services()
    snapshot, report = registry_service.validate()
    assert report.is_valid
    result = workflow_service.run_until_blocked(workspace, snapshot, provider="stub", max_steps=50)
    # `client.requirements_signoff` (human_approval gate) blocks the
    # objective until the operator confirms approval.
    assert result.stopped_reason == "planner_blocked"

    clarification_service = ClarificationService(runtime, provider="stub")
    signoff = next(
        req
        for req in runtime.list_clarification_requests(workspace)
        if req.source_type == "quality_gate"
        and req.source_id == "client.requirements_signoff@1.0.0"
        and req.status == "open"
    )
    clarification_service.answer_clarification(
        workspace, request_id=signoff.request_id, selected_option_ids=("approved",)
    )

    result = workflow_service.run_until_blocked(workspace, snapshot, provider="stub", max_steps=5)
    assert result.stopped_reason == "objective_completed"


def test_api_exposes_operator_projections_for_task_graph(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "case1"
    project_id = init_project(
        workspace,
        "Нужно подготовить техническое задание для сервиса, который структурирует бизнес-запросы.",
    )
    run_stub_workflow(workspace)

    app = create_app(repo_root=REPO_ROOT, runtime_root=runtime_root, websocket_poll_interval=0.05)
    client = TestClient(app)

    projects = client.get("/api/projects")
    assert projects.status_code == 200
    payload = projects.json()
    assert len(payload) == 1
    assert payload[0]["project_id"] == project_id

    shell = client.get(f"/api/projects/{project_id}/shell").json()
    assert shell["name"] == "API Demo"
    assert shell["objective_ref"] == OBJECTIVE_REF
    assert shell["status_label"] == "Готово"

    task_graph = client.get(f"/api/projects/{project_id}/task-graph").json()
    assert task_graph["completed_leaf_tasks"] == task_graph["total_leaf_tasks"]
    assert task_graph["nodes"][0]["template_type"] == "composite"
    assert task_graph["nodes"][0]["children"]

    situation = client.get(f"/api/projects/{project_id}/situation").json()
    assert situation["status_label"] == "Готово"
    assert situation["primary_action"]["kind"] == "open_artifact"

    timeline = client.get(f"/api/projects/{project_id}/timeline").json()
    assert timeline["total_entries"] >= 12
    assert any(entry["detail_view"] == "task_graph" for entry in timeline["entries"])

    artifacts = client.get(f"/api/projects/{project_id}/artifacts").json()
    assert "requirements_spec" in {item["artifact_role"] for item in artifacts}
    spec_id = next(item["artifact_id"] for item in artifacts if item["artifact_role"] == "requirements_spec")
    artifact_detail = client.get(f"/api/projects/{project_id}/artifacts/{spec_id}").json()
    assert "Техническое задание" in artifact_detail["json_content"]
    assert artifact_detail["markdown_content"] is not None

    review = client.get(f"/api/projects/{project_id}/review").json()
    assert review["status"] == "passed"

    state = client.get(f"/api/projects/{project_id}/state").json()
    assert state["root_task_id"] is not None
    assert state["active_gaps"] == []

    debug = client.get(f"/api/projects/{project_id}/debug").json()
    assert len(debug["tasks"]) >= 16
    assert len(debug["execution_runs"]) >= 11
    assert len(debug["context_manifests"]) >= 11


def test_api_websocket_reports_projection_changes_after_command(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "case2"
    project_id = init_project(
        workspace,
        "Нужно подготовить техническое задание по новому сервису.",
    )

    app = create_app(repo_root=REPO_ROOT, runtime_root=runtime_root, websocket_poll_interval=0.02)
    client = TestClient(app)
    ready = threading.Event()

    def trigger_command() -> None:
        ready.wait(timeout=1.0)
        response = client.post(
            f"/api/projects/{project_id}/commands/run-next",
            json={"provider": "stub"},
        )
        assert response.status_code == 200

    worker = threading.Thread(target=trigger_command, daemon=True)
    worker.start()

    with client.websocket_connect(
        f"/ws/projects/{project_id}?projections=situation,task_graph,timeline,artifacts,state,debug"
    ) as websocket:
        snapshot = websocket.receive_json()
        assert snapshot["type"] == "snapshot"
        ready.set()
        received: set[str] = set()
        deadline = time.time() + 2.0
        expected = {"task_graph", "timeline", "artifacts"}
        while time.time() < deadline and not expected.issubset(received):
            message = websocket.receive_json()
            if message["type"] == "projection_changed":
                received.add(message["projection"])
        assert expected.issubset(received)

    worker.join(timeout=1.0)

    situation = client.get(f"/api/projects/{project_id}/situation").json()
    assert situation["status_label"] in {"Готов к продолжению", "Выполняется"}


def test_api_can_list_registry_entries_and_create_project(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    app = create_app(repo_root=REPO_ROOT, runtime_root=runtime_root, websocket_poll_interval=0.02)
    client = TestClient(app)

    objectives = client.get("/api/registry/objectives")
    assert objectives.status_code == 200
    assert any(item["objective_ref"] == OBJECTIVE_REF for item in objectives.json())

    packs = client.get("/api/registry/domain-packs")
    assert packs.status_code == 200
    assert any(item["pack_ref"] == "frontend.web_workspace@1.0.0" for item in packs.json())

    create_response = client.post(
        "/api/projects",
        json={
            "name": "UI Created Demo",
            "objective_ref": OBJECTIVE_REF,
            "request_text": "Нужно подготовить ТЗ для сервиса с пользовательским кабинетом.",
            "domain_pack_refs": ["frontend.web_workspace@1.0.0"],
        },
    )
    assert create_response.status_code == 200
    created = create_response.json()
    assert created["project_id"]
    assert created["domain_pack_refs"] == ["frontend.web_workspace@1.0.0"]

    shell = client.get(f"/api/projects/{created['project_id']}/shell")
    assert shell.status_code == 200
    shell_payload = shell.json()
    assert shell_payload["name"] == "UI Created Demo"
    assert shell_payload["active_domain_packs"] == ["frontend.web_workspace@1.0.0"]


def test_api_retry_task_reexecutes_failed_task(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "case-retry"
    project_id = init_project(
        workspace,
        "Нужно подготовить техническое задание для сервиса, который структурирует бизнес-запросы.",
    )
    registry_service, _runtime, _project_service, planning_service, _workflow_service = build_services()
    snapshot, report = registry_service.validate()
    assert report.is_valid

    decision = planning_service.plan(workspace, snapshot, mode="apply")
    assert decision.selected_task_id is not None
    task_id = decision.selected_task_id
    planning_service.transition_task(
        workspace,
        task_id,
        "fail",
        payload={"error_message": "Искусственно сломанная задача для теста retry."},
    )

    app = create_app(repo_root=REPO_ROOT, runtime_root=runtime_root, websocket_poll_interval=0.02)
    client = TestClient(app)

    before = client.get(f"/api/projects/{project_id}/task-graph")
    assert before.status_code == 200
    failed_node = find_task_node(before.json()["nodes"], task_id)
    assert failed_node is not None
    assert failed_node["status"] == "failed"
    assert failed_node["retryable"] is True

    retry = client.post(
        f"/api/projects/{project_id}/commands/retry-task",
        json={"task_id": task_id, "provider": "stub"},
    )
    assert retry.status_code == 200
    assert retry.json()["status"] == "accepted"

    after = client.get(f"/api/projects/{project_id}/task-graph")
    assert after.status_code == 200
    completed_node = find_task_node(after.json()["nodes"], task_id)
    assert completed_node is not None
    assert completed_node["status"] == "completed"
    assert completed_node["retryable"] is False

    debug = client.get(f"/api/projects/{project_id}/debug")
    assert debug.status_code == 200
    task = next(item for item in debug.json()["tasks"] if item["task_id"] == task_id)
    assert task["status"] == "completed"
    assert task["attempt"] == 2


def find_task_node(nodes: list[dict[str, object]], task_id: str) -> dict[str, object] | None:
    for node in nodes:
        if node["task_id"] == task_id:
            return node
        nested = find_task_node(node.get("children", []), task_id)  # type: ignore[arg-type]
        if nested is not None:
            return nested
    return None


def test_api_create_project_can_auto_select_domain_packs(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    app = create_app(repo_root=REPO_ROOT, runtime_root=runtime_root, websocket_poll_interval=0.02)
    client = TestClient(app)

    create_response = client.post(
        "/api/projects",
        json={
            "name": "Auto Selection",
            "objective_ref": OBJECTIVE_REF,
            "request_text": (
                "Нужен PoV по предиктивной аналитике оттока на ML. "
                "Источники: 1С и корпоративный портал. "
                "Нужны API-обновления, on-prem, персональные данные, BI и веб-интерфейс."
            ),
            "selection_provider": "stub",
        },
    )
    assert create_response.status_code == 200
    created = create_response.json()
    assert created["domain_pack_refs"] == [
        "frontend.web_workspace@1.0.0",
        "integration.enterprise_integration@1.0.0",
        "ml.predictive_analytics@1.0.0",
        "security.enterprise_compliance@1.0.0",
    ]

    state = client.get(f"/api/projects/{created['project_id']}/state")
    assert state.status_code == 200
    state_payload = state.json()
    assert any(
        str(item.get("identifier", "")) == "domain_pack_selection"
        and "подбора доменных пакетов" in str(item.get("statement", "")).lower()
        for item in state_payload["known_facts"]
    )


def test_api_exposes_and_answers_clarification_requests(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "case-clarification"
    project_id = init_project(
        workspace,
        "Нужно подготовить техническое задание для сервиса, который помогает HR оценивать риски.",
    )
    runtime = SqliteRuntime()
    state = runtime.load_process_state(workspace)
    assert state.root_task_id is not None
    candidate = ClarificationCandidate(
        candidate_id=str(uuid.uuid4()),
        project_id=project_id,
        source_type="validation",
        source_id="test:business-priority",
        need="Уточнить бизнес-приоритет, который влияет на дальнейшую детализацию ТЗ.",
        question="Что важнее зафиксировать в PoC: точность прогноза или скорость проверки гипотезы?",
        description=(
            "Система готовит ТЗ для сервиса, который помогает HR оценивать риски. "
            "На текущем этапе нужно определить, какой акцент важнее для PoC: быстро проверить гипотезу или строже описать качество прогноза. "
            "Это решение влияет на критерии приемки, состав требований к данным и глубину аналитической части."
        ),
        rationale="Без этого выбора система не может надежно расставить акценты в требованиях и критериях приемки.",
        impact="Ответ попадет в решения проекта и будет учитываться следующими задачами.",
        severity="high",
        confidence_without_user=0.2,
        visibility="architectural",
        default_assumption=None,
        recommended_answer="speed",
        answer_mode="single",
        options=(
            ClarificationOption(
                option_id="speed",
                label="Скорость проверки",
                description="Сфокусироваться на быстром PoC и минимальном достаточном составе требований.",
                confidence=0.64,
            ),
            ClarificationOption(
                option_id="accuracy",
                label="Точность прогноза",
                description="Сфокусироваться на качестве модели, данных и строгих метриках.",
                confidence=0.36,
            ),
        ),
        affected_task_ids=(state.root_task_id,),
        blocking_scope="objective",
        created_at=utc_now_iso(),
    )
    decisions = ClarificationService(runtime).register_candidates(workspace, (candidate,))
    assert decisions[0].action == "ask"
    assert decisions[0].request_id is not None

    app = create_app(repo_root=REPO_ROOT, runtime_root=runtime_root, websocket_poll_interval=0.02)
    client = TestClient(app)

    clarifications = client.get(f"/api/projects/{project_id}/clarifications")
    assert clarifications.status_code == 200
    clarifications_payload = clarifications.json()
    assert clarifications_payload["open_count"] == 1
    assert clarifications_payload["blocking_count"] == 1
    request_id = clarifications_payload["items"][0]["clarification_id"]
    assert clarifications_payload["items"][0]["recommended_option_id"] == "speed"
    assert clarifications_payload["items"][0]["description"].startswith("Система готовит ТЗ")
    assert clarifications_payload["items"][0]["visibility"] == "architectural"
    assert clarifications_payload["items"][0]["options"][0]["confidence"] == 0.64

    task_graph = client.get(f"/api/projects/{project_id}/task-graph")
    assert task_graph.status_code == 200
    assert task_graph.json()["nodes"][0]["blocking_clarification_count"] == 1

    answer = client.post(
        f"/api/projects/{project_id}/commands/answer-clarification",
        json={"clarification_id": request_id, "selected_option_ids": ["speed"]},
    )
    assert answer.status_code == 200
    assert answer.json()["status"] == "accepted"

    updated = client.get(f"/api/projects/{project_id}/clarifications").json()
    assert updated["open_count"] == 0
    assert updated["answered_count"] == 1
    assert updated["items"][0]["status"] == "answered"
    assert updated["items"][0]["resolution_summary"] == "Скорость проверки"

    refreshed_task_graph = client.get(f"/api/projects/{project_id}/task-graph").json()
    assert refreshed_task_graph["nodes"][0]["blocking_clarification_count"] == 0

    state_payload = client.get(f"/api/projects/{project_id}/state").json()
    assert any("Скорость проверки" in item["statement"] for item in state_payload["decisions"])


def test_clarification_candidates_created_from_questions_have_answer_options(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "case-clarification-options"
    project_id = init_project(
        workspace,
        "Нужно подготовить ТЗ для аналитического сервиса, но часть бизнес-вводных пока не раскрыта.",
    )
    runtime = SqliteRuntime()
    service = ClarificationService(runtime, provider="stub")
    state = runtime.load_process_state(workspace)
    assert state.root_task_id is not None

    candidate = service.candidate_from_question(
        project_id=project_id,
        source_type="validation",
        source_id="test:missing-context",
        question="Какой бизнес-контекст нужно учесть перед продолжением?",
        affected_task_ids=(state.root_task_id,),
        related_artifact_ids=(),
    )
    decisions = service.register_candidates(workspace, (candidate,))
    assert decisions[0].request_id is not None

    request = runtime.get_clarification_request(workspace, decisions[0].request_id)
    assert request.answer_mode == "single"
    assert len(request.description.split(". ")) >= 3
    # candidate_from_question default role = business → visibility = principal.
    assert request.visibility == "principal"
    assert len(request.options) >= 2
    assert {option.option_id for option in request.options} >= {
        "include_in_current_project",
        "use_working_assumption",
    }
    assert all(option.confidence is not None for option in request.options)

    raw_candidate = ClarificationCandidate(
        candidate_id=str(uuid.uuid4()),
        project_id=project_id,
        source_type="validation",
        source_id="test:raw-free-text-context",
        need="Уточнить недостающий контекст проекта.",
        question="Какой контекст проекта сейчас критично не потерять?",
        description=(
            "Источник вопроса обнаружил недостающий контекст в проекте. "
            "Без ответа система не сможет надежно понять, какие вводные критичны для дальнейшего ТЗ. "
            "Пользователь должен выбрать, как учитывать этот контекст, либо пояснить ответ своими словами."
        ),
        rationale="Источник вопроса не предоставил варианты ответа, но пользовательский вопрос не должен быть пустым полем.",
        impact="Ответ будет учтен при дальнейшей детализации требований.",
        severity="high",
        confidence_without_user=0.1,
        visibility="architectural",
        default_assumption=None,
        recommended_answer=None,
        answer_mode="free_text",
        options=(),
        affected_task_ids=(state.root_task_id,),
        related_artifact_ids=(),
        blocking_scope="task",
        created_at=utc_now_iso(),
    )
    raw_decision = service.register_candidates(workspace, (raw_candidate,))[0]
    assert raw_decision.request_id is not None

    raw_request = runtime.get_clarification_request(workspace, raw_decision.request_id)
    assert raw_request.answer_mode == "single"
    assert len(raw_request.options) >= 2
    assert all(option.option_id != "synthetic:other" and "друг" not in option.label.lower() for option in raw_request.options)


def test_clarification_request_details_are_generated_by_provider(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "case-clarification-provider"
    project_id = init_project(
        workspace,
        "Нужно подготовить ТЗ для пилота, но пока не ясно, должен ли он учитывать будущий промышленный контур.",
    )
    runtime = SqliteRuntime()
    service = ClarificationService(runtime, draft_provider=FakeClarificationDraftProvider())
    state = runtime.load_process_state(workspace)
    assert state.root_task_id is not None

    candidate = service.candidate_from_question(
        project_id=project_id,
        source_type="validation",
        source_id="test:provider-draft",
        question="Какую рамку этапа нужно считать целевой для текущего ТЗ?",
        affected_task_ids=(state.root_task_id,),
        related_artifact_ids=(),
    )
    decision = service.register_candidates(workspace, (candidate,))[0]
    assert decision.request_id is not None

    request = runtime.get_clarification_request(workspace, decision.request_id)
    assert request.description.startswith("Система анализирует проектный запрос")
    assert request.answer_mode == "multiple"
    assert request.recommended_option_id == "strict_poc"
    # FakeClarificationDraftProvider returns visibility=architectural now.
    assert request.visibility == "architectural"
    assert [option.option_id for option in request.options] == ["strict_poc", "future_scale"]
    assert request.options[0].confidence == 0.68
