from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from ..application.context_service import ContextService
from ..application.execution_service import ExecutionService
from ..application.planning_service import PlanningService
from ..application.project_service import ProjectService
from ..application.registry_service import RegistryService
from ..application.domain_pack_selection_service import DomainPackSelectionService
from ..application.validation_service import ValidationService
from ..application.workflow_service import WorkflowService
from ..application.workspace_catalog import WorkspaceCatalog
from ..application.workspace_command_service import WorkspaceCommandService
from ..application.workspace_query_service import WorkspaceQueryService
from ..common.env import load_repo_env
from ..common.errors import PovGeneratorError
from ..common.serialization import to_primitive, utc_now_iso
from ..infrastructure.filesystem_registry import FilesystemRegistryLoader
from ..infrastructure.sqlite_runtime import SqliteRuntime


def create_app(
    *,
    repo_root: Path | None = None,
    runtime_root: Path | None = None,
    websocket_poll_interval: float = 0.75,
) -> FastAPI:
    resolved_repo_root = repo_root or Path(__file__).resolve().parents[3]
    load_repo_env(resolved_repo_root)
    app = FastAPI(title="PoV Generator Operator API", version="0.1.0")

    resolved_runtime_root = runtime_root or (resolved_repo_root / "runtime")
    ui_dist_root = resolved_repo_root / "ui" / "workspace" / "dist"

    registry_service = RegistryService(FilesystemRegistryLoader(resolved_repo_root / "templates"))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)
    context_service = ContextService(runtime)
    execution_service = ExecutionService(runtime, context_service)
    validation_service = ValidationService(runtime)
    workflow_service = WorkflowService(runtime, planning_service, execution_service, validation_service)
    catalog = WorkspaceCatalog(resolved_runtime_root, runtime)
    query_service = WorkspaceQueryService(catalog, registry_service, runtime, planning_service)
    domain_pack_selection_service = DomainPackSelectionService()
    command_service = WorkspaceCommandService(
        catalog,
        registry_service,
        project_service,
        planning_service,
        workflow_service,
        domain_pack_selection_service,
    )

    app.state.query_service = query_service
    app.state.command_service = command_service
    app.state.poll_interval = websocket_poll_interval

    @app.exception_handler(PovGeneratorError)
    async def pov_error_handler(_, exc: PovGeneratorError):
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "time": utc_now_iso(),
            "runtime_root": str(resolved_runtime_root),
        }

    @app.get("/api/projects")
    def list_projects() -> Any:
        return to_primitive(query_service.list_projects())

    @app.post("/api/projects")
    def create_project(payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        domain_pack_refs = payload.get("domain_pack_refs", [])
        if not isinstance(domain_pack_refs, list):
            raise PovGeneratorError("Поле 'domain_pack_refs' должно быть списком.")
        return to_primitive(
            command_service.create_project(
                name=_required_str(payload, "name"),
                recipe_ref=_required_str(payload, "recipe_ref"),
                request_text=_required_str(payload, "request_text"),
                domain_pack_refs=tuple(_required_string_list(domain_pack_refs, "domain_pack_refs")),
                selection_provider=_optional_str(payload, "selection_provider"),
                selection_model=_optional_str(payload, "selection_model"),
            )
        )

    @app.get("/api/registry/recipes")
    def list_recipes() -> Any:
        return to_primitive(query_service.list_recipes())

    @app.get("/api/registry/domain-packs")
    def list_domain_packs() -> Any:
        return to_primitive(query_service.list_domain_packs())

    @app.get("/api/projects/{project_id}/shell")
    def project_shell(project_id: str) -> Any:
        return to_primitive(query_service.project_shell(project_id))

    @app.get("/api/projects/{project_id}/journey")
    def project_journey(project_id: str) -> Any:
        return to_primitive(query_service.project_journey(project_id))

    @app.get("/api/projects/{project_id}/situation")
    def project_situation(project_id: str) -> Any:
        return to_primitive(query_service.project_situation(project_id))

    @app.get("/api/projects/{project_id}/timeline")
    def project_timeline(project_id: str, after_sequence: int = 0) -> Any:
        return to_primitive(query_service.project_timeline(project_id, after_sequence=after_sequence))

    @app.get("/api/projects/{project_id}/artifacts")
    def project_artifacts(project_id: str) -> Any:
        return to_primitive(query_service.project_artifacts(project_id))

    @app.get("/api/projects/{project_id}/artifacts/{artifact_id}")
    def project_artifact_detail(project_id: str, artifact_id: str) -> Any:
        return to_primitive(query_service.artifact_detail(project_id, artifact_id))

    @app.get("/api/projects/{project_id}/review")
    def project_review(project_id: str) -> Any:
        return to_primitive(query_service.project_review(project_id))

    @app.get("/api/projects/{project_id}/state")
    def project_state(project_id: str) -> Any:
        return to_primitive(query_service.project_state(project_id))

    @app.get("/api/projects/{project_id}/debug")
    def project_debug(project_id: str) -> Any:
        return to_primitive(query_service.project_debug(project_id))

    @app.post("/api/projects/{project_id}/commands/run-next")
    def run_next(project_id: str, payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        return to_primitive(
            command_service.run_next(
                project_id,
                provider=_optional_str(payload, "provider"),
                model=_optional_str(payload, "model"),
            )
        )

    @app.post("/api/projects/{project_id}/commands/run-until-blocked")
    def run_until_blocked(project_id: str, payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        return to_primitive(
            command_service.run_until_blocked(
                project_id,
                provider=_optional_str(payload, "provider"),
                model=_optional_str(payload, "model"),
                max_steps=int(payload.get("max_steps", 20)),
            )
        )

    @app.post("/api/projects/{project_id}/commands/retry-task")
    def retry_task(project_id: str, payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        return to_primitive(
            command_service.retry_task(
                project_id,
                task_id=_required_str(payload, "task_id"),
                provider=_optional_str(payload, "provider"),
                model=_optional_str(payload, "model"),
            )
        )

    @app.post("/api/projects/{project_id}/commands/set-goal")
    def set_goal(project_id: str, payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        return to_primitive(command_service.set_goal(project_id, text=_required_str(payload, "text")))

    @app.post("/api/projects/{project_id}/commands/close-gap")
    def close_gap(project_id: str, payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        return to_primitive(command_service.close_gap(project_id, gap_id=_required_str(payload, "gap_id")))

    @app.post("/api/projects/{project_id}/commands/set-readiness")
    def set_readiness(project_id: str, payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        return to_primitive(
            command_service.set_readiness(
                project_id,
                dimension=_required_str(payload, "dimension"),
                status=_required_str(payload, "status"),
                blocking=bool(payload.get("blocking", True)),
                confidence=float(payload.get("confidence", 1.0)),
            )
        )

    @app.post("/api/projects/{project_id}/commands/enable-domain-pack")
    def enable_domain_pack(project_id: str, payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        return to_primitive(
            command_service.enable_domain_pack(project_id, pack_ref=_required_str(payload, "pack_ref"))
        )

    @app.websocket("/ws/projects/{project_id}")
    async def project_updates(websocket: WebSocket, project_id: str) -> None:
        await websocket.accept()
        raw_projections = websocket.query_params.get("projections")
        projections = (
            tuple(name.strip() for name in raw_projections.split(",") if name.strip())
            if raw_projections
            else ("shell", "journey", "situation", "timeline", "artifacts", "review", "state")
        )
        try:
            last_token = await asyncio.to_thread(
                query_service.realtime_token,
                project_id,
            )
            await websocket.send_json(
                {
                    "type": "snapshot",
                    "project_id": project_id,
                    "projections": projections,
                    "signatures": {projection_name: last_token for projection_name in projections},
                }
            )
            while True:
                await asyncio.sleep(app.state.poll_interval)
                current_token = await asyncio.to_thread(
                    query_service.realtime_token,
                    project_id,
                )
                if current_token != last_token:
                    for projection_name in projections:
                        await websocket.send_json(
                            {
                                "type": "projection_changed",
                                "project_id": project_id,
                                "projection": projection_name,
                                "signature": current_token,
                            }
                        )
                    last_token = current_token
        except WebSocketDisconnect:
            return
        except PovGeneratorError as exc:
            await websocket.send_json({"type": "error", "message": str(exc)})
            await websocket.close()

    if ui_dist_root.exists():
        assets_root = ui_dist_root / "assets"
        if assets_root.exists():
            @app.get("/assets/{asset_path:path}", include_in_schema=False)
            def ui_assets(asset_path: str):
                asset_file = assets_root / asset_path
                if asset_file.exists() and asset_file.is_file():
                    return FileResponse(asset_file)
                return HTMLResponse(status_code=404, content="UI asset not found.")

        @app.get("/", include_in_schema=False)
        def ui_index():
            return FileResponse(ui_dist_root / "index.html")

        @app.get("/{full_path:path}", include_in_schema=False)
        def ui_spa_fallback(full_path: str):
            if full_path.startswith(("api/", "docs", "openapi.json", "redoc", "assets/")):
                return HTMLResponse(status_code=404, content="Not found.")
            index_file = ui_dist_root / "index.html"
            if index_file.exists():
                return FileResponse(index_file)
            return HTMLResponse(status_code=404, content="UI build not found.")
    else:
        @app.get("/", include_in_schema=False)
        def ui_unavailable():
            return HTMLResponse(
                status_code=200,
                content=(
                    "<html><body style='font-family:Segoe UI, sans-serif; background:#111315; color:#f5f7f8; "
                    "padding:32px'><h1>UI не собран</h1><p>Соберите frontend командой "
                    "<code>npm install && npm run build</code> в каталоге <code>ui/workspace</code>, "
                    "затем перезапустите API.</p><p>Swagger доступен по <a style='color:#78B8C9' "
                    "href='/docs'>/docs</a>.</p></body></html>"
                ),
            )

    return app


def _required_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PovGeneratorError(f"Ожидалось непустое строковое поле '{key}'.")
    return value.strip()


def _optional_str(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PovGeneratorError(f"Поле '{key}' должно быть строкой.")
    return value.strip()


def _required_string_list(values: list[object], key: str) -> list[str]:
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise PovGeneratorError(f"Каждый элемент поля '{key}' должен быть непустой строкой.")
        normalized.append(value.strip())
    return normalized


def main() -> None:
    import uvicorn

    repo_root = Path(__file__).resolve().parents[3]
    uvicorn.run(
        create_app(repo_root=repo_root, runtime_root=repo_root / "runtime"),
        host="127.0.0.1",
        port=8788,
    )
