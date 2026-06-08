"""Регресс: подвкладка/этап гейта не должны задваиваться.

Баг (после кросс-objective отката): active-цель оставалась в
``objective_history``, и степпер показывал её дважды (done + active). Проверяем,
что ``project_stages`` дедупит, а нормализация истории в откате чинит источник.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.application.workspace_catalog import WorkspaceCatalog
from pov_generator.application.workspace_query_service import WorkspaceQueryService
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]
TZ_REF = "common.requirements_specification@1.0.0"


def test_project_stages_dedups_active_in_history(tmp_path: Path) -> None:
    runtime = SqliteRuntime()
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    project_service = ProjectService(runtime)
    workspace = tmp_path / "case"
    bootstrap = project_service.init_project(
        workspace=workspace,
        name="t",
        objective_ref=ObjectRef.parse(TZ_REF),
        request_text="r",
        domain_packs=(),
    )
    project_id = bootstrap.manifest.project_id

    # Инжектируем баг-состояние: active-цель попала в objective_history.
    manifest = runtime.load_manifest(workspace)
    runtime.update_manifest(
        workspace, replace(manifest, objective_history=(manifest.objective_ref,))
    )

    catalog = WorkspaceCatalog(workspace.parent, runtime)
    qs = WorkspaceQueryService(catalog, registry_service, runtime, PlanningService(runtime))
    stages = qs.project_stages(project_id)

    refs = [s.objective_ref for s in stages.stages]
    # Без дублей; ТЗ ровно один раз и помечен active.
    assert len(refs) == len(set(refs))
    tz_stages = [s for s in stages.stages if s.objective_ref == TZ_REF]
    assert len(tz_stages) == 1
    assert tz_stages[0].state == "active"
