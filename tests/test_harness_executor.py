"""Ф1 harness-бэкенда: контракт + stub-провайдер + диспетч executor=harness.

Без Docker. Проверяем: stub-провайдер отдаёт фикстуру по роли; реестр
резолвит дефолт; brief собирается; produce_artifact_payload даёт payload;
и сквозь execute_task узел executor=harness производит артефакт тем же
downstream-путём, что LLM/stub (артефакт + execution run + usage).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from pov_generator.application.context_service import ContextService
from pov_generator.application.execution_service import ExecutionService
from pov_generator.application.harness_execution_service import (
    HarnessExecutionService,
    render_harness_brief,
)
from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.application.workspace_catalog import WorkspaceCatalog
from pov_generator.application.workspace_query_service import WorkspaceQueryService
from pov_generator.common.errors import ConflictError
from pov_generator.common.serialization import utc_now_iso
from pov_generator.domain.registry import ObjectRef
from pov_generator.domain.tasks import TaskRecord
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.harness import (
    ExpectedArtifact,
    HarnessProviderRegistry,
    HarnessRunSpec,
)
from pov_generator.infrastructure.harness.providers.stub import StubHarnessProvider
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE_REF = "common.requirements_specification@1.0.0"
DEMO_TEMPLATE_REF = "harness.demo@1.0.0"
BUNDLE_TEMPLATE_REF = "harness.demo_bundle@1.0.0"


# --- контракт/провайдер/реестр (без БД) -------------------------------------


def test_stub_harness_returns_fixture_payload() -> None:
    provider = StubHarnessProvider()
    spec = HarnessRunSpec(
        brief="...",
        expected_artifacts=(ExpectedArtifact(role="demo_output", fmt="json"),),
    )
    result = provider.run(spec)
    assert result.status == "completed"
    assert len(result.artifacts) == 1
    art = result.artifacts[0]
    assert art.role == "demo_output"
    assert art.payload is not None
    assert art.payload["produced_by"] == "stub-harness"


def test_stub_harness_fails_for_missing_role() -> None:
    provider = StubHarnessProvider()
    spec = HarnessRunSpec(
        brief="...",
        expected_artifacts=(ExpectedArtifact(role="does_not_exist", fmt="json"),),
    )
    result = provider.run(spec)
    assert result.status == "failed"
    assert result.error is not None


def test_registry_resolves_default_stub() -> None:
    registry = HarnessProviderRegistry()
    assert registry.default_provider_name() == "stub"
    assert "stub" in registry.supported_providers
    assert registry.resolve_default().name == "stub"
    with pytest.raises(ConflictError):
        registry.get("nope")


def test_render_brief_includes_expectations() -> None:
    # structured-выход: соглашение .povgen/out/<role>.<fmt> (harvest-by-convention).
    brief = render_harness_brief(
        artifact_role="demo_output",
        system_prompt="SYS",
        user_prompt="USR",
        expected_artifacts=(ExpectedArtifact(role="demo_output", fmt="json"),),
        output_kind="structured",
    )
    assert "SYS" in brief and "USR" in brief
    assert "demo_output" in brief
    assert ".povgen/out/demo_output.json" in brief


def test_render_brief_bundle_writes_code_not_povgen() -> None:
    # bundle-выход (код): агент пишет код в рабочий каталог, НЕ в .povgen и НЕ
    # JSON-описанием. Это устраняет «реализация не реализует» — корневую причину.
    brief = render_harness_brief(
        artifact_role="component_implementation",
        system_prompt="SYS",
        user_prompt="USR",
        expected_artifacts=(ExpectedArtifact(role="component_implementation", fmt="files"),),
        output_kind="bundle",
    )
    assert "исходный код" in brief.lower() or "рабочий код" in brief.lower()
    # Не должно велеть писать в служебный .povgen для bundle.
    assert ".povgen/out/component_implementation" not in brief


def test_produce_artifact_payload_returns_outcome() -> None:
    service = HarnessExecutionService()
    outcome = service.produce_artifact_payload(
        artifact_role="demo_output",
        system_prompt="SYS",
        user_prompt="USR",
    )
    assert outcome.payload["produced_by"] == "stub-harness"
    assert outcome.provider_name == "harness:stub"
    assert outcome.usage is None  # stub не отдаёт usage; оценку делает execute_task


# --- сквозной диспетч executor=harness через execute_task -------------------


def _harness_leaf_task(
    project_id: str, objective_ref: str, *, template_ref: str = DEMO_TEMPLATE_REF
) -> TaskRecord:
    now = utc_now_iso()
    return TaskRecord(
        task_id=str(uuid.uuid4()),
        project_id=project_id,
        objective_ref=objective_ref,
        parent_task_id=None,
        template_ref=template_ref,
        template_type="leaf",
        title="Демо harness-узла",
        status="in_progress",
        origin_kind="system",
        origin_ref=template_ref,
        stable_key=f"{objective_ref}:{template_ref.split('@')[0]}",
        depth=0,
        slot_id=None,
        attempt=0,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


def _bootstrap_execution(tmp_path: Path):
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    context_service = ContextService(runtime)
    execution_service = ExecutionService(runtime, context_service)
    snapshot, report = registry_service.validate()
    assert report.is_valid
    workspace = tmp_path / "case"
    bootstrap = project_service.init_project(
        workspace=workspace,
        name="harness e2e",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="Проверка узла-агента.",
        domain_packs=(),
    )
    return runtime, execution_service, snapshot, workspace, bootstrap.manifest.project_id


def test_execute_task_routes_harness_node_to_harness_backend(tmp_path: Path) -> None:
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    context_service = ContextService(runtime)
    # Без LLM-инъекций: harness-узел не должен звать LLM вовсе.
    execution_service = ExecutionService(runtime, context_service)

    snapshot, report = registry_service.validate()
    assert report.is_valid
    workspace = tmp_path / "case"
    bootstrap = project_service.init_project(
        workspace=workspace,
        name="harness e2e",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="Проверка узла-агента.",
        domain_packs=(),
    )
    project_id = bootstrap.manifest.project_id

    task = _harness_leaf_task(project_id, OBJECTIVE_REF)
    runtime.create_task(workspace, task)

    bundle = execution_service.execute_task(workspace, snapshot, task.task_id)
    assert bundle.result.status == "succeeded"
    assert any(o.artifact_role == "demo_output" for o in bundle.result.outputs)

    # Артефакт реально сохранён, с payload из harness-фикстуры и метой провайдера.
    artifact = runtime.latest_artifact_by_role(workspace, "demo_output")
    assert artifact is not None
    assert artifact.metadata.provider == "harness:stub"
    payload = json.loads(runtime.load_artifact_content(workspace, artifact.artifact_id))
    assert payload["produced_by"] == "stub-harness"

    # Ф6: провенанс прогона (L3/L4) сохранён в метаданных и пережил round-trip.
    trace = artifact.metadata.harness_trace
    assert trace["provider"] == "harness:stub"
    assert trace["output_kind"] == "structured"
    assert trace["brief"]
    assert "gates" in trace  # stub гейты не исполняет → пустой список
    assert trace["gates"] == []

    # Учёт токенов: estimated-usage записан (downstream одинаков для всех бэкендов).
    usage = runtime.llm_usage_for_task(workspace, task.task_id)
    assert usage is not None


def test_execute_task_harness_bundle_output_persists_files(tmp_path: Path) -> None:
    runtime, execution_service, snapshot, workspace, project_id = _bootstrap_execution(tmp_path)

    task = _harness_leaf_task(project_id, OBJECTIVE_REF, template_ref=BUNDLE_TEMPLATE_REF)
    runtime.create_task(workspace, task)

    bundle = execution_service.execute_task(workspace, snapshot, task.task_id)
    assert bundle.result.status == "succeeded"
    assert any(o.artifact_role == "demo_bundle" for o in bundle.result.outputs)

    # Сохранён как bundle-артефакт; файлы фикстуры-каталога доехали.
    artifact = runtime.latest_artifact_by_role(workspace, "demo_bundle")
    assert artifact is not None
    assert artifact.artifact_format == "bundle"
    assert artifact.metadata.provider == "harness:stub"

    manifest = runtime.load_bundle_manifest(workspace, artifact.artifact_id)
    paths = {f.path for f in manifest.files}
    assert "src/main.py" in paths
    assert "README.md" in paths
    code = runtime.load_bundle_file(workspace, artifact.artifact_id, "src/main.py")
    assert b"main" in code
    # Код классифицирован как код.
    kinds = {f.path: f.content_kind for f in manifest.files}
    assert kinds["src/main.py"] == "code"

    # Ф6: провенанс одинаков и для файлового выхода (output_kind=bundle).
    trace = artifact.metadata.harness_trace
    assert trace["provider"] == "harness:stub"
    assert trace["output_kind"] == "bundle"
    assert trace["brief"]


def test_harness_trace_query_exposes_provenance(tmp_path: Path) -> None:
    runtime, execution_service, snapshot, workspace, project_id = _bootstrap_execution(tmp_path)
    task = _harness_leaf_task(project_id, OBJECTIVE_REF)
    runtime.create_task(workspace, task)
    execution_service.execute_task(workspace, snapshot, task.task_id)

    catalog = WorkspaceCatalog(workspace.parent, runtime)
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    query_service = WorkspaceQueryService(
        catalog, registry_service, runtime, PlanningService(runtime)
    )

    result = query_service.task_harness_trace(project_id, task.task_id)
    assert result["trace"] is not None
    assert result["trace"]["provider"] == "harness:stub"
    assert result["primary_artifact_id"]

    # Не-harness задача (которой нет провенанса) → trace=None, без падения.
    empty = query_service.task_harness_trace(project_id, "no-such-task")
    assert empty["trace"] is None
