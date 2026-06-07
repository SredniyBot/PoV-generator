"""Ф6: веер по компонентам в слое реализации + инъекция фокуса в контекст.

Проверяем:
1. веер по component_model создаёт по одному инстансу component_build_spec на
   каждый компонент (origin_ref = id компонента);
2. context_service кладёт «свой» компонент как фокус (fanout_focus) — без этого
   per-component задача не знает, за что отвечает.
"""

from __future__ import annotations

import json
from pathlib import Path

from pov_generator.application.context_service import ContextService
from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.common.serialization import utc_now_iso
from pov_generator.domain.artifacts import ArtifactMetadata, ArtifactRecord, ArtifactRelations
from pov_generator.domain.registry import ObjectRef
from pov_generator.domain.tasks import TaskRecord
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]

_COMPONENT_MODEL = {
    "components": [
        {
            "id": "ingest",
            "name": "Приём заявок",
            "type": "service",
            "layer": "application",
            "responsibility": "Принимает заявки.",
            "justification": "Точка входа.",
            "provided_interfaces": [{"name": "submitRequest"}],
            "modules": [{"id": "v", "responsibility": "валидация", "realizes": "submitRequest"}],
        },
        {
            "id": "store",
            "name": "Хранилище",
            "type": "datastore",
            "layer": "infrastructure",
            "responsibility": "Хранит заявки.",
            "justification": "Долговременное хранение.",
            "provided_interfaces": [{"name": "saveRequest"}],
            "modules": [{"id": "r", "responsibility": "доступ", "realizes": "saveRequest"}],
        },
    ],
    "coverage": {"actors": [], "external_systems": []},
}


def _setup(tmp_path: Path):
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    snapshot, report = registry_service.validate()
    assert report.is_valid
    runtime = SqliteRuntime()
    ws = tmp_path / "ws"
    ProjectService(runtime).init_project(
        workspace=ws,
        name="T",
        objective_ref=ObjectRef.parse("common.requirements_specification@1.0.0"),
        request_text="нужно приложение заявок",
        domain_packs=(),
    )
    return ws, snapshot, runtime, PlanningService(runtime)


def _store_component_model(runtime: SqliteRuntime, ws: Path) -> None:
    artifact = ArtifactRecord(
        artifact_id="cm1",
        project_id=runtime.load_manifest(ws).project_id,
        artifact_role="component_model",
        title="Модель компонентов",
        description=None,
        artifact_format="json",
        artifact_kind="primary",
        created_by_task_id=None,
        storage_path="artifacts/cm1.json",
        created_at=utc_now_iso(),
        relations=ArtifactRelations(),
        metadata=ArtifactMetadata(),
    )
    runtime.store_artifact(ws, artifact=artifact, content=json.dumps(_COMPONENT_MODEL, ensure_ascii=False))


def _make_fanout_task(runtime: SqliteRuntime, ws: Path) -> TaskRecord:
    now = utc_now_iso()
    return runtime.create_task(
        ws,
        TaskRecord(
            task_id="fo-comp",
            project_id=runtime.load_manifest(ws).project_id,
            objective_ref="implementation.build_plan@1.0.0",
            parent_task_id=None,
            template_ref="implementation.component_build_fanout@1.0.0",
            template_type="fan_out",
            title="Спеки сборки по компонентам (веер)",
            status="waiting_for_fan_out_source",
            origin_kind="base_child",
            origin_ref="impl",
            stable_key="impl:component_build_fanout",
            depth=1,
            slot_id=None,
            attempt=1,
            error_message=None,
            created_at=now,
            updated_at=now,
        ),
    )


def test_fanout_creates_instance_per_component(tmp_path: Path) -> None:
    ws, snapshot, runtime, planning = _setup(tmp_path)
    _store_component_model(runtime, ws)
    fanout = _make_fanout_task(runtime, ws)

    planning._expand_fan_outs(ws, snapshot)
    instances = [t for t in runtime.list_tasks(ws) if t.parent_task_id == fanout.task_id]
    assert {t.origin_ref for t in instances} == {"ingest", "store"}
    assert all(
        t.template_ref == "implementation.component_build_spec@1.0.0" for t in instances
    )


def test_context_injects_component_focus(tmp_path: Path) -> None:
    ws, snapshot, runtime, planning = _setup(tmp_path)
    _store_component_model(runtime, ws)
    fanout = _make_fanout_task(runtime, ws)
    planning._expand_fan_outs(ws, snapshot)

    instance = next(
        t
        for t in runtime.list_tasks(ws)
        if t.parent_task_id == fanout.task_id and t.origin_ref == "ingest"
    )
    result = ContextService(runtime).build_for_task(ws, snapshot, instance.task_id)
    focus = [it for it in result.manifest.items if it.item_type == "fanout_focus"]
    assert focus, "фокус-элемент компонента должен попасть в контекст"
    assert "ingest" in focus[0].content
    assert "Приём заявок" in focus[0].content
    # фокус — именно целевой компонент, а не соседний
    assert "store" not in json.loads(focus[0].content).get("id", "")
