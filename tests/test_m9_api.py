from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from pov_generator.application.checkpoint_service import CheckpointService
from pov_generator.application.context_service import ContextService
from pov_generator.application.execution_service import ExecutionService
from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.application.validation_service import ValidationService
from pov_generator.application.workflow_service import WorkflowService
from pov_generator.domain.checkpoints import CheckpointAnswer
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime
from pov_generator.interfaces.api import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE_REF = "common.requirements_specification@1.0.0"
SIGNOFF_GATE_TITLE = "Согласование ТЗ с заказчиком"


def build_services():
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)
    context_service = ContextService(runtime)
    execution_service = ExecutionService(runtime, context_service)
    validation_service = ValidationService(runtime, CheckpointService(runtime))
    workflow_service = WorkflowService(runtime, planning_service, execution_service, validation_service)
    return registry_service, runtime, project_service, planning_service, workflow_service


def _approve_signoff_via_checkpoint(
    runtime: SqliteRuntime, workspace: Path, project_id: str, *, choice: str = "approved"
) -> None:
    """v3.1: signoff hits Decision/CheckpointSession pipeline.

    Находит pending checkpoint-сессию, в которой есть decision со signoff-title
    ("Согласовать результат gate 'Согласование ТЗ с заказчиком'?"), и
    финализирует её через CheckpointService.submit_answers с выбором
    ``approved`` (или указанной альтернативой).
    """
    sessions = runtime.list_checkpoint_sessions(workspace, project_id=project_id, status="pending")
    checkpoint_service = CheckpointService(runtime)
    for session in sessions:
        signoff_decisions = []
        for decision_id in session.decision_ids:
            decision = runtime.get_decision(workspace, decision_id)
            if SIGNOFF_GATE_TITLE in decision.title:
                signoff_decisions.append(decision)
        if not signoff_decisions:
            continue
        answers = tuple(
            CheckpointAnswer(
                decision_id=decision.decision_id,
                kind="select_alternative",
                selected_option_id=choice,
            )
            for decision in signoff_decisions
        )
        checkpoint_service.submit_answers(workspace, session_id=session.session_id, answers=answers)
        return
    raise AssertionError(
        f"Не найдена pending CheckpointSession с signoff-decision (title contains "
        f"{SIGNOFF_GATE_TITLE!r}) для проекта {project_id!r}"
    )


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

    manifest = runtime.load_manifest(workspace)
    _approve_signoff_via_checkpoint(runtime, workspace, manifest.project_id)

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
    # LLM-ревью удалён (вариант B): /review больше не производит review_report,
    # проекция возвращает "missing". Валидатор ТЗ теперь — человеческий sign-off.
    assert review["status"] == "missing"

    state = client.get(f"/api/projects/{project_id}/state").json()
    assert state["root_task_id"] is not None
    assert state["active_gaps"] == []

    debug = client.get(f"/api/projects/{project_id}/debug").json()
    assert len(debug["tasks"]) >= 16
    assert len(debug["execution_runs"]) >= 11
    assert len(debug["context_manifests"]) >= 11


def test_list_projects_isolates_broken_project(tmp_path: Path, monkeypatch) -> None:
    """Один проект, который не удаётся построить целиком, не должен ронять
    весь список (регрессия 409 на GET /api/projects).

    Сценарий из практики: проект создан старой версией флоу, его граф задач
    ссылается на шаблон, удалённый из реестра. Раньше исключение из одного
    проекта поднималось наружу и весь список отвечал 409 → пустой экран.
    Теперь битый проект показывается в деградированном виде, остальные —
    нормально.
    """
    from pov_generator.application.workspace_query_service import WorkspaceQueryService
    from pov_generator.common.errors import ConflictError

    runtime_root = tmp_path / "runtime"
    good_id = init_project(
        runtime_root / "good",
        "Подготовить ТЗ для сервиса нормализации входящих заявок.",
    )
    bad_id = init_project(
        runtime_root / "bad",
        "Подготовить ТЗ для сервиса маршрутизации обращений.",
    )

    # Падение моделируем на _build_task_graph — это тот же seam, где в
    # реальном кейсе всплывает "Task template not found" (резолв шаблонов
    # при построении графа). list_projects строит проекции через приватные
    # билдеры, а не через публичные project_* методы.
    original_build_task_graph = WorkspaceQueryService._build_task_graph

    def flaky_build_task_graph(self, context, *args, **kwargs):
        if context.manifest.project_id == bad_id:
            raise ConflictError(
                "Task template not found: common.review_requirements_spec@1.0.0"
            )
        return original_build_task_graph(self, context, *args, **kwargs)

    monkeypatch.setattr(WorkspaceQueryService, "_build_task_graph", flaky_build_task_graph)

    app = create_app(repo_root=REPO_ROOT, runtime_root=runtime_root, websocket_poll_interval=0.05)
    client = TestClient(app)

    resp = client.get("/api/projects")
    assert resp.status_code == 200
    payload = resp.json()

    by_id = {item["project_id"]: item for item in payload}
    assert set(by_id) == {good_id, bad_id}

    bad = by_id[bad_id]
    assert bad["status_label"] == "Ошибка загрузки"
    assert bad["has_blockers"] is True
    assert bad["current_step_title"] is None

    good = by_id[good_id]
    assert good["status_label"] != "Ошибка загрузки"


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


