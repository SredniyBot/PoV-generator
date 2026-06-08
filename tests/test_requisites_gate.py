"""Реквизиты Ф5: многоисточниковая агрегация + шлюз перехода.

Проверяем:
1. извлечение реквизитов из модели компонентов (kind/blocking/stage/ключ);
2. агрегацию из двух источников (реализуемость + архитектура) с дедупом;
3. шлюз: непредоставленные блокирующие реквизиты держат переход, после
   предоставления — отпускают.
"""

from __future__ import annotations

import json
from pathlib import Path

from pov_generator.application.project_service import ProjectService
from pov_generator.application.workspace_query_service import (
    _extract_component_model_requisites,
    blocking_requisites_unprovided,
    gather_advisory_prerequisites,
    gather_requisites,
)
from pov_generator.domain.artifacts import ArtifactRecord
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

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
                    "needed_for": "store.saveRequest",
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

_FEASIBILITY = {
    "capabilities": [
        {"name": "CRUD-сервис", "prerequisites": ["Описание сущностей"]},
    ]
}


def _store(runtime: SqliteRuntime, workspace: Path, role: str, payload: dict, aid: str) -> None:
    artifact = ArtifactRecord(
        artifact_id=aid,
        project_id="p",
        artifact_role=role,
        title=role,
        description=None,
        artifact_format="json",
        artifact_kind="primary",
        created_by_task_id=None,
        storage_path=f"artifacts/{aid}.json",
        created_at="2026-06-07T00:00:00+00:00",
    )
    runtime.store_artifact(workspace, artifact=artifact, content=json.dumps(payload, ensure_ascii=False))


# --- 1. Извлечение из модели компонентов ------------------------------------


def test_extract_component_model_requisites() -> None:
    items = _extract_component_model_requisites(_COMPONENT_MODEL)
    by_title = {i.title: i for i in items}
    creds = by_title["Доступ к API CRM"]
    assert creds.kind == "credential"
    assert creds.blocking is True
    assert creds.stage == "architecture"
    assert creds.key == "architecture:ingest:crm_creds"
    assert creds.needed_for == "Приём заявок"
    assert by_title["Точный формат формы"].blocking is False


def test_extract_component_model_tolerates_malformed() -> None:
    assert _extract_component_model_requisites({}) == ()
    assert _extract_component_model_requisites({"components": "нет"}) == ()
    assert _extract_component_model_requisites({"components": [None, {}, {"requisites": "x"}]}) == ()


# --- 2. Разделение actionable (архитектура) vs advisory (предпосылки) --------


def test_actionable_and_advisory_are_separate(tmp_path: Path) -> None:
    """Редизайн: конкретные запросы данных (модель компонентов) — в actionable;
    предпосылки реализуемости (условия) — отдельно, advisory."""
    runtime = SqliteRuntime()
    ws = tmp_path / "case"
    _store(runtime, ws, "feasibility_assessment", _FEASIBILITY, "f1")
    _store(runtime, ws, "component_model", _COMPONENT_MODEL, "c1")

    items, source_id, _ = gather_requisites(runtime, ws)
    item_titles = {i.title for i in items}
    assert "Доступ к API CRM" in item_titles  # actionable (архитектура)
    assert "Описание сущностей" not in item_titles  # предпосылка → не actionable
    assert all(i.stage == "architecture" for i in items)
    assert source_id is not None

    advisory = gather_advisory_prerequisites(runtime, ws)
    adv_titles = {i.title for i in advisory}
    assert "Описание сущностей" in adv_titles  # предпосылка реализуемости
    assert "Доступ к API CRM" not in adv_titles


def test_gather_missing_when_no_sources(tmp_path: Path) -> None:
    runtime = SqliteRuntime()
    ws = tmp_path / "empty"
    runtime.list_requisite_provisions(ws)  # инициализирует БД
    items, source_id, _ = gather_requisites(runtime, ws)
    assert items == ()
    assert source_id is None
    assert gather_advisory_prerequisites(runtime, ws) == ()


# --- 3. Шлюз перехода --------------------------------------------------------


def test_gate_holds_until_blocking_requisite_provided(tmp_path: Path) -> None:
    runtime = SqliteRuntime()
    ws = tmp_path / "case"
    # mark_requisite_provided берёт project_id из manifest — создаём проект.
    ProjectService(runtime).init_project(
        workspace=ws,
        name="T",
        objective_ref=ObjectRef.parse("common.requirements_specification@1.0.0"),
        request_text="r",
        domain_packs=(),
    )
    _store(runtime, ws, "component_model", _COMPONENT_MODEL, "c1")

    # До предоставления: блокирующий реквизит держит переход.
    assert blocking_requisites_unprovided(runtime, ws) == ("Доступ к API CRM",)

    # Предоставляем по устойчивому ключу — шлюз отпускает.
    runtime.mark_requisite_provided(ws, requisite_key="architecture:ingest:crm_creds", note="выдан")
    assert blocking_requisites_unprovided(runtime, ws) == ()

    # Неблокирующий реквизит виден, но переход не держит.
    items, _, _ = gather_requisites(runtime, ws)
    fmt = next(i for i in items if i.title == "Точный формат формы")
    assert fmt.status == "requested"
    assert fmt.blocking is False
