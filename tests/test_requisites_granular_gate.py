"""Реквизиты v2 (Ф4): честный гранулярный гейтинг.

Непредоставленный блокирующий реквизит держит в admission ТОЛЬКО свою
задачу-потребителя (компонент), а не соседние компоненты и не весь переход.
Любой режим разрешения (данные / допущение / позже / неприменимо) снимает блок.
"""

from __future__ import annotations

import json
from pathlib import Path

from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.common.serialization import utc_now_iso
from pov_generator.domain.artifacts import ArtifactRecord
from pov_generator.domain.registry import ObjectRef
from pov_generator.domain.tasks import TaskRecord
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]

# Два компонента: у «ingest» есть блокирующий реквизит, у «store» — нет.
_MODEL = {
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
            "requisites": [
                {"id": "crm_creds", "kind": "credential", "title": "Доступ к API CRM", "blocking": True},
            ],
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
    )
    runtime.store_artifact(ws, artifact=artifact, content=json.dumps(_MODEL, ensure_ascii=False))

    now = utc_now_iso()
    fanout = runtime.create_task(
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
    planning = PlanningService(runtime)
    planning._expand_fan_outs(ws, snapshot)
    return ws, snapshot, runtime, planning, fanout


def _requisite_check(candidate):
    return next(ch for ch in candidate.checks if ch.name == "blocking_requisites")


def _by_origin(runtime: SqliteRuntime, ws: Path, fanout_id: str):
    return {
        t.origin_ref: t
        for t in runtime.list_tasks(ws)
        if t.parent_task_id == fanout_id
    }


def test_requisite_blocks_only_its_consumer(tmp_path: Path) -> None:
    ws, snapshot, runtime, planning, fanout = _setup(tmp_path)
    instances = _by_origin(runtime, ws, fanout.task_id)
    assert {"ingest", "store"} <= set(instances)

    candidates = {
        c.task_id: c for c in planning._recompute_admission(ws, snapshot, runtime.list_tasks(ws))
    }
    ingest_check = _requisite_check(candidates[instances["ingest"].task_id])
    store_check = _requisite_check(candidates[instances["store"].task_id])

    # Блокирует только потребителя «ingest», сосед «store» не задет.
    assert ingest_check.passed is False
    assert "Доступ к API CRM" in ingest_check.detail
    assert store_check.passed is True


def test_provision_unblocks_consumer(tmp_path: Path) -> None:
    ws, snapshot, runtime, planning, fanout = _setup(tmp_path)
    instances = _by_origin(runtime, ws, fanout.task_id)

    # Предоставляем по устойчивому ключу → блок снят.
    runtime.mark_requisite_provided(
        ws, requisite_key="architecture:ingest:crm_creds", note="выдан"
    )
    candidates = {
        c.task_id: c for c in planning._recompute_admission(ws, snapshot, runtime.list_tasks(ws))
    }
    assert _requisite_check(candidates[instances["ingest"].task_id]).passed is True


def test_deferred_resolution_also_unblocks(tmp_path: Path) -> None:
    """Обход «позже» (deferred) — тоже снятие гранулярного блока."""
    ws, snapshot, runtime, planning, fanout = _setup(tmp_path)
    instances = _by_origin(runtime, ws, fanout.task_id)

    runtime.mark_requisite_provided(
        ws, requisite_key="architecture:ingest:crm_creds", mode="deferred", note="позже"
    )
    candidates = {
        c.task_id: c for c in planning._recompute_admission(ws, snapshot, runtime.list_tasks(ws))
    }
    assert _requisite_check(candidates[instances["ingest"].task_id]).passed is True


def test_unprovide_reblocks_consumer(tmp_path: Path) -> None:
    """Ф7: снятие предоставления (un-provide) снова блокирует потребителя."""
    ws, snapshot, runtime, planning, fanout = _setup(tmp_path)
    instances = _by_origin(runtime, ws, fanout.task_id)
    key = "architecture:ingest:crm_creds"

    runtime.mark_requisite_provided(ws, requisite_key=key, note="выдан")
    after_provide = {
        c.task_id: c for c in planning._recompute_admission(ws, snapshot, runtime.list_tasks(ws))
    }
    assert _requisite_check(after_provide[instances["ingest"].task_id]).passed is True

    assert runtime.delete_requisite_provision(ws, key) is True
    after_unprovide = {
        c.task_id: c for c in planning._recompute_admission(ws, snapshot, runtime.list_tasks(ws))
    }
    assert _requisite_check(after_unprovide[instances["ingest"].task_id]).passed is False
    # idempotent: повтор по уже снятому — False.
    assert runtime.delete_requisite_provision(ws, key) is False


def test_provided_status_survives_artifact_regeneration(tmp_path: Path) -> None:
    """Ф7: стабильный ключ — предоставление переживает ре-генерацию артефакта.

    Перевыпуск component_model (новый artifact_id) не «теряет» отметку
    «предоставлено», т.к. ключ устойчив (architecture:<cid>:<rid>), а не привязан
    к id артефакта или формулировке заголовка.
    """
    ws, snapshot, runtime, planning, _ = _setup(tmp_path)
    from pov_generator.application.workspace_query_service import gather_requisites

    runtime.mark_requisite_provided(
        ws, requisite_key="architecture:ingest:crm_creds", note="выдан"
    )

    # Перевыпуск модели компонентов под новым id (та же структура реквизита).
    artifact = ArtifactRecord(
        artifact_id="cm2",
        project_id=runtime.load_manifest(ws).project_id,
        artifact_role="component_model",
        title="Модель компонентов (v2)",
        description=None,
        artifact_format="json",
        artifact_kind="primary",
        created_by_task_id=None,
        storage_path="artifacts/cm2.json",
        created_at=utc_now_iso(),
    )
    runtime.store_artifact(ws, artifact=artifact, content=json.dumps(_MODEL, ensure_ascii=False))

    items, _, _ = gather_requisites(runtime, ws)
    creds = next(i for i in items if i.key == "architecture:ingest:crm_creds")
    assert creds.status == "provided"
