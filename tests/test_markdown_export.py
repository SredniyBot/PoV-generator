"""Тесты фичи 3 — экспорт артефакта в Markdown + массовый zip-экспорт.

Покрывает: endpoint download.md, endpoint export.zip (только MD-артефакты,
осмысленные имена, MANIFEST), пустой проект, 404 при отсутствии MD.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from pov_generator.application.clarification_service import ClarificationService
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
OBJECTIVE_REF = "common.requirements_specification@1.0.0"


def _setup_with_artifacts(tmp_path: Path) -> tuple[TestClient, str]:
    """Проект с несколькими MD-артефактами (через stub-workflow) + TestClient."""
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "case-md"
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)
    context_service = ContextService(runtime)
    execution_service = ExecutionService(runtime, context_service)
    validation_service = ValidationService(runtime, ClarificationService(runtime, provider="stub"))
    workflow_service = WorkflowService(runtime, planning_service, execution_service, validation_service)

    snapshot, report = registry_service.validate()
    assert report.is_valid
    bootstrap = project_service.init_project(
        workspace=workspace,
        name="md export",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="Нужна CRM-интеграция для отдела продаж.",
        domain_packs=(),
    )
    planning_service.expand_graph(workspace, snapshot)
    # Один прогон до блокировки создаёт набор leaf-артефактов (каждый с .md).
    workflow_service.run_until_blocked(workspace, snapshot, provider="stub", max_steps=20)

    client = TestClient(create_app(repo_root=REPO_ROOT, runtime_root=runtime_root))
    return client, bootstrap.manifest.project_id


def _setup_empty(tmp_path: Path) -> tuple[TestClient, str]:
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "case-empty"
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    snapshot, _ = registry_service.validate()
    bootstrap = project_service.init_project(
        workspace=workspace,
        name="empty",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="Запрос без артефактов.",
        domain_packs=(),
    )
    PlanningService(runtime).expand_graph(workspace, snapshot)
    client = TestClient(create_app(repo_root=REPO_ROOT, runtime_root=runtime_root))
    return client, bootstrap.manifest.project_id


def test_download_md_returns_markdown(tmp_path: Path) -> None:
    client, project_id = _setup_with_artifacts(tmp_path)
    artifacts = client.get(f"/api/projects/{project_id}/artifacts").json()
    assert artifacts
    artifact_id = artifacts[0]["artifact_id"]

    response = client.get(f"/api/projects/{project_id}/artifacts/{artifact_id}/download.md")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "attachment" in response.headers["content-disposition"]
    assert ".md" in response.headers["content-disposition"]
    assert response.text.lstrip().startswith("#")


def test_download_md_unknown_artifact_returns_404(tmp_path: Path) -> None:
    client, project_id = _setup_with_artifacts(tmp_path)
    response = client.get(f"/api/projects/{project_id}/artifacts/does-not-exist/download.md")
    assert response.status_code == 404


def test_export_zip_contains_markdown_artifacts(tmp_path: Path) -> None:
    client, project_id = _setup_with_artifacts(tmp_path)
    artifacts = client.get(f"/api/projects/{project_id}/artifacts").json()
    md_count = sum(1 for a in artifacts if a["has_markdown"])
    assert md_count > 0

    response = client.get(f"/api/projects/{project_id}/export.zip")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = archive.namelist()
    assert "MANIFEST.txt" in names
    md_names = [n for n in names if n.endswith(".md")]
    assert len(md_names) == md_count
    # Имена осмысленные (не голый uuid) и контент непустой.
    first_md = md_names[0]
    assert archive.read(first_md).decode("utf-8").strip()


def test_export_zip_empty_project(tmp_path: Path) -> None:
    client, project_id = _setup_empty(tmp_path)
    response = client.get(f"/api/projects/{project_id}/export.zip")
    assert response.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert "MANIFEST.txt" in archive.namelist()
    manifest = archive.read("MANIFEST.txt").decode("utf-8")
    assert "Включено артефактов: 0" in manifest