def test_api_delete_project_removes_workspace(tmp_path: Path) -> None:
    """DELETE /api/projects/{id} удаляет workspace целиком: проект исчезает
    из списка, а его папка с диска удаляется."""
    runtime_root = tmp_path / "runtime"
    app = create_app(repo_root=REPO_ROOT, runtime_root=runtime_root, websocket_poll_interval=0.02)
    client = TestClient(app)

    created = client.post(
        "/api/projects",
        json={
            "name": "To Be Deleted",
            "objective_ref": OBJECTIVE_REF,
            "request_text": "Тестовый проект для удаления.",
            "domain_pack_refs": ["frontend.web_workspace@1.0.0"],
        },
    ).json()
    project_id = created["project_id"]
    assert any(p["project_id"] == project_id for p in client.get("/api/projects").json())

    deleted = client.delete(f"/api/projects/{project_id}")
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "deleted"

    # Исчез из списка и его shell больше недоступен.
    assert all(p["project_id"] != project_id for p in client.get("/api/projects").json())
    assert client.get(f"/api/projects/{project_id}/shell").status_code in (404, 409, 500)

    # Повторное удаление — 404 (резолв до side-effect'ов).
    assert client.delete(f"/api/projects/{project_id}").status_code == 404


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


ARCHITECTURE_OBJECTIVE_REF = "architecture.system_design@1.0.0"
IMPLEMENTATION_OBJECTIVE_REF = "implementation.build_plan@1.0.0"


def test_api_exposes_stage_roadmap(tmp_path: Path) -> None:
    """Проекция /stages: цепочка этапов + прогресс/ошибки активного."""
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "case-stages"
    project_id = init_project(
        workspace,
        "Нужно подготовить техническое задание для сервиса, который структурирует бизнес-запросы.",
    )

    app = create_app(repo_root=REPO_ROOT, runtime_root=runtime_root, websocket_poll_interval=0.05)
    client = TestClient(app)

    # 1. Свежий проект: ТЗ — active, дальше по цепочке — locked.
    fresh = client.get(f"/api/projects/{project_id}/stages")
    assert fresh.status_code == 200
    payload = fresh.json()
    assert payload["objective_ref"] == OBJECTIVE_REF
    assert payload["objective_complete"] is False
    states = {s["objective_ref"]: s["state"] for s in payload["stages"]}
    assert states[OBJECTIVE_REF] == "active"
    assert states[ARCHITECTURE_OBJECTIVE_REF] == "locked"
    assert states[IMPLEMENTATION_OBJECTIVE_REF] == "locked"
    active_stage = next(s for s in payload["stages"] if s["is_current"])
    assert active_stage["artifacts_required"] >= 1
    assert active_stage["artifacts_ready"] == 0
    # next_objective_refs активного ТЗ = архитектура.
    assert ARCHITECTURE_OBJECTIVE_REF in payload["next_objective_refs"]

    # 2. Форс-падение задачи → failed_count на активном этапе.
    registry_service, _runtime, _project_service, planning_service, _workflow_service = build_services()
    snapshot, report = registry_service.validate()
    assert report.is_valid
    decision = planning_service.plan(workspace, snapshot, mode="apply")
    assert decision.selected_task_id is not None
    failed_task_id = decision.selected_task_id
    planning_service.transition_task(
        workspace,
        failed_task_id,
        "fail",
        payload={"error_message": "Искусственно сломанная задача для теста stages."},
    )

    after_fail = client.get(f"/api/projects/{project_id}/stages").json()
    active_stage = next(s for s in after_fail["stages"] if s["is_current"])
    assert active_stage["failed_count"] >= 1
    failing = active_stage["failing_tasks"]
    assert any(
        t["task_id"] == failed_task_id and t["retryable"] is True and t["status"] == "failed"
        for t in failing
    )


def test_api_stage_roadmap_marks_done_and_advances(tmp_path: Path) -> None:
    """После прогона ТЗ и активации архитектуры: ТЗ — done, архитектура — active."""
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "case-stages-done"
    project_id = init_project(
        workspace,
        "Нужно подготовить техническое задание для сервиса, который структурирует бизнес-запросы.",
    )
    run_stub_workflow(workspace)

    app = create_app(repo_root=REPO_ROOT, runtime_root=runtime_root, websocket_poll_interval=0.05)
    client = TestClient(app)

    done = client.get(f"/api/projects/{project_id}/stages").json()
    active_stage = next(s for s in done["stages"] if s["is_current"])
    assert active_stage["objective_ref"] == OBJECTIVE_REF
    assert active_stage["artifacts_ready"] == active_stage["artifacts_required"]
    assert done["objective_complete"] is True
    assert ARCHITECTURE_OBJECTIVE_REF in done["next_objective_refs"]

    # Активируем архитектуру — ТЗ уходит в done, архитектура становится active.
    registry_service, _runtime, project_service, planning_service, _workflow_service = build_services()
    snapshot, report = registry_service.validate()
    assert report.is_valid
    project_service.activate_next_objective(
        workspace, ObjectRef.parse(ARCHITECTURE_OBJECTIVE_REF)
    )
    planning_service.expand_graph(workspace, snapshot)

    advanced = client.get(f"/api/projects/{project_id}/stages").json()
    states = {s["objective_ref"]: s["state"] for s in advanced["stages"]}
    assert states[OBJECTIVE_REF] == "done"
    assert states[ARCHITECTURE_OBJECTIVE_REF] == "active"
    # Из архитектуры дальше по цепочке — implementation (locked).
    assert states[IMPLEMENTATION_OBJECTIVE_REF] == "locked"
