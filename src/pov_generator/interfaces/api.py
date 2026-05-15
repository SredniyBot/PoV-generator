from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from ..application.clarification_service import ClarificationService
from ..application.context_service import ContextService
from ..application.domain_pack_selection_service import DomainPackSelectionService
from ..application.execution_service import ExecutionService
from ..application.planning_service import PlanningService
from ..application.project_service import ProjectService
from ..application.registry_service import RegistryService
from ..application.validation_service import ValidationService
from ..application.workflow_runner_service import WorkflowRunnerService
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
    clarification_service = ClarificationService(runtime)
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)
    context_service = ContextService(runtime)
    execution_service = ExecutionService(runtime, context_service)
    validation_service = ValidationService(runtime, clarification_service)
    workflow_service = WorkflowService(runtime, planning_service, execution_service, validation_service)
    workflow_runner_service = WorkflowRunnerService(
        runtime, registry_service, workflow_service, planning_service
    )
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
        clarification_service,
    )

    app.state.query_service = query_service
    app.state.command_service = command_service
    app.state.poll_interval = websocket_poll_interval

    # ---- Startup recovery: orphan runs/tasks от прошлых процессов -------
    #
    # `WorkflowRunnerService` запускает задачи в daemon-потоках. При
    # рестарте процесса (например, после правок кода или хот-релоада) эти
    # потоки умирают, но БД остаётся с записями `workflow_runs.status =
    # 'running'` и `tasks.status = 'in_progress'`. UI это видит и
    # показывает «идёт работа», хотя реально ничего не работает; новые
    # run'ы не стартуют, потому что `latest_active_run` находит зомби.
    #
    # При старте процесса проходим по всем workspace'ам и приводим зомби в
    # консистентное состояние: run помечаем как cancelled, in_progress
    # задачи возвращаем в ready (admission на следующем планировании
    # пересчитает их).
    from dataclasses import replace as _dc_replace
    from ..common.serialization import utc_now_iso as _utc_now
    try:
        for workspace_ref in catalog.list_workspaces():
            ws = workspace_ref.workspace
            # 1. Orphan workflow_runs
            try:
                for run in runtime.list_workflow_runs(ws, project_id=workspace_ref.project_id, limit=50):
                    if run.status in {"pending", "running"}:
                        runtime.update_workflow_run(
                            ws,
                            _dc_replace(
                                run,
                                status="cancelled",
                                finished_at=_utc_now(),
                                stop_reason="process_restart",
                                last_step_summary=(
                                    (run.last_step_summary or "")
                                    + " [восстановление: процесс был перезапущен]"
                                ).strip(),
                            ),
                        )
            except Exception:
                pass
            # 2. Orphan tasks в статусе in_progress
            try:
                for task in runtime.list_tasks(ws):
                    if task.status == "in_progress":
                        try:
                            runtime.transition_task(
                                ws,
                                task.task_id,
                                "fail",
                                payload={
                                    "error_message": "Процесс был перезапущен во время исполнения задачи.",
                                    "error_type": "process_restart",
                                },
                            )
                        except Exception:
                            # state-machine может не допускать transition
                            # для каких-то редких статусов — пропускаем.
                            pass
            except Exception:
                pass
    except Exception:
        # Recovery — best-effort. Сбой здесь не должен мешать старту API.
        pass
    # ---- end recovery ----------------------------------------------------

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
                objective_ref=_required_str(payload, "objective_ref"),
                request_text=_required_str(payload, "request_text"),
                domain_pack_refs=tuple(_required_string_list(domain_pack_refs, "domain_pack_refs")),
                selection_provider=_optional_str(payload, "selection_provider"),
                selection_model=_optional_str(payload, "selection_model"),
            )
        )

    @app.get("/api/registry/objectives")
    def list_objectives() -> Any:
        return to_primitive(query_service.list_objectives())

    @app.get("/api/registry/domain-packs")
    def list_domain_packs() -> Any:
        return to_primitive(query_service.list_domain_packs())

    @app.get("/api/registry/methodology-packs")
    def list_methodology_packs() -> Any:
        return to_primitive(query_service.list_methodology_packs())

    @app.get("/api/projects/{project_id}/shell")
    def project_shell(project_id: str) -> Any:
        return to_primitive(query_service.project_shell(project_id))

    @app.get("/api/projects/{project_id}/overview")
    def project_overview(project_id: str) -> Any:
        return to_primitive(query_service.project_overview(project_id))

    @app.get("/api/projects/{project_id}/task-graph")
    def project_task_graph(project_id: str) -> Any:
        return to_primitive(query_service.project_task_graph(project_id))

    @app.get("/api/projects/{project_id}/situation")
    def project_situation(project_id: str) -> Any:
        return to_primitive(query_service.project_situation(project_id))

    @app.get("/api/projects/{project_id}/timeline")
    def project_timeline(project_id: str, after_sequence: int = 0) -> Any:
        return to_primitive(query_service.project_timeline(project_id, after_sequence=after_sequence))

    @app.get("/api/projects/{project_id}/clarifications")
    def project_clarifications(project_id: str) -> Any:
        return to_primitive(query_service.project_clarifications(project_id))

    @app.get("/api/projects/{project_id}/clarifications/{clarification_id}")
    def project_clarification_detail(project_id: str, clarification_id: str) -> Any:
        return to_primitive(query_service.clarification_detail(project_id, clarification_id))

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

    @app.get("/api/projects/{project_id}/tasks/{task_id}/methodology-trace")
    def task_methodology_trace(project_id: str, task_id: str) -> Any:
        return to_primitive(query_service.task_methodology_trace(project_id, task_id))

    # ------ L6 design extensions ------------------------------------------
    @app.get("/api/projects/{project_id}/artifacts/{artifact_id}/skeleton")
    def project_artifact_skeleton(project_id: str, artifact_id: str) -> Any:
        return to_primitive(query_service.artifact_skeleton(project_id, artifact_id))

    @app.get("/api/projects/{project_id}/decisions")
    def project_decision_log(project_id: str) -> Any:
        return to_primitive(query_service.project_decision_log(project_id))

    @app.get("/api/projects/{project_id}/artifact-versions")
    def project_artifact_versions(project_id: str) -> Any:
        return to_primitive(query_service.project_artifact_versions(project_id))

    @app.get("/api/projects/{project_id}/failure-pins")
    def project_failure_pins(project_id: str, artifact_id: str | None = None) -> Any:
        return to_primitive(query_service.project_failure_pins(project_id, artifact_id))

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
        # W4.1 (R1): запуск асинхронный. Endpoint возвращает свежесозданную
        # WorkflowRunRecord (status=pending) сразу. UI наблюдает прогресс
        # через GET /workflow-runs/active (polling) или WS broadcast,
        # которое поднимается каждый раз когда runner UPDATE'ит запись.
        workspace_ref = catalog.resolve_workspace(project_id)
        # W5.1: cap снят. По умолчанию даём workflow пройти столько шагов,
        # сколько нужно до естественной блокировки (objective_completed /
        # planner_blocked / validation_failed). 100 — sanity ceiling против
        # бесконечной петли при поломке планировщика; cancel доступен в UI.
        record = workflow_runner_service.start_run_until_blocked(
            workspace_ref.workspace,
            project_id,
            provider=_optional_str(payload, "provider"),
            model=_optional_str(payload, "model"),
            # Дефолт 1000 — эффективно «без лимита» (sanity ceiling против петли
            # планировщика). Раньше был 100; для реальных проектов с retry'ями и
            # composite-задачами этого мало не было, но «1000» делает явным:
            # пользователь не должен думать про лимиты, workflow доезжает до
            # естественного финала (objective_completed / planner_blocked).
            max_steps=int(payload.get("max_steps", 1000)),
            continue_past_validation_failure=bool(payload.get("continue_past_validation_failure", False)),
        )
        return to_primitive(record)

    @app.post("/api/projects/{project_id}/commands/cancel-workflow")
    def cancel_workflow(project_id: str, payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        workspace_ref = catalog.resolve_workspace(project_id)
        run_id = _required_str(payload, "run_id")
        cancelled = workflow_runner_service.cancel_run(workspace_ref.workspace, run_id)
        return {"status": "accepted" if cancelled else "not_found", "run_id": run_id}

    @app.get("/api/projects/{project_id}/workflow-runs/active")
    def workflow_runs_active(project_id: str) -> Any:
        workspace_ref = catalog.resolve_workspace(project_id)
        record = workflow_runner_service.latest_active_run(workspace_ref.workspace, project_id)
        return to_primitive(record) if record is not None else None

    @app.get("/api/projects/{project_id}/workflow-runs")
    def workflow_runs_list(project_id: str, limit: int = 20) -> Any:
        workspace_ref = catalog.resolve_workspace(project_id)
        return to_primitive(
            workflow_runner_service.list_runs(workspace_ref.workspace, project_id=project_id, limit=limit)
        )

    @app.get("/api/projects/{project_id}/workflow-runs/{run_id}")
    def workflow_run_detail(project_id: str, run_id: str) -> Any:
        workspace_ref = catalog.resolve_workspace(project_id)
        record = workflow_runner_service.get_run(workspace_ref.workspace, run_id)
        if record is None:
            return JSONResponse(status_code=404, content={"error": "run_not_found"})
        return to_primitive(record)

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

    def _autoresume_workflow_if_unblocked(project_id: str) -> None:
        """Авто-продолжает workflow когда у проекта не осталось blocking
        clarifications в статусе open. Идемпотентно: если запущен
        активный run, ничего не делает.

        Решает жалобу: после ответа на последний вопрос workflow стоял
        пока пользователь не нажмёт «Run» вручную.

        Особенности:
        - Provider/model берём из последнего workflow_run этого проекта,
          чтобы auto-resume использовал ту же модель, что и manual «Run».
          Иначе runner мог свалиться в stub-провайдер из env и выдать
          мусор, который проваливал валидацию.
        - `continue_past_validation_failure=True`: одна валящаяся задача
          (например, низкоуверенный goal_hypothesis) не должна
          блокировать весь pipeline. Planner после её failed-статуса
          сам перейдёт к следующей готовой задаче (например,
          request_normalization).
        """
        try:
            workspace_ref = catalog.resolve_workspace(project_id)
        except Exception:
            return
        if workflow_runner_service.latest_active_run(workspace_ref.workspace, project_id) is not None:
            return
        runtime_local = workflow_runner_service._runtime  # type: ignore[attr-defined]
        try:
            blocking = [
                req
                for req in runtime_local.list_clarification_requests(
                    workspace_ref.workspace, statuses=("open",)
                )
                if req.blocking_scope != "none"
            ]
        except Exception:
            return
        if blocking:
            return

        # Подхватываем provider/model из последнего run проекта —
        # пользователь явно выбрал их через UI, не теряем настройку.
        provider: str | None = None
        model: str | None = None
        try:
            recent_runs = workflow_runner_service.list_runs(
                workspace_ref.workspace, project_id=project_id, limit=1
            )
            if recent_runs:
                provider = recent_runs[0].provider
                model = recent_runs[0].model
        except Exception:
            pass

        try:
            workflow_runner_service.start_run_until_blocked(
                workspace_ref.workspace,
                project_id,
                provider=provider,
                model=model,
                max_steps=1000,
                continue_past_validation_failure=True,
            )
        except Exception:
            # Best-effort: ошибка авто-resume не должна ломать ответ пользователя.
            pass

    @app.post("/api/projects/{project_id}/commands/answer-clarification")
    def answer_clarification(project_id: str, payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        selected_option_ids = payload.get("selected_option_ids", [])
        if not isinstance(selected_option_ids, list):
            raise PovGeneratorError("Поле 'selected_option_ids' должно быть списком.")
        result = to_primitive(
            command_service.answer_clarification(
                project_id,
                clarification_id=_required_str(payload, "clarification_id"),
                selected_option_ids=tuple(_required_string_list(selected_option_ids, "selected_option_ids")),
                free_text=_optional_str(payload, "free_text"),
            )
        )
        _autoresume_workflow_if_unblocked(project_id)
        return result

    @app.post("/api/projects/{project_id}/commands/accept-assumption")
    def accept_assumption(project_id: str, payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        result = to_primitive(
            command_service.accept_assumption(
                project_id,
                clarification_id=_required_str(payload, "clarification_id"),
            )
        )
        _autoresume_workflow_if_unblocked(project_id)
        return result

    @app.post("/api/projects/{project_id}/commands/set-clarification-mode")
    def set_clarification_mode(project_id: str, payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        mode = _required_str(payload, "mode")
        if mode not in {"autopilot", "balanced", "control", "expert"}:
            raise PovGeneratorError("Режим уточнений должен быть одним из: autopilot, balanced, control, expert.")
        return to_primitive(command_service.set_clarification_mode(project_id, mode=mode))

    @app.post("/api/projects/{project_id}/commands/set-methodology")
    def set_methodology(project_id: str, payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        pack_ref = _required_str(payload, "pack_ref")
        return to_primitive(command_service.set_methodology(project_id, pack_ref=pack_ref))

    # ---- W5.1: defer / reopen / events / next ---------------------------

    @app.post("/api/projects/{project_id}/commands/defer-clarification")
    def defer_clarification(project_id: str, payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        workspace_ref = catalog.resolve_workspace(project_id)
        request_id = _required_str(payload, "clarification_id")
        reason = _optional_str(payload, "reason")
        return to_primitive(
            clarification_service.defer_clarification(
                workspace_ref.workspace, request_id=request_id, reason=reason,
            )
        )

    @app.post("/api/projects/{project_id}/commands/reopen-clarification")
    def reopen_clarification(project_id: str, payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        workspace_ref = catalog.resolve_workspace(project_id)
        request_id = _required_str(payload, "clarification_id")
        return to_primitive(
            clarification_service.reopen_clarification(workspace_ref.workspace, request_id=request_id)
        )

    @app.get("/api/projects/{project_id}/clarifications/{clarification_id}/events")
    def clarification_events(project_id: str, clarification_id: str) -> Any:
        workspace_ref = catalog.resolve_workspace(project_id)
        return to_primitive(
            clarification_service.list_events(workspace_ref.workspace, clarification_id)
        )

    @app.get("/api/projects/{project_id}/clarifications/next")
    def clarification_next(project_id: str, after_id: str | None = None) -> Any:
        """Возвращает следующий открытый вопрос (по приоритету), отличный
        от `after_id`. Это flow-навигация UI wizard'а после ответа."""
        workspace_ref = catalog.resolve_workspace(project_id)
        opens = [
            req for req in runtime.list_clarification_requests(
                workspace_ref.workspace, statuses=("open",),
            )
            if req.request_id != after_id
        ]
        # Сортируем по priority desc, потом по created_at asc — старые более
        # приоритетные сверху.
        priority_rank = {"critical": 3, "high": 2, "medium": 1, "low": 0}
        opens.sort(
            key=lambda r: (-priority_rank.get(r.priority, 0), r.created_at)
        )
        return to_primitive(opens[0]) if opens else None

    @app.websocket("/ws/projects/{project_id}")
    async def project_updates(websocket: WebSocket, project_id: str) -> None:
        await websocket.accept()
        raw_projections = websocket.query_params.get("projections")
        projections = (
            tuple(name.strip() for name in raw_projections.split(",") if name.strip())
            if raw_projections
            else (
                "shell",
                "task_graph",
                "situation",
                "timeline",
                "artifacts",
                "clarifications",
                "review",
                "state",
                # Aggregated L1 / L2 projections (W2 UI). realtime_token tracks
                # the workspace as a whole, so any change broadcasts these too,
                # which is exactly what L1 Mission Control needs.
                "overview",
                "methodology",
            )
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


def main(argv: list[str] | None = None) -> None:
    """Cross-platform entry point for the API server.

    Configurable via CLI flags or environment variables (``POV_HOST``,
    ``POV_PORT``, ``POV_RELOAD``, ``POV_WORKERS``). Examples::

        povgen-api                          # 127.0.0.1:8788
        povgen-api --port 9000              # custom port
        povgen-api --reload                 # hot-reload for development
        POV_PORT=9000 povgen-api            # env-driven
    """
    import argparse
    import os

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="povgen-api",
        description="Run the PoV Generator FastAPI server.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("POV_HOST", "127.0.0.1"),
        help="Bind address (default: 127.0.0.1, env: POV_HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("POV_PORT", "8788")),
        help="Bind port (default: 8788, env: POV_PORT).",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        default=os.environ.get("POV_RELOAD", "").lower() in {"1", "true", "yes"},
        help="Enable code reload on file changes (development; env: POV_RELOAD).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("POV_WORKERS", "1")),
        help="Number of worker processes (production; ignored with --reload).",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("POV_LOG_LEVEL", "info"),
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="Uvicorn log level (default: info, env: POV_LOG_LEVEL).",
    )
    args = parser.parse_args(argv)

    # Both --reload and --workers >1 require an import string + factory=True
    # so uvicorn can re-import the app per worker / per file change.
    if args.reload or args.workers > 1:
        uvicorn.run(
            "pov_generator.interfaces.api:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=args.reload,
            workers=args.workers if args.workers > 1 and not args.reload else None,
            log_level=args.log_level,
        )
        return

    repo_root = Path(__file__).resolve().parents[3]
    uvicorn.run(
        create_app(repo_root=repo_root, runtime_root=repo_root / "runtime"),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
