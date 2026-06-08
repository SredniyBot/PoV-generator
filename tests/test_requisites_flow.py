"""Реквизиты v2 (Ф3): данные реально втекают в работу + безопасность.

Проверяем сквозной канал «предоставление → слой A → контекст задачи»:
1. предоставленное значение становится пользовательским фактом (source="user")
   и попадает в собранный контекст задачи;
2. секрет не утекает: credential (и режим reference) НЕ создают value-положение,
   значение не персистится; смена value → reference снимает прежнее положение;
3. предоставление — событие (эндпоинт отрабатывает, реквизит помечен provided).
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from pov_generator.application.context_service import ContextService
from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.common.serialization import utc_now_iso
from pov_generator.domain.artifacts import ArtifactRecord
from pov_generator.domain.positions import REQUISITE_POSITION_PREFIX, Position
from pov_generator.domain.project_knowledge import UpsertPositionPatch
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime
from pov_generator.interfaces.api import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE_REF = "common.requirements_specification@1.0.0"

_COMPONENT_MODEL = {
    "components": [
        {
            "id": "ingest",
            "name": "Приём заявок",
            "requisites": [
                {
                    "id": "crm_creds",
                    "kind": "credential",
                    "title": "Доступ к API CRM",
                    "blocking": True,
                },
                {
                    "id": "fmt",
                    "kind": "interface_format",
                    "title": "Точный формат формы",
                    "blocking": False,
                },
            ],
        }
    ],
    "coverage": {"actors": [], "external_systems": []},
}


def _store_component_model(runtime: SqliteRuntime, workspace: Path) -> None:
    artifact = ArtifactRecord(
        artifact_id="c1",
        project_id="p",
        artifact_role="component_model",
        title="component_model",
        description=None,
        artifact_format="json",
        artifact_kind="primary",
        created_by_task_id=None,
        storage_path="artifacts/c1.json",
        created_at="2026-06-07T00:00:00+00:00",
    )
    runtime.store_artifact(
        workspace, artifact=artifact, content=json.dumps(_COMPONENT_MODEL, ensure_ascii=False)
    )


# --- 1. Канал контекста: пользовательский факт реквизита доходит до задачи ----


def test_provided_value_reaches_task_context(tmp_path: Path) -> None:
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    runtime = SqliteRuntime()
    snapshot, report = registry_service.validate()
    assert report.is_valid
    workspace = tmp_path / "case"
    ProjectService(runtime).init_project(
        workspace=workspace,
        name="flow",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="Нужна CRM-интеграция.",
        domain_packs=(),
    )
    PlanningService(runtime).expand_graph(workspace, snapshot)

    secret_marker = "ФОРМАТ-ФОРМЫ-XYZ-7788"
    runtime.apply_knowledge_patch(
        workspace,
        UpsertPositionPatch(
            Position(
                identifier=f"{REQUISITE_POSITION_PREFIX}architecture:ingest:fmt",
                type="fact",
                statement=f"Данные, предоставленные пользователем по реквизиту «Формат»:\n\n{secret_marker}",
                visibility="architectural",
                scope="global",
                source="user",
                taken_by="requisite",
                taken_at=utc_now_iso(),
                tags=("requisite", "user_input"),
            )
        ),
        actor="requisite",
        reason="test provide value",
    )

    # Лист, не требующий upstream-артефактов — иначе build_for_task упадёт на
    # отсутствующем обязательном входе.
    leaf = next(
        t
        for t in runtime.list_tasks(workspace)
        if t.template_type == "leaf"
        and not snapshot.resolve_template(t.template_ref).inputs.required_artifact_roles
    )
    result = ContextService(runtime).build_for_task(
        workspace, snapshot, leaf.task_id, model_context_window=200_000
    )
    blob = "\n".join(item.content for item in result.manifest.items)
    assert secret_marker in blob, "предоставленное значение не дошло до контекста задачи"


# --- 2. Безопасность + втекание через команду (полная проводка, API) ---------


def _api(tmp_path: Path) -> TestClient:
    return TestClient(create_app(repo_root=REPO_ROOT, runtime_root=tmp_path / "runtime"))


def _create_project(client: TestClient) -> str:
    response = client.post(
        "/api/projects",
        json={
            "name": "flow api",
            "objective_ref": OBJECTIVE_REF,
            "request_text": "CRM интеграция.",
            "domain_pack_refs": [],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["project_id"]


def test_value_provision_creates_user_position(tmp_path: Path) -> None:
    client = _api(tmp_path)
    pid = _create_project(client)
    qs = client.app.state.query_service
    runtime = client.app.state.command_service._project_service.runtime
    workspace = qs._load_context(pid).workspace
    _store_component_model(runtime, workspace)

    response = client.post(
        f"/api/projects/{pid}/requisites/provide",
        json={"key": "architecture:ingest:fmt", "mode": "value", "value": "JSON {name,email}"},
    )
    assert response.status_code == 200, response.text

    position = runtime.load_knowledge(workspace).positions.get(
        f"{REQUISITE_POSITION_PREFIX}architecture:ingest:fmt"
    )
    assert position is not None
    assert position.status == "active"
    assert position.source == "user"
    assert "JSON {name,email}" in position.statement
    # Реквизит помечен предоставленным.
    items = response.json()["items"]
    fmt = next(i for i in items if i["key"] == "architecture:ingest:fmt")
    assert fmt["status"] == "provided"


def test_credential_value_is_not_persisted(tmp_path: Path) -> None:
    """Инвариант безопасности: секрет (credential) не утекает в контекст.

    Даже если клиент прислал mode=value со значением, команда принуждает
    reference: value-положение не создаётся, значение не сохраняется в провижене.
    """
    client = _api(tmp_path)
    pid = _create_project(client)
    qs = client.app.state.query_service
    runtime = client.app.state.command_service._project_service.runtime
    workspace = qs._load_context(pid).workspace
    _store_component_model(runtime, workspace)

    leaked = "СЕКРЕТНЫЙ-ТОКЕН-CRM-9999"
    response = client.post(
        f"/api/projects/{pid}/requisites/provide",
        json={"key": "architecture:ingest:crm_creds", "mode": "value", "value": leaked},
    )
    assert response.status_code == 200, response.text

    # Положение-значение не создано.
    position = runtime.load_knowledge(workspace).positions.get(
        f"{REQUISITE_POSITION_PREFIX}architecture:ingest:crm_creds"
    )
    assert position is None, "credential не должен создавать value-положение в слое A"
    # Секрет не сохранён в записи предоставления.
    provisions = runtime.list_requisite_provisions(workspace)
    record = provisions["architecture:ingest:crm_creds"]
    assert record["mode"] == "reference"
    assert record["value"] == ""
    assert leaked not in json.dumps(provisions, ensure_ascii=False)
    # Но реквизит помечен предоставленным (доступ выдан вне системы).
    fmt = next(i for i in response.json()["items"] if i["key"] == "architecture:ingest:crm_creds")
    assert fmt["status"] == "provided"


def test_switch_value_to_reference_clears_position(tmp_path: Path) -> None:
    client = _api(tmp_path)
    pid = _create_project(client)
    qs = client.app.state.query_service
    runtime = client.app.state.command_service._project_service.runtime
    workspace = qs._load_context(pid).workspace
    _store_component_model(runtime, workspace)
    pos_id = f"{REQUISITE_POSITION_PREFIX}architecture:ingest:fmt"

    client.post(
        f"/api/projects/{pid}/requisites/provide",
        json={"key": "architecture:ingest:fmt", "mode": "value", "value": "v1"},
    )
    assert runtime.load_knowledge(workspace).positions[pos_id].status == "active"

    # Переключение на reference снимает прежнее value-положение.
    client.post(
        f"/api/projects/{pid}/requisites/provide",
        json={"key": "architecture:ingest:fmt", "mode": "reference", "note": "выдано отдельно"},
    )
    assert pos_id not in [p.identifier for p in runtime.load_knowledge(workspace).active()]
