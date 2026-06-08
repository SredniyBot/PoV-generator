"""Реквизиты v2 (Ф5b): файл-реквизит (отдельный бакет) + посев в build-том.

Проверяем:
1. вложение-реквизит помечается purpose="requisite" (не «входной материал»);
2. предоставленные данные компонента доходят до harness-узла как файлы /work:
   значение → текстовый файл, файл → извлечённый текст вложения;
3. секрет (reference) в build-том не сеется.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from pov_generator.application.attachment_service import AttachmentService
from pov_generator.application.context_service import ContextService
from pov_generator.application.execution_service import ExecutionService
from pov_generator.application.project_service import ProjectService
from pov_generator.common.serialization import utc_now_iso
from pov_generator.domain.artifacts import ArtifactRecord
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

_MODEL = {
    "components": [
        {
            "id": "ingest",
            "name": "Приём заявок",
            "requisites": [
                {"id": "fmt", "kind": "interface_format", "title": "Формат формы", "blocking": False},
                {"id": "crm_creds", "kind": "credential", "title": "Доступ к CRM", "blocking": True},
            ],
        }
    ],
    "coverage": {"actors": [], "external_systems": []},
}


def _setup(tmp_path: Path):
    runtime = SqliteRuntime()
    ws = tmp_path / "ws"
    pid = (
        ProjectService(runtime)
        .init_project(
            workspace=ws,
            name="T",
            objective_ref=ObjectRef.parse("common.requirements_specification@1.0.0"),
            request_text="r",
            domain_packs=(),
        )
        .manifest.project_id
    )
    artifact = ArtifactRecord(
        artifact_id="cm1",
        project_id=pid,
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
    return runtime, ws, pid


def test_requisite_attachment_tagged_separate_bucket(tmp_path: Path) -> None:
    runtime, ws, pid = _setup(tmp_path)
    svc = AttachmentService(runtime)
    req = svc.upload(
        ws, pid, filename="sample.csv", content=b"a,b\n1,2",
        extract_in_background=False, purpose="requisite",
    )
    inp = svc.upload(
        ws, pid, filename="brief.txt", content="бриф".encode("utf-8"),
        extract_in_background=False,
    )
    assert runtime.load_attachment(ws, req.attachment_id).purpose == "requisite"
    assert runtime.load_attachment(ws, inp.attachment_id).purpose == "input"


def test_value_requisite_seeds_harness_input(tmp_path: Path) -> None:
    runtime, ws, _ = _setup(tmp_path)
    runtime.mark_requisite_provided(
        ws, requisite_key="architecture:ingest:fmt", mode="value", value="JSON {name,email}"
    )
    exec_service = ExecutionService(runtime, ContextService(runtime))
    inputs = exec_service._collect_requisite_inputs(ws, SimpleNamespace(origin_ref="ingest"))
    assert any(v == "JSON {name,email}" for v in inputs.values())
    assert all(name.startswith("requisite_") for name in inputs)


def test_file_requisite_seeds_extracted_text(tmp_path: Path) -> None:
    runtime, ws, pid = _setup(tmp_path)
    svc = AttachmentService(runtime)
    rec = svc.upload(
        ws, pid, filename="rows.csv", content="col\nзначение".encode("utf-8"),
        extract_in_background=False, purpose="requisite",
    )
    runtime.mark_requisite_provided(
        ws, requisite_key="architecture:ingest:fmt", mode="file", attachment_id=rec.attachment_id
    )
    exec_service = ExecutionService(runtime, ContextService(runtime))
    inputs = exec_service._collect_requisite_inputs(ws, SimpleNamespace(origin_ref="ingest"))
    assert any("значение" in v for v in inputs.values())


def test_credential_reference_not_seeded(tmp_path: Path) -> None:
    runtime, ws, _ = _setup(tmp_path)
    # credential выдан как reference (без значения) — в build-том не попадает.
    runtime.mark_requisite_provided(
        ws, requisite_key="architecture:ingest:crm_creds", mode="reference", note="выдан"
    )
    exec_service = ExecutionService(runtime, ContextService(runtime))
    inputs = exec_service._collect_requisite_inputs(ws, SimpleNamespace(origin_ref="ingest"))
    assert inputs == {}
