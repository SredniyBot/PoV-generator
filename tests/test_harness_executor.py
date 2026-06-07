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
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
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
    brief = render_harness_brief(
        artifact_role="demo_output",
        system_prompt="SYS",
        user_prompt="USR",
        expected_artifacts=(ExpectedArtifact(role="demo_output", fmt="json"),),
    )
    assert "SYS" in brief and "USR" in brief
    assert "demo_output" in brief
    assert ".povgen/out/demo_output.json" in brief


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


def _harness_leaf_task(project_id: str, objective_ref: str) -> TaskRecord:
    now = utc_now_iso()
    return TaskRecord(
        task_id=str(uuid.uuid4()),
        project_id=project_id,
        objective_ref=objective_ref,
        parent_task_id=None,
        template_ref=DEMO_TEMPLATE_REF,
        template_type="leaf",
        title="Демо harness-узла",
        status="in_progress",
        origin_kind="system",
        origin_ref=DEMO_TEMPLATE_REF,
        stable_key=f"{objective_ref}:harness.demo",
        depth=0,
        slot_id=None,
        attempt=0,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


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

    # Учёт токенов: estimated-usage записан (downstream одинаков для всех бэкендов).
    usage = runtime.llm_usage_for_task(workspace, task.task_id)
    assert usage is not None
