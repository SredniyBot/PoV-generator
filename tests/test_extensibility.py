"""Тесты расширяемости (Этап 7 roadmap).

Покрывает:

* **7.3 — выводимая финальная композиция.** Финальная merge-задача
  (``requirements_spec_generation``) не хранит hand-coded список
  optional артефактов. Вместо этого активные доменные паки
  автоматически подмешивают свои primary-артефакты через
  ``collect_optional_from_active_domain_packs``.

* **7.5 — честные контракты.** Псевдо-контракты YAML с
  ``additionalProperties: true`` и без обязательных полей помечены
  ``unstructured: true``; реальную валидацию выполняет hand-coded
  ``application/artifact_contracts.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pov_generator.application.context_service import ContextService
from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.domain.artifacts import (
    ArtifactMetadata,
    ArtifactRecord,
    ArtifactRelations,
)
from pov_generator.domain.registry import ObjectRef
from pov_generator.domain.tasks import TaskRecord
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE_REF = "common.requirements_specification@1.0.0"


# --- 7.5 honest contracts --------------------------------------------------


class TestUnstructuredContracts:
    def test_registry_loads_unstructured_flag(self) -> None:
        snapshot, _ = RegistryService(
            FilesystemRegistryLoader(REPO_ROOT / "templates")
        ).validate()

        # Семейство pseudo-контрактов помечены явно.
        pseudo_ids = [
            "common.goal_hypothesis@1.0.0",
            "common.requirements_spec@1.0.0",
            "ml.predictive_problem_definition@1.0.0",
            "security.security_compliance_constraints@1.0.0",
            "frontend.ui_requirements_outline@1.0.0",
        ]
        for pid in pseudo_ids:
            contract = snapshot.artifact_contracts[pid]
            assert contract.unstructured is True, (
                f"Контракт {pid} имеет пустую/пермиссивную схему — "
                "должен быть явно помечен unstructured=true."
            )

    def test_unstructured_defaults_to_false(self) -> None:
        """Если поле unstructured не задано в YAML, считается False."""
        from pov_generator.domain.registry import parse_artifact_contract

        contract = parse_artifact_contract(
            {
                "kind": "artifact_contract",
                "id": "test.with_schema",
                "version": "1.0.0",
                "title": "T",
                "schema": {"type": "object", "required": ["x"]},
            },
            Path("/dev/null"),
        )
        assert contract.unstructured is False

    def test_unstructured_true_parses(self) -> None:
        from pov_generator.domain.registry import parse_artifact_contract

        contract = parse_artifact_contract(
            {
                "kind": "artifact_contract",
                "id": "test.no_schema",
                "version": "1.0.0",
                "title": "T",
                "schema": {"type": "object", "additionalProperties": True},
                "unstructured": True,
            },
            Path("/dev/null"),
        )
        assert contract.unstructured is True


# --- 7.3 derived composition ----------------------------------------------


def _bootstrap_with_domain_packs(tmp_path: Path, packs: tuple[str, ...]):
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    snapshot, _ = registry_service.validate()
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    workspace = tmp_path / "case"
    domain_packs = tuple(snapshot.resolve_domain_pack(ref) for ref in packs)
    project_service.init_project(
        workspace=workspace,
        name="auto-collect-test",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="Тест auto-collect доменов.",
        domain_packs=domain_packs,
    )
    PlanningService(runtime).expand_graph(workspace, snapshot)
    return runtime, workspace, snapshot


class TestDerivedComposition:
    def test_requirements_spec_generation_uses_auto_collect(self) -> None:
        """Шаблон финального ТЗ помечен collect_optional_from_active_domain_packs."""
        snapshot, _ = RegistryService(
            FilesystemRegistryLoader(REPO_ROOT / "templates")
        ).validate()
        template = snapshot.resolve_template("common.requirements_spec_generation@1.0.0")
        assert template.inputs.collect_optional_from_active_domain_packs is True
        # И hand-coded optional пустой — больше не нужно их перечислять.
        assert template.inputs.optional_artifact_roles == ()

    def test_new_domain_pack_artifact_auto_appears_in_context(self, tmp_path: Path) -> None:
        """Когда активен ml-пак, его primary-артефакт (predictive_problem_definition)
        автоматически попадает в контекст финального merge — даже если YAML
        финального шаблона не упоминает его в optional."""
        runtime, workspace, snapshot = _bootstrap_with_domain_packs(
            tmp_path, packs=("ml.predictive_analytics@1.0.0",)
        )

        # Сеять primary артефакт от ml-доменной задачи.
        ml_task = next(
            t for t in runtime.list_tasks(workspace)
            if t.origin_kind == "domain_contribution"
            and t.template_ref.startswith("ml.")
        )
        artifact = ArtifactRecord(
            artifact_id="ml-art-1",
            project_id=ml_task.project_id,
            artifact_role="predictive_problem_definition",
            title="ML problem",
            description=None,
            artifact_format="json",
            artifact_kind="primary",
            created_by_task_id=ml_task.task_id,
            storage_path="artifacts/ml-art-1.json",
            created_at="2026-05-13T10:00:00+00:00",
            relations=ArtifactRelations(),
            metadata=ArtifactMetadata(),
        )
        runtime.store_artifact(workspace, artifact=artifact, content="{}")

        # Сеять обязательные core-артефакты (минимум для admission).
        for role in [
            "normalized_request",
            "business_outcome_model",
            "scope_boundary_matrix",
            "stakeholder_map",
            "solution_option_inventory",
        ]:
            stub = ArtifactRecord(
                artifact_id=f"core-{role}",
                project_id=ml_task.project_id,
                artifact_role=role,
                title=role,
                description=None,
                artifact_format="json",
                artifact_kind="primary",
                created_by_task_id=None,
                storage_path=f"artifacts/core-{role}.json",
                created_at="2026-05-13T10:00:00+00:00",
            )
            runtime.store_artifact(workspace, artifact=stub, content="{}")

        # Собираем контекст финальной merge-задачи.
        final_task = next(
            t for t in runtime.list_tasks(workspace)
            if t.template_ref.startswith("common.requirements_spec_generation@")
        )
        ctx_result = ContextService(runtime).build_for_task(
            workspace, snapshot, final_task.task_id
        )

        artifact_refs = {
            item.source_ref for item in ctx_result.manifest.items
            if item.item_type == "artifact"
        }
        assert "artifact:ml-art-1" in artifact_refs, (
            "ml-доменный артефакт должен попасть в контекст финальной задачи "
            "через collect_optional_from_active_domain_packs."
        )

    def test_auto_collect_off_by_default(self, tmp_path: Path) -> None:
        """Для шаблонов без флага — старое поведение (только явный optional)."""
        from pov_generator.domain.registry import parse_task_template

        template = parse_task_template(
            {
                "kind": "task_template",
                "id": "common.test_no_collect",
                "version": "1.0.0",
                "title": "T",
                "type": "leaf",
                "status": "active",
                "executor": "stub",
                "requires": {
                    "state": [],
                    "artifacts": {"required": [], "optional": []},
                    "readiness": [],
                    "forbidden_open_gaps": [],
                    "domain_packs": [],
                },
                "produces": {"artifact": "common.requirements_spec@1.0.0"},
                "effects": {"readiness": {"set": []}, "gaps": {"close": []}},
                "context": {"include": []},
                "planning": {"priority": 0},
                "validation": {},
            },
            Path("/dev/null"),
        )
        assert template.inputs.collect_optional_from_active_domain_packs is False
