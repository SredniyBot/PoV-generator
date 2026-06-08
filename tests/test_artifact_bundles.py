"""Ф5: общий файловый артефакт-бандл — классификация + хранение разнородных выходов.

Без Docker/harness. Проверяем доменную классификацию видов содержимого, сборку
манифеста и round-trip хранения (файлы на диске + манифест в БД), а также защиту
от path traversal.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from pov_generator.common.errors import NotFoundError
from pov_generator.common.serialization import utc_now_iso
from pov_generator.domain.artifacts import ArtifactRecord
from pov_generator.domain.bundles import build_manifest, classify_content
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

# --- классификация видов содержимого ----------------------------------------


def test_classify_by_extension() -> None:
    assert classify_content("src/app.py") == "code"
    assert classify_content("README.md") == "document"
    assert classify_content("data/config.yaml") == "data"
    assert classify_content("db/app.sqlite") == "database"
    assert classify_content("dump.sql") == "database"
    assert classify_content("dist/app.exe") == "binary"
    assert classify_content("release.tar.gz") == "archive"
    assert classify_content("Dockerfile") == "code"


def test_classify_by_magic_and_heuristic() -> None:
    assert classify_content("noext", b"SQLite format 3\x00rest") == "database"
    assert classify_content("noext", b"PK\x03\x04zip") == "archive"
    assert classify_content("noext", b"%PDF-1.7") == "document"
    assert classify_content("noext", b"\x7fELFblob") == "binary"
    assert classify_content("noext", b"plain text here") == "text"
    assert classify_content("noext", b"has\x00null") == "binary"


def test_build_manifest_mixed_and_kinds() -> None:
    files = {
        "src/main.py": b"print('hi')\n",
        "README.md": b"# Doc\n",
        "bin/tool": b"\x7fELF\x00\x00",
    }
    manifest = build_manifest(files)
    assert manifest.total_files == 3
    assert manifest.total_bytes == sum(len(v) for v in files.values())
    assert manifest.bundle_kind == "mixed"
    # Файлы отсортированы по пути и снабжены sha256.
    paths = [f.path for f in manifest.files]
    assert paths == sorted(paths)
    assert all(len(f.sha256) == 64 for f in manifest.files)
    kinds = {f.path: f.content_kind for f in manifest.files}
    assert kinds["src/main.py"] == "code"
    assert kinds["bin/tool"] == "binary"


def test_build_manifest_single_kind_and_override() -> None:
    code_only = build_manifest({"a.py": b"x=1", "b.py": b"y=2"})
    assert code_only.bundle_kind == "code"

    # Образ — частный случай: вид объявляется производителем (override).
    image = build_manifest(
        {"image.tar": b"....."},
        bundle_kind="container_image",
        kind_overrides={"image.tar": "container_image"},
    )
    assert image.bundle_kind == "container_image"
    assert image.files[0].content_kind == "container_image"


# --- хранение бандла (round-trip) -------------------------------------------


def _bundle_record(project_id: str) -> ArtifactRecord:
    artifact_id = str(uuid.uuid4())
    return ArtifactRecord(
        artifact_id=artifact_id,
        project_id=project_id,
        artifact_role="component_impl",
        title="Реализация компонента",
        description="Бандл с кодом",
        artifact_format="json",  # store_bundle_artifact переставит на "bundle"
        artifact_kind="primary",
        created_by_task_id=None,
        storage_path=f"artifacts/{artifact_id}.json",
        created_at=utc_now_iso(),
    )


def _workspace(tmp_path: Path) -> tuple[SqliteRuntime, Path, str]:
    from pov_generator.application.project_service import ProjectService
    from pov_generator.application.registry_service import RegistryService
    from pov_generator.domain.registry import ObjectRef
    from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader

    repo_root = Path(__file__).resolve().parents[1]
    runtime = SqliteRuntime()
    RegistryService(FilesystemRegistryLoader(repo_root / "templates")).validate()
    workspace = tmp_path / "ws"
    bootstrap = ProjectService(runtime).init_project(
        workspace=workspace,
        name="bundle test",
        objective_ref=ObjectRef.parse("common.requirements_specification@1.0.0"),
        request_text="Тест бандлов.",
        domain_packs=(),
    )
    return runtime, workspace, bootstrap.manifest.project_id


def test_store_and_read_bundle_roundtrip(tmp_path: Path) -> None:
    runtime, workspace, project_id = _workspace(tmp_path)
    files = {
        "src/main.py": b"print('hello')\n",
        "docs/readme.md": b"# Component\n",
        "db/seed.sqlite": b"SQLite format 3\x00....",
    }
    record, manifest = runtime.store_bundle_artifact(
        workspace, artifact=_bundle_record(project_id), files=files
    )
    assert record.artifact_format == "bundle"
    assert manifest.total_files == 3
    assert manifest.bundle_kind == "mixed"

    # Манифест читается из строки артефакта.
    loaded = runtime.load_bundle_manifest(workspace, record.artifact_id)
    assert loaded.total_files == 3
    assert {f.path for f in loaded.files} == set(files)
    assert any(f.content_kind == "database" for f in loaded.files)

    # Отдельные файлы читаются с диска и совпадают по содержимому.
    assert runtime.load_bundle_file(workspace, record.artifact_id, "src/main.py") == files["src/main.py"]
    assert runtime.load_bundle_file(workspace, record.artifact_id, "db/seed.sqlite") == files["db/seed.sqlite"]

    # Файлы лежат под artifacts/<id>/, а не блобом в БД.
    assert (workspace / "artifacts" / record.artifact_id / "src" / "main.py").exists()


def test_bundle_artifact_listed_and_archivable(tmp_path: Path) -> None:
    runtime, workspace, project_id = _workspace(tmp_path)
    record, _ = runtime.store_bundle_artifact(
        workspace, artifact=_bundle_record(project_id), files={"a.py": b"x=1"}
    )
    # Виден в обычном списке артефактов (downstream/откат работают как есть).
    roles = {a.artifact_id: a for a in runtime.list_artifacts(workspace)}
    assert record.artifact_id in roles
    assert roles[record.artifact_id].artifact_format == "bundle"


def test_load_bundle_file_blocks_path_traversal(tmp_path: Path) -> None:
    runtime, workspace, project_id = _workspace(tmp_path)
    record, _ = runtime.store_bundle_artifact(
        workspace, artifact=_bundle_record(project_id), files={"ok.py": b"x=1"}
    )
    with pytest.raises(NotFoundError):
        runtime.load_bundle_file(workspace, record.artifact_id, "../../etc/passwd")


# --- категория + просмотр бандла в окне артефакта (#1/#2) --------------------


def _query_service(runtime, workspace):
    from pov_generator.application.planning_service import PlanningService
    from pov_generator.application.registry_service import RegistryService
    from pov_generator.application.workspace_catalog import WorkspaceCatalog
    from pov_generator.application.workspace_query_service import WorkspaceQueryService
    from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader

    repo_root = Path(__file__).resolve().parents[1]
    catalog = WorkspaceCatalog(workspace.parent, runtime)
    registry = RegistryService(FilesystemRegistryLoader(repo_root / "templates"))
    return WorkspaceQueryService(catalog, registry, runtime, PlanningService(runtime))


def test_code_bundle_category_and_viewer(tmp_path: Path) -> None:
    runtime, workspace, project_id = _workspace(tmp_path)
    record, _ = runtime.store_bundle_artifact(
        workspace,
        artifact=_bundle_record(project_id),
        files={"src/app.py": b"print('hi')\n", "README.md": b"# proj\n"},
    )
    qs = _query_service(runtime, workspace)

    # #1: бандл кода относится к категории «code» в списке.
    summary = {a.artifact_id: a for a in qs.project_artifacts(project_id)}
    assert summary[record.artifact_id].category == "code"

    # #2: окно артефакта отдаёт дерево файлов, а не сырой манифест.
    detail = qs.artifact_detail(project_id, record.artifact_id)
    assert detail.is_bundle is True
    assert detail.bundle_kind in {"code", "mixed"}
    assert {f.path for f in detail.bundle_files} == {"src/app.py", "README.md"}

    # Содержимое файла читается для просмотра.
    got = qs.bundle_file_text(project_id, record.artifact_id, "src/app.py")
    assert got["binary"] is False
    assert "print('hi')" in got["text"]


def test_document_artifact_category_is_documents(tmp_path: Path) -> None:
    runtime, workspace, project_id = _workspace(tmp_path)
    # Структурный артефакт (input.request от init_project) — категория documents.
    qs = _query_service(runtime, workspace)
    cats = {a.category for a in qs.project_artifacts(project_id)}
    assert cats == {"documents"} or "documents" in cats
