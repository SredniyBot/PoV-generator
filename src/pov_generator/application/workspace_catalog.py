from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..common.errors import NotFoundError
from ..infrastructure.sqlite_runtime import ProjectManifest, SqliteRuntime


@dataclass(frozen=True)
class WorkspaceRef:
    project_id: str
    workspace: Path
    manifest: ProjectManifest


class WorkspaceCatalog:
    def __init__(self, runtime_root: Path, runtime: SqliteRuntime) -> None:
        self._runtime_root = runtime_root
        self._runtime = runtime

    @property
    def runtime_root(self) -> Path:
        return self._runtime_root

    def list_workspaces(self) -> list[WorkspaceRef]:
        if not self._runtime_root.exists():
            return []
        items: list[WorkspaceRef] = []
        for manifest_path in self._runtime_root.rglob(self._runtime.MANIFEST_FILENAME):
            workspace = manifest_path.parent
            try:
                manifest = self._runtime.load_manifest(workspace)
            except Exception:
                continue
            items.append(WorkspaceRef(project_id=manifest.project_id, workspace=workspace, manifest=manifest))
        return sorted(items, key=lambda item: (item.manifest.created_at, item.project_id), reverse=True)

    def resolve_workspace(self, project_id: str) -> WorkspaceRef:
        for item in self.list_workspaces():
            if item.project_id == project_id:
                return item
        raise NotFoundError(f"Проект '{project_id}' не найден в каталоге runtime.")
