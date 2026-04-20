from __future__ import annotations

from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient

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
from pov_generator.interfaces.api import create_app


REPO_ROOT = Path(__file__).resolve().parents[1]


def build_services():
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)
    context_service = ContextService(runtime)
    execution_service = ExecutionService(runtime, context_service)
    validation_service = ValidationService(runtime)
    workflow_service = WorkflowService(runtime, planning_service, execution_service, validation_service)
    return registry_service, runtime, project_service, planning_service, workflow_service


def init_project(workspace: Path, request_text: str, domain_packs: tuple[str, ...] = ()) -> str:
    registry_service, _runtime, project_service, planning_service, _workflow_service = build_services()
    snapshot, report = registry_service.validate()
    assert report.is_valid
    recipe_ref = ObjectRef.parse("common.build_requirements_spec@1.0.0")
    bootstrap_recipe = planning_service.build_recipe_bootstrap(
        snapshot,
        recipe_ref.as_string(),
        enabled_domain_pack_refs=domain_packs,
    )
    bootstrap = project_service.init_project(
        workspace=workspace,
        name="API Demo",
        recipe_ref=recipe_ref,
        request_text=request_text,
        bootstrap_recipe=bootstrap_recipe,
    )
    return bootstrap.manifest.project_id


def run_stub_workflow(workspace: Path) -> None:
    registry_service, _runtime, _project_service, _planning_service, workflow_service = build_services()
    snapshot, report = registry_service.validate()
    assert report.is_valid
    workflow_service.run_until_blocked(workspace, snapshot, provider="stub")


def test_api_exposes_separate_operator_projections(tmp_path: Path) -> None:
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
    assert shell["status_label"] == "Готово"

    journey = client.get(f"/api/projects/{project_id}/journey").json()
    assert journey["completed_steps"] == journey["total_steps"] == 5

    situation = client.get(f"/api/projects/{project_id}/situation").json()
    assert situation["status_label"] == "Готово"
    assert situation["primary_action"]["kind"] == "open_artifact"

    timeline = client.get(f"/api/projects/{project_id}/timeline").json()
    assert timeline["total_entries"] >= 6
    assert any(entry["title"] == "Подготовлен черновик ТЗ" for entry in timeline["entries"])

    artifacts = client.get(f"/api/projects/{project_id}/artifacts").json()
    assert {item["artifact_role"] for item in artifacts} == {
        "clarification_notes",
        "user_story_map",
        "alternatives_analysis",
        "requirements_spec",
        "review_report",
    }
    spec_id = next(item["artifact_id"] for item in artifacts if item["artifact_role"] == "requirements_spec")
    artifact_detail = client.get(f"/api/projects/{project_id}/artifacts/{spec_id}").json()
    assert "Техническое задание" in artifact_detail["json_content"]
    assert artifact_detail["markdown_content"] is not None

    review = client.get(f"/api/projects/{project_id}/review").json()
    assert review["status"] == "passed"

    state = client.get(f"/api/projects/{project_id}/state").json()
    assert state["goal"] is not None
    assert state["active_gaps"] == []

    debug = client.get(f"/api/projects/{project_id}/debug").json()
    assert len(debug["tasks"]) == 5
    assert len(debug["execution_runs"]) == 5
    assert len(debug["context_manifests"]) == 5


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

    with client.websocket_connect(f"/ws/projects/{project_id}?projections=situation,journey,timeline,artifacts,state,debug") as websocket:
        snapshot = websocket.receive_json()
        assert snapshot["type"] == "snapshot"
        ready.set()
        received: set[str] = set()
        deadline = time.time() + 2.0
        expected = {"journey", "timeline", "artifacts"}
        while time.time() < deadline and not expected.issubset(received):
            message = websocket.receive_json()
            if message["type"] == "projection_changed":
                received.add(message["projection"])
        assert "journey" in received
        assert "timeline" in received
        assert "artifacts" in received

    worker.join(timeout=1.0)

    situation = client.get(f"/api/projects/{project_id}/situation").json()
    assert situation["status_label"] in {"Готов к продолжению", "Выполняется"}
