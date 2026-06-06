"""Закрепление графа задач за проектом.

Инвариант: после первого обращения граф проекта заморожен. Правки templates/
(удаление/изменение шаблонов) не меняют снимок закреплённого проекта — его можно
смотреть и перезапускать на исходном графе.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from pov_generator.application.project_registry import ProjectRegistryResolver
from pov_generator.application.project_service import ProjectService
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]


def _init_project(runtime: SqliteRuntime, workspace: Path) -> None:
    ProjectService(runtime).init_project(
        workspace=workspace,
        name="T",
        objective_ref=ObjectRef.parse("common.requirements_specification@1.0.0"),
        request_text="req",
        domain_packs=(),
    )


def test_pinned_graph_survives_template_changes(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    shutil.copytree(REPO_ROOT / "templates", templates)
    runtime = SqliteRuntime()
    workspace = tmp_path / "ws"
    _init_project(runtime, workspace)

    resolver = ProjectRegistryResolver(runtime, templates)
    snap1 = resolver.snapshot_for(workspace)
    original_count = len(snap1.templates)
    assert original_count > 0
    # Снимок закреплён в БД.
    assert runtime.load_pinned_registry(workspace) is not None

    # Меняем граф: удаляем несколько шаблонов задач.
    removed = 0
    for path in (templates / "tasks").rglob("*.yaml"):
        path.unlink()
        removed += 1
        if removed >= 3:
            break
    assert removed == 3

    # Свежий резолвер (чистый кеш) на том же проекте — снимок из БД, а не с диска.
    fresh = ProjectRegistryResolver(runtime, templates)
    snap2 = fresh.snapshot_for(workspace)
    assert len(snap2.templates) == original_count  # граф проекта не «съехал»


def test_new_project_uses_current_templates(tmp_path: Path) -> None:
    # Новый проект, созданный после правки графа, берёт ТЕКУЩИЙ реестр.
    templates = tmp_path / "templates"
    shutil.copytree(REPO_ROOT / "templates", templates)
    runtime = SqliteRuntime()

    ws_old = tmp_path / "old"
    _init_project(runtime, ws_old)
    full_count = len(ProjectRegistryResolver(runtime, templates).snapshot_for(ws_old).templates)

    # Удаляем один шаблон задачи и создаём новый проект — он видит урезанный граф.
    next((templates / "tasks").rglob("*.yaml")).unlink()
    ws_new = tmp_path / "new"
    _init_project(runtime, ws_new)
    new_snap = ProjectRegistryResolver(runtime, templates).snapshot_for(ws_new)

    assert len(new_snap.templates) == full_count - 1
