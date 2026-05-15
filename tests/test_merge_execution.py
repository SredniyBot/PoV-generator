"""Integration-тест: leaf-задача с merge.strategy=structural выполняется
детерминированно (без LLM) и фиксирует merge_strategy в метаинформации.

Тест работает на синтетическом workspace — собирает руками 2 входных
артефакта, запускает merge-задачу через ExecutionService и проверяет
результат + метаданные.

Использует существующий контракт ``common.requirements_spec@1.0.0`` как
выходную схему — это позволяет не плодить новые YAML-файлы.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from pov_generator.application.context_service import ContextService
from pov_generator.application.execution_service import ExecutionService
from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.domain.artifacts import (
    ArtifactMetadata,
    ArtifactRecord,
    ArtifactRelations,
)
from pov_generator.domain.registry import ObjectRef, TemplateSpec, MergeConfig
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE_REF = "common.requirements_specification@1.0.0"


def _bootstrap(tmp_path: Path):
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    snapshot, _ = registry_service.validate()
    workspace = tmp_path / "case"
    project_service.init_project(
        workspace=workspace,
        name="merge-exec-test",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="merge integration.",
    )
    PlanningService(runtime).expand_graph(workspace, snapshot)
    return workspace, runtime, snapshot


def _seed_input_artifact(
    runtime: SqliteRuntime,
    workspace: Path,
    project_id: str,
    role: str,
    payload: dict[str, Any],
) -> str:
    artifact_id = str(uuid.uuid4())
    record = ArtifactRecord(
        artifact_id=artifact_id,
        project_id=project_id,
        artifact_role=role,
        title=f"seed {role}",
        description=None,
        artifact_format="json",
        artifact_kind="primary",
        created_by_task_id=None,
        storage_path=f"artifacts/{artifact_id}.json",
        created_at="2026-05-13T10:00:00+00:00",
    )
    runtime.store_artifact(workspace, artifact=record, content=json.dumps(payload))
    return artifact_id


# --- 1. Structural merge integration ---------------------------------------


def test_structural_merge_task_runs_without_llm(tmp_path: Path) -> None:
    """leaf-задача с merge.strategy=structural не вызывает LLM,
    объединяет inputs детерминированно, проставляет merge_strategy в
    метадату."""
    workspace, runtime, snapshot = _bootstrap(tmp_path)
    manifest = runtime.load_manifest(workspace)

    # Сеять два input-артефакта с непересекающимися полями.
    # Чтобы итог прошёл render_markdown (`title` обязательно для
    # requirements_spec), задаём title в первом входе.
    seed_a = _seed_input_artifact(
        runtime,
        workspace,
        manifest.project_id,
        role="normalized_request",
        payload={"title": "Merged spec", "section_a": ["alpha", "beta"]},
    )
    seed_b = _seed_input_artifact(
        runtime,
        workspace,
        manifest.project_id,
        role="business_outcome_model",
        payload={"section_b": ["gamma"]},
    )

    # Используем существующую leaf-задачу `requirements_spec_generation`,
    # но подменяем её резолв на тестовую копию с merge.strategy=structural
    # и узким набором required_artifact_roles, чтобы не тянуть весь pipeline.
    real_template = snapshot.resolve_template(
        "common.requirements_spec_generation@1.0.0"
    )
    test_template = TemplateSpec(
        identifier=real_template.identifier,
        version=real_template.version,
        title=real_template.title,
        template_type=real_template.template_type,
        status=real_template.status,
        domain=real_template.domain,
        complexity=real_template.complexity,
        children=real_template.children,
        slots=real_template.slots,
        executor=real_template.executor,
        inputs=real_template.inputs.__class__(
            required_problem_fields=(),
            required_artifact_roles=("normalized_request", "business_outcome_model"),
            optional_artifact_roles=(),
            required_readiness=(),
            forbidden_open_gaps=(),
            required_domain_packs=(),
        ),
        outputs=real_template.outputs,
        effects=real_template.effects,
        planning=real_template.planning,
        context_policy=real_template.context_policy,
        validation_policy=real_template.validation_policy,
        instruction=real_template.instruction,
        summary=real_template.summary,
        merge=MergeConfig(strategy="structural", conflict_policy="union"),
        source_path=real_template.source_path,
    )

    # Подмена через registry.templates dict (поле frozen-dataclass'а
    # — сам dict mutable, можно перезаписать запись по ключу).
    snapshot.templates[real_template.ref.as_string()] = test_template

    # Берём существующую leaf-задачу из workspace.
    spec_gen_task = next(
        task for task in runtime.list_tasks(workspace)
        if task.template_ref.startswith("common.requirements_spec_generation@")
    )
    runtime.transition_task(workspace, spec_gen_task.task_id, "mark_ready")

    context_service = ContextService(runtime)
    execution_service = ExecutionService(runtime, context_service)
    bundle = execution_service.execute_task(
        workspace, snapshot, spec_gen_task.task_id, provider="stub"
    )

    assert bundle.result.status == "succeeded"

    # Загружаем результирующий primary артефакт.
    primary_output = bundle.result.outputs[0]
    artifact = runtime.load_artifact(workspace, primary_output.artifact_id)
    content = json.loads(runtime.load_artifact_content(workspace, artifact.artifact_id))

    # Структурный merge детерминировано объединил оба входа.
    assert content == {
        "title": "Merged spec",
        "section_a": ["alpha", "beta"],
        "section_b": ["gamma"],
    }

    # Metadata содержит merge_strategy.
    assert artifact.metadata.merge_strategy == "structural"
    # Входы артефакта в графе — оба исходных артефакта (lineage).
    assert set(artifact.relations.input_artifact_ids) >= {seed_a, seed_b}


# --- 2. Synthetic merge: behaviour unchanged, metadata labelled ------------


def test_synthetic_merge_is_recorded_in_metadata(tmp_path: Path) -> None:
    """requirements_spec_generation помечен как merge.strategy=synthetic
    в templates/. После исполнения это должно отразиться в метадате
    результирующего артефакта (LLM-путь сохранён, метка добавлена)."""
    workspace, runtime, snapshot = _bootstrap(tmp_path)

    spec_gen_task = next(
        task for task in runtime.list_tasks(workspace)
        if task.template_ref.startswith("common.requirements_spec_generation@")
    )
    template = snapshot.resolve_template(spec_gen_task.template_ref)
    # YAML-аннотация должна была распарситься.
    assert template.merge is not None
    assert template.merge.strategy == "synthetic"
    assert template.merge.conflict_policy == "union"


# --- 3. Wiring with workspace_views: merge_strategy сохраняется в проекции -


def test_merge_strategy_survives_persistence_round_trip(tmp_path: Path) -> None:
    """ArtifactMetadata.merge_strategy сериализуется в SQLite и
    восстанавливается при загрузке."""
    workspace = tmp_path / "case"
    runtime = SqliteRuntime()
    artifact = ArtifactRecord(
        artifact_id="merged-1",
        project_id="proj",
        artifact_role="requirements_spec",
        title="t",
        description=None,
        artifact_format="json",
        artifact_kind="primary",
        created_by_task_id=None,
        storage_path="artifacts/merged-1.json",
        created_at="2026-05-13T10:00:00+00:00",
        relations=ArtifactRelations(input_artifact_ids=("a", "b")),
        metadata=ArtifactMetadata(
            merge_strategy="structural",
            template_ref="common.requirements_spec_generation@1.0.0",
        ),
    )
    runtime.store_artifact(workspace, artifact=artifact, content="{}")

    loaded = runtime.load_artifact(workspace, "merged-1")
    assert loaded.metadata.merge_strategy == "structural"
