from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from ..application.attachment_service import AttachmentService
from ..application.checkpoint_service import CheckpointService
from ..application.context_service import ContextService
from ..application.decision_extraction_service import DecisionExtractionService
from ..application.decision_identification_service import DecisionIdentificationService
from ..application.domain_pack_selection_service import DomainPackSelectionService
from ..application.execution_service import ExecutionService
from ..application.harness_execution_service import HarnessExecutionService
from ..application.harness_onboarding_service import HarnessOnboardingService
from ..application.harness_settings_service import HarnessSettingsService
from ..application.parallel_scheduling import (
    max_concurrency_for as default_max_concurrency_for,
)
from ..application.pdf_export import render_artifact_pdf, render_decisions_pdf
from ..application.planning_service import PlanningService
from ..application.project_lock import ensure_project_unlocked
from ..application.project_registry import ProjectRegistryResolver
from ..application.project_service import ProjectService
from ..application.provider_settings_service import (
    PURPOSE_LABELS_FOR_UI,
    ProviderSettingsService,
)
from ..application.registry_service import RegistryService
from ..application.rollback_coordinator import RollbackCoordinator
from ..application.rollback_service import RollbackService
from ..application.validation_service import ValidationService
from ..application.workflow_runner_service import WorkflowRunnerService
from ..application.workflow_service import WorkflowService
from ..application.workspace_catalog import WorkspaceCatalog
from ..application.workspace_command_service import WorkspaceCommandService
from ..application.workspace_query_service import WorkspaceQueryService
from ..common.env import load_repo_env
from ..common.errors import NotFoundError, PovGeneratorError, ValidationError
from ..common.logging import bind, configure_logging, get_logger, new_request_id
from ..common.serialization import to_primitive, utc_now_iso
from ..domain.llm_settings import ALL_PURPOSES
from ..infrastructure.filesystem_registry import (
    CachingRegistryLoader,
    FilesystemRegistryLoader,
)
from ..infrastructure.harness import (
    ADAPTER_CAPABILITIES as _HARNESS_ADAPTER_CAPABILITIES,
)
from ..infrastructure.harness import DockerSandboxRuntime, HarnessProviderRegistry
from ..infrastructure.harness.images import DockerImagePreparer
from ..infrastructure.harness_settings_store import HarnessSettingsStore
from ..infrastructure.llm import LLMProviderRegistry
from ..infrastructure.llm_settings_store import SqliteSettingsStore
from ..infrastructure.sqlite_runtime import SqliteRuntime


def create_app(
    *,
    repo_root: Path | None = None,
    runtime_root: Path | None = None,
    websocket_poll_interval: float = 0.75,
) -> FastAPI:
    resolved_repo_root = repo_root or Path(__file__).resolve().parents[3]
    load_repo_env(resolved_repo_root)
    configure_logging()
    log = get_logger("api")
    http_log = get_logger("http")
    app = FastAPI(title="PoV Generator Operator API", version="0.1.0")

    # Единое HTTP-логирование (вместо uvicorn.access). На каждый запрос —
    # request_id (трассировка сквозь все нижележащие логи), метод, путь,
    # статус, длительность. Политика уровней против спама:
    #   * GET (чтения/поллинг) → DEBUG — не зашумляем INFO;
    #   * мутации (POST/DELETE/...) → INFO — это действия пользователя;
    #   * 4xx → WARNING, 5xx/исключение → ERROR;
    #   * медленный ответ (> SLOW_MS) → WARNING независимо от метода.
    _SLOW_MS = 2000

    @app.middleware("http")
    async def _logging_middleware(request, call_next):  # type: ignore[no-untyped-def]
        request_id = new_request_id()
        method = request.method
        path = request.url.path
        start = time.perf_counter()
        # method/path/status уже в тексте сообщения — отдельными полями их не
        # дублируем (это и был «шум»). В pretty остаётся: «POST /path → 200 — 12мс».
        with bind(request_id=request_id):
            try:
                response = await call_next(request)
            except Exception:
                dur = round((time.perf_counter() - start) * 1000)
                http_log.error(f"{method} {path} → необработанное исключение", duration_ms=dur, exc_info=True)
                raise
            dur = round((time.perf_counter() - start) * 1000)
            status = response.status_code
            msg = f"{method} {path} → {status}"
            if status >= 500:
                http_log.error(msg, duration_ms=dur, exc_info=False)
            elif status >= 400:
                http_log.warning(msg, duration_ms=dur)
            elif dur > _SLOW_MS:
                http_log.warning(f"{msg} (медленно)", duration_ms=dur)
            elif method == "GET":
                http_log.debug(msg, duration_ms=dur)
            else:
                http_log.info(msg, duration_ms=dur)
            response.headers["X-Request-ID"] = request_id
            return response

    resolved_runtime_root = runtime_root or (resolved_repo_root / "runtime")
    ui_dist_root = resolved_repo_root / "ui" / "workspace" / "dist"

    # Кеширующий декоратор: реестр (73 YAML) парсится один раз и
    # переиспользуется, пока файлы не изменятся (инвалидация по mtime+size).
    # Снимает повторный парсинг на каждом запросе/поллинге проекций.
    registry_service = RegistryService(
        CachingRegistryLoader(FilesystemRegistryLoader(resolved_repo_root / "templates"))
    )
    runtime = SqliteRuntime()
    # Закрепление графа за проектом: запуск/просмотр идут на снимке реестра,
    # снятом при первом обращении к проекту. Правки templates/ не ломают
    # прошлые проекты (их можно смотреть и перезапускать на исходном графе).
    registry_resolver = ProjectRegistryResolver(runtime, resolved_repo_root / "templates")
    # Persistence слой настроек LLM-провайдеров (system-wide, не per-workspace).
    settings_store = SqliteSettingsStore(resolved_runtime_root)
    # Registry — единственное место в коде, где живёт switch по имени
    # провайдера. Привязан к settings_store, чтобы resolve_for_purpose работал.
    llm_registry = LLMProviderRegistry(settings_store=settings_store)
    # Сервис управления настройками: CRUD + test_connection / test_model +
    # bootstrap из env.
    provider_settings_service = ProviderSettingsService(settings_store, llm_registry=llm_registry)
    # Первичный bootstrap: если БД пуста и в env есть кредиты — auto-import.
    # Идемпотентно — при наличии connections ничего не делает.
    try:
        provider_settings_service.ensure_default_settings()
    except Exception:  # noqa: BLE001
        # Не критично: settings можно настроить через UI позже.
        pass

    # Sync known-models: добавляем routings для моделей, которые появились
    # в новом релизе (KNOWN_MODELS_BY_PROVIDER пополнили), но в старых
    # connections их ещё нет. Например: opus 4.7 → автоматически
    # появится в каталоге без ручных действий админа.
    try:
        provider_settings_service.sync_all_connections()
    except Exception:  # noqa: BLE001
        pass

    # v3.0: sync новых purpose-assignments. При добавлении нового purpose
    # в RECOMMENDED_BY_PURPOSE (например, decision_planning) — назначаем
    # рекомендуемую модель для существующих пользователей. Без этого
    # пользователь видит «не назначено» для нового сценария.
    try:
        provider_settings_service.sync_missing_purpose_assignments()
    except Exception:  # noqa: BLE001
        pass

    # Ф7d: активное harness-подключение (system-wide, тот же settings.db, своя
    # таблица, без секретов). Резолвер передаётся реестру ленивым загрузчиком —
    # смена адаптера из UI применяется без перезапуска.
    harness_settings_store = HarnessSettingsStore(resolved_runtime_root)
    harness_settings_service = HarnessSettingsService(harness_settings_store)
    harness_registry = HarnessProviderRegistry(
        connection_loader=harness_settings_service.resolve_runtime_connection
    )
    harness_execution_service = HarnessExecutionService(harness_registry)

    checkpoint_service = CheckpointService(runtime)
    decision_identification_service = DecisionIdentificationService(llm_registry=llm_registry)
    # v3.10 (идея А): сервис больше не вызывает LLM — он персистит решения,
    # которые модель вернула в ответе генерации. LLM-зависимость не нужна.
    decision_extraction_service = DecisionExtractionService(runtime)
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)
    context_service = ContextService(runtime)
    execution_service = ExecutionService(
        runtime,
        context_service,
        llm_registry=llm_registry,
        harness_service=harness_execution_service,
        decision_identification_service=decision_identification_service,
        decision_extraction_service=decision_extraction_service,
        checkpoint_service=checkpoint_service,
    )
    validation_service = ValidationService(runtime, checkpoint_service=checkpoint_service)
    workflow_service = WorkflowService(runtime, planning_service, execution_service, validation_service)

    # Резолвер параллельности: берём per-provider настройку из UI
    # (ProviderConnection.extras["max_concurrency"]); если не задана — provider-
    # aware дефолт. provider=None (резолв по purpose) → определяем провайдера
    # execution-цели и читаем его коннекшн. Best-effort: любой сбой → дефолт.
    _PROVIDER_TO_CONN_TYPE = {
        "claude_sdk": "anthropic",
        "claude_subscription": "claude_cli",
        "openrouter": "openrouter",
    }

    def _resolve_max_concurrency(provider: str | None) -> int:
        effective = provider
        try:
            if effective is None:
                try:
                    effective = llm_registry.resolve_for_purpose("execution").name
                except Exception:  # noqa: BLE001
                    effective = None
            conn_type = _PROVIDER_TO_CONN_TYPE.get(effective or "")
            if conn_type:
                for connection in provider_settings_service.list_connections():
                    if connection.provider_type == conn_type:
                        raw = connection.extras.get("max_concurrency")
                        if raw:
                            return max(1, int(raw))
                        break
        except Exception:  # noqa: BLE001
            pass
        return default_max_concurrency_for(effective)

    workflow_runner_service = WorkflowRunnerService(
        runtime,
        registry_service,
        workflow_service,
        planning_service,
        concurrency_resolver=_resolve_max_concurrency,
        registry_resolver=registry_resolver,
    )
    # Ролбек шага: чистый движок (RollbackService) + координатор
    # конкуррентности (замок проекта + авто-отмена активного прогона).
    rollback_service = RollbackService(runtime)
    rollback_coordinator = RollbackCoordinator(
        runtime, workflow_runner_service, rollback_service
    )
    catalog = WorkspaceCatalog(resolved_runtime_root, runtime)
    attachment_service = AttachmentService(runtime)
    query_service = WorkspaceQueryService(
        catalog, registry_service, runtime, planning_service, registry_resolver
    )
    domain_pack_selection_service = DomainPackSelectionService(llm_registry=llm_registry)
    command_service = WorkspaceCommandService(
        catalog,
        registry_service,
        project_service,
        planning_service,
        workflow_service,
        domain_pack_selection_service,
        checkpoint_service,
    )
    # Онбординг harness-агентов (Ф4): Docker-песочница + подготовка образов.
    # Мягко деградируют без Docker (статус «недоступен»); импорт docker — ленивый.
    harness_onboarding = HarnessOnboardingService(
        DockerSandboxRuntime(),
        DockerImagePreparer(),
    )

    app.state.query_service = query_service
    app.state.command_service = command_service
    app.state.provider_settings_service = provider_settings_service
    app.state.checkpoint_service = checkpoint_service
    app.state.llm_registry = llm_registry
    # Тот же экземпляр, что исполняет узлы: живой статус harness-рантайма (Ф6).
    app.state.execution_service = execution_service
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
    _recovered_runs = 0
    _recovered_tasks = 0
    try:
        for workspace_ref in catalog.list_workspaces():
            ws = workspace_ref.workspace
            # 1. Orphan workflow_runs
            try:
                for run in runtime.list_workflow_runs(ws, project_id=workspace_ref.project_id, limit=50):
                    if run.status in {"pending", "running"}:
                        _recovered_runs += 1
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
            # 2. Orphan tasks в статусе in_progress: процесс прервал их на лету.
            # Нормализация статусов: прерванная задача — это НЕ ошибка, её надо
            # просто переисполнить. cancel возвращает её в ready (как обычная
            # отмена), и следующий прогон продолжит ровно с неё. Статус `failed`
            # остаётся только для настоящих ошибок исполнения.
            try:
                for task in runtime.list_tasks(ws):
                    if task.status == "in_progress":
                        try:
                            runtime.transition_task(ws, task.task_id, "cancel")
                            _recovered_tasks += 1
                        except Exception:
                            # state-machine может не допускать transition
                            # для каких-то редких статусов — пропускаем.
                            pass
            except Exception:
                pass
    except Exception:
        # Recovery — best-effort. Сбой здесь не должен мешать старту API.
        pass
    if _recovered_runs or _recovered_tasks:
        log.warning(
            "восстановление после рестарта: остановлены зомби прошлого процесса",
            runs=_recovered_runs,
            tasks=_recovered_tasks,
        )
    # ---- end recovery ----------------------------------------------------

    @app.exception_handler(PovGeneratorError)
    async def pov_error_handler(_, exc: PovGeneratorError):
        return JSONResponse(status_code=409, content={"error": str(exc)})

    # ----- Harness-агенты: онбординг/готовность (Ф4) -----------------------
    #
    # Видимая подготовка вместо «тихих» долгих операций: готовность Docker,
    # скачивание образа с прогрессом (фоново), самопроверка цепочки, рекомендации
    # по мощности. Без Docker всё мягко деградирует (status.ready=false).

    @app.get("/api/harness/status")
    def harness_status() -> Any:
        return to_primitive(harness_onboarding.readiness())

    @app.get("/api/harness/runtime")
    def harness_runtime() -> Any:
        # Живой снимок «машинного отделения» (Ф6): слоты класса конкуррентности,
        # очередь ожидания, накопленный расход и лимиты прогона. Отдельно от
        # /status (готовность Docker/образа) — это разные сущности: подготовка
        # vs. текущая загрузка.
        return to_primitive(app.state.execution_service.harness_runtime_status())

    @app.get("/api/harness/adapters")
    def harness_adapters() -> Any:
        # Ф7c: матрица возможностей адаптеров (для выбора в настройках) +
        # текущий выбранный исполнитель. Характеристики инструмента, не оценки.
        active = app.state.execution_service.harness_runtime_status().provider_name
        return {
            "active": active,
            "capabilities": to_primitive(_HARNESS_ADAPTER_CAPABILITIES),
        }

    @app.get("/api/harness/llm")
    def harness_llm() -> Any:
        # Связка LLM↔агент: какое настроенное LLM-подключение проекта использует
        # агент (креды + модель). Для панели «Настройки окружения».
        return app.state.execution_service.harness_llm_status()

    @app.get("/api/harness/connection")
    def harness_connection() -> Any:
        # Ф7d: активное harness-подключение (нечувствительный выбор исполнителя).
        return to_primitive(harness_settings_service.get_connection())

    @app.put("/api/harness/connection")
    def harness_connection_set(payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        timeout_raw = payload.get("default_timeout_s")
        timeout = int(timeout_raw) if isinstance(timeout_raw, (int, float)) else None
        saved = harness_settings_service.set_connection(
            provider=_required_str(payload, "provider"),
            image=_optional_str(payload, "image"),
            model=_optional_str(payload, "model"),
            command=_optional_str(payload, "command"),
            default_timeout_s=timeout,
            engine=_optional_str(payload, "engine") or "docker",
            host_security=_optional_str(payload, "host_security") or "restricted",
            network=_optional_str(payload, "network") or "none",
        )
        return to_primitive(saved)

    @app.post("/api/harness/prepare")
    def harness_prepare(payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        image = _optional_str(payload, "image")
        harness_onboarding.start_prepare(image)
        return {
            "status": "accepted",
            "pull": to_primitive(harness_onboarding.pull_progress(image)),
        }

    @app.get("/api/harness/image-progress")
    def harness_image_progress(image: str | None = None) -> Any:
        # Прогресс сборки/тяги конкретного образа (для кнопки «Собрать образ»).
        return to_primitive(harness_onboarding.pull_progress(image))

    @app.get("/api/harness/image-status")
    def harness_image_status(image: str) -> Any:
        # Готов ли образ + прогресс текущей сборки — для естественного шага
        # подготовки в настройках исполнителя.
        return {
            "image": image,
            "ready": harness_onboarding.image_ready(image),
            "progress": to_primitive(harness_onboarding.pull_progress(image)),
        }

    @app.post("/api/harness/self-test")
    def harness_self_test(payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        image = _optional_str(payload, "image")
        return to_primitive(harness_onboarding.self_test(image=image))

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "time": utc_now_iso(),
            "runtime_root": str(resolved_runtime_root),
        }

    # ----- Settings: LLM-провайдеры и модели -------------------------------
    #
    # Управление через UI: см. /settings в фронте.
    # Один источник истины — `SqliteSettingsStore` (`<runtime>/settings.db`).
    # API-сервис `ProviderSettingsService` обёртывает store и добавляет
    # автозаполнение routings + реальные test-вызовы.

    @app.get("/api/settings/purposes")
    def settings_list_purposes() -> Any:
        """Каталог сценариев (purpose) для UI Default Models tab."""
        return [
            {"id": purpose, "label": PURPOSE_LABELS_FOR_UI.get(purpose, purpose)}
            for purpose in ALL_PURPOSES
        ]

    @app.get("/api/settings/app")
    def settings_get_app() -> Any:
        """Общие настройки приложения (раздел «Общие»). Сейчас: режим «дебаг»,
        который открывает в окне артефакта поля Проверки/Provenance/JSON/Контекст."""
        return {"debug": (settings_store.get_app_setting("debug") == "1")}

    @app.put("/api/settings/app")
    def settings_update_app(payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        if "debug" in payload:
            settings_store.set_app_setting("debug", "1" if bool(payload.get("debug")) else "0")
        return {"debug": (settings_store.get_app_setting("debug") == "1")}

    @app.get("/api/settings/providers")
    def settings_list_providers() -> Any:
        return [_provider_connection_to_dict(c) for c in provider_settings_service.list_connections()]

    @app.post("/api/settings/providers")
    def settings_create_provider(payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        try:
            connection = provider_settings_service.add_connection(
                provider_type=_required_str(payload, "provider_type"),  # type: ignore[arg-type]
                display_name=_required_str(payload, "display_name"),
                api_key=_optional_str(payload, "api_key"),
                extras=_extract_extras(payload.get("extras")),
            )
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _provider_connection_to_dict(connection)

    @app.put("/api/settings/providers/{connection_id}")
    def settings_update_provider(
        connection_id: str, payload: dict[str, object] = Body(default_factory=dict)
    ) -> Any:
        try:
            updated = provider_settings_service.update_connection(
                connection_id,
                display_name=_optional_str(payload, "display_name"),
                api_key=_optional_str_keep_empty(payload, "api_key"),
                extras=_extract_extras(payload.get("extras")) if "extras" in payload else None,
            )
        except ValidationError as exc:
            raise HTTPException(status_code=404 if "не найден" in str(exc) else 400, detail=str(exc))
        return _provider_connection_to_dict(updated)

    @app.delete("/api/settings/providers/{connection_id}")
    def settings_delete_provider(connection_id: str) -> Any:
        try:
            provider_settings_service.delete_connection(connection_id)
        except ValidationError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"status": "deleted", "connection_id": connection_id}

    @app.post("/api/settings/providers/{connection_id}/test")
    def settings_test_provider(
        connection_id: str, payload: dict[str, object] = Body(default_factory=dict)
    ) -> Any:
        test_model_name = _optional_str(payload, "model")
        result = provider_settings_service.test_connection(connection_id, test_model=test_model_name)
        return _test_result_to_dict(result)

    @app.post("/api/settings/providers/{connection_id}/sync-models")
    def settings_sync_known_models(connection_id: str) -> Any:
        """Добавить отсутствующие routings для known-моделей провайдера.

        Используется когда в новом релизе пополнили KNOWN_MODELS_BY_PROVIDER
        (например, opus 4.7), а connection был создан до этого.
        """
        try:
            added = provider_settings_service.sync_known_routings(connection_id)
        except ValidationError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {
            "connection_id": connection_id,
            "added_count": len(added),
            "added_models": [r.model_name for r in added],
        }

    @app.get("/api/settings/models")
    def settings_list_models() -> Any:
        return list(provider_settings_service.list_models())

    @app.post("/api/settings/models")
    def settings_add_model(payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        try:
            routing = provider_settings_service.add_custom_model(
                connection_id=_required_str(payload, "connection_id"),
                model_name=_required_str(payload, "model_name"),
                priority=int(payload.get("priority", 100) or 100),
            )
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "routing_id": routing.routing_id,
            "connection_id": routing.connection_id,
            "model_name": routing.model_name,
            "priority": routing.priority,
            "enabled": routing.enabled,
        }

    @app.put("/api/settings/routings/{routing_id}")
    def settings_update_routing(
        routing_id: str, payload: dict[str, object] = Body(default_factory=dict)
    ) -> Any:
        try:
            routing = provider_settings_service.update_routing(
                routing_id,
                priority=int(payload["priority"]) if "priority" in payload else None,
                enabled=bool(payload["enabled"]) if "enabled" in payload else None,
            )
        except ValidationError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {
            "routing_id": routing.routing_id,
            "connection_id": routing.connection_id,
            "model_name": routing.model_name,
            "priority": routing.priority,
            "enabled": routing.enabled,
        }

    @app.delete("/api/settings/routings/{routing_id}")
    def settings_delete_routing(routing_id: str) -> Any:
        try:
            provider_settings_service.delete_routing(routing_id)
        except ValidationError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"status": "deleted", "routing_id": routing_id}

    @app.post("/api/settings/models/{model_name:path}/test")
    def settings_test_model(model_name: str) -> Any:
        result = provider_settings_service.test_model(model_name=model_name)
        return _test_result_to_dict(result)

    @app.put("/api/settings/models/context-limit")
    def settings_set_context_limit(payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        """Задать или сбросить лимит контекста (токены) для модели.

        ``model_name`` идёт в теле (а не в пути) — имена моделей содержат «/».
        ``context_limit_tokens`` отсутствует/null → сброс к дефолту по модели.
        """
        model_name = _required_str(payload, "model_name")
        raw = payload.get("context_limit_tokens")
        try:
            if raw is None or raw == "":
                provider_settings_service.reset_context_limit(model_name)
            else:
                provider_settings_service.set_context_limit(
                    model_name=model_name, context_limit_tokens=int(raw)
                )
        except (ValidationError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        for entry in provider_settings_service.list_models():
            if entry["model_name"] == model_name:
                return entry
        return {"model_name": model_name}

    @app.get("/api/settings/assignments")
    def settings_list_assignments() -> Any:
        return [
            {"purpose": a.purpose, "model_name": a.model_name}
            for a in provider_settings_service.list_assignments()
        ]

    @app.put("/api/settings/assignments")
    def settings_set_assignment(payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        try:
            assignment = provider_settings_service.set_assignment(
                purpose=_required_str(payload, "purpose"),
                model_name=_required_str(payload, "model_name"),
            )
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"purpose": assignment.purpose, "model_name": assignment.model_name}

    @app.post("/api/settings/assignments/reset-to-recommended")
    def settings_reset_assignments() -> Any:
        applied = provider_settings_service.reset_assignments_to_recommended()
        return [{"purpose": a.purpose, "model_name": a.model_name} for a in applied]

    @app.get("/api/settings/diagnostics")
    def settings_diagnostics() -> Any:
        """Что реально пойдёт в LLM-вызов при текущих настройках.

        Для каждого purpose: имя модели + через какой connection пойдёт
        + список fallback'ов. Используется в UI как наглядное
        подтверждение, что переключение модели в Assignments действительно
        работает.
        """
        return list(provider_settings_service.diagnose_resolution())

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

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str) -> Any:
        from ..common.errors import NotFoundError

        # 404, если проекта нет — резолв до любых side-effect'ов.
        try:
            workspace_ref = catalog.resolve_workspace(project_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        workspace = workspace_ref.workspace
        # 1. Остановить активный run и ДОЖДАТЬСЯ остановки daemon-потока.
        #    Иначе in-flight запись через `_connect` пере-создаст папку
        #    поверх rmtree и проект «воскреснет» частично.
        active = workflow_runner_service.latest_active_run(workspace, project_id)
        if active is not None:
            log.warning("удаление проекта: останавливаю активный прогон")
            workflow_runner_service.cancel_run(workspace, active.run_id)
            workflow_runner_service.wait_until_idle(active.run_id, timeout_s=15.0)
        # 2. Удалить workspace целиком. UI инвалидирует список проектов;
        #    realtime_token (mtime-based) тоже сдвинется и разошлёт
        #    projection_changed подписчикам.
        catalog.delete_workspace(project_id)
        log.info(f"проект удалён ({project_id[:8]})")
        return {"status": "deleted", "project_id": project_id}

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

    @app.get("/api/projects/{project_id}/stages")
    def project_stages(project_id: str) -> Any:
        return to_primitive(query_service.project_stages(project_id))

    @app.get("/api/projects/{project_id}/requisites")
    def project_requisites(project_id: str) -> Any:
        return to_primitive(query_service.project_requisites(project_id))

    @app.get("/api/projects/{project_id}/capability-gaps")
    def project_capability_gaps(project_id: str) -> Any:
        return to_primitive(query_service.project_capability_gaps(project_id))

    @app.post("/api/projects/{project_id}/requisites/provide")
    def project_requisite_provide(
        project_id: str, payload: dict[str, object] = Body(default_factory=dict)
    ) -> Any:
        """Разрешить реквизит (реквизиты v2): предоставить данные ИЛИ обойти.

        Body: ``{"key", "mode"?, "value"?, "attachment_id"?, "note"?}``. ``mode``:
        данные — ``value`` | ``file`` | ``reference`` (по умолчанию reference);
        обход — ``assumption`` («допущение»: рабочий дефолт), ``deferred``
        («позже») или ``not_applicable`` («неприменимо»). Любой режим снимает
        гранулярный блок задачи-потребителя. Секреты не храним: для credential
        принудительно reference (без поля значения); значение несут только value/
        assumption. Возвращает обновлённый список.
        """
        key = str(payload.get("key") or "").strip()
        if not key:
            raise HTTPException(status_code=400, detail="Не указан реквизит (key).")
        mode = str(payload.get("mode") or "reference").strip() or "reference"
        # mock (#3): «поставить заглушку» — система сгенерирует тестовые данные
        # для сегмента. Положительное разрешение без данных (снимает блок узла),
        # узлу-агенту в бриф попадает указание сгенерировать mock.
        allowed_modes = {"value", "file", "reference", "assumption", "deferred", "not_applicable", "mock"}
        if mode not in allowed_modes:
            raise HTTPException(status_code=400, detail=f"Неизвестный режим: {mode}.")
        note = str(payload.get("note") or "")
        attachment_id = str(payload.get("attachment_id") or "")
        # Значение несут только value/assumption; остальные режимы — без значения
        # (защита от утечки секрета; команда дополнительно принудит reference для
        # credential).
        value = str(payload.get("value") or "") if mode in {"value", "assumption"} else ""
        command_service.provide_requisite(
            project_id,
            key=key,
            mode=mode,
            value=value,
            attachment_id=attachment_id,
            note=note,
        )
        return to_primitive(query_service.project_requisites(project_id))

    @app.post("/api/projects/{project_id}/requisites/unprovide")
    def project_requisite_unprovide(
        project_id: str, payload: dict[str, object] = Body(default_factory=dict)
    ) -> Any:
        """Снять предоставление реквизита (реквизиты v7, un-provide).

        Body: ``{"key": "<ключ реквизита>"}``. Удаляет запись и связанное
        value/assumption-положение; блокирующий реквизит снова держит свою
        задачу-потребителя. Возвращает обновлённый список.
        """
        key = str(payload.get("key") or "").strip()
        if not key:
            raise HTTPException(status_code=400, detail="Не указан реквизит (key).")
        command_service.unprovide_requisite(project_id, key=key)
        return to_primitive(query_service.project_requisites(project_id))

    @app.get("/api/projects/{project_id}/task-graph")
    def project_task_graph(project_id: str) -> Any:
        return to_primitive(query_service.project_task_graph(project_id))

    @app.get("/api/projects/{project_id}/objectives/task-graph")
    def project_objective_task_graph(project_id: str, ref: str) -> Any:
        """Граф задач конкретного гейта (objective) проекта — для подвкладок
        графа по гейтам (Ф1). ``ref`` передаётся query-параметром, так как
        содержит '@'. Активный гейт → живой граф; завершённый → сохранённые
        задачи; ещё не запущенный → статический скелет (read-only)."""
        from ..common.errors import NotFoundError

        try:
            return to_primitive(
                query_service.project_objective_task_graph(project_id, ref)
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.get("/api/projects/{project_id}/tasks/{task_id}/gate")
    def task_gate(project_id: str, task_id: str) -> Any:
        """Гейт (objective_ref), которому принадлежит задача. Нужен дип-линку
        «открыть задачу на графе», чтобы выбрать правильную подвкладку гейта,
        а не открывать задачу в графе активного гейта (Ф1)."""
        workspace = catalog.resolve_workspace(project_id).workspace
        try:
            task = runtime.get_task(workspace, task_id)
        except Exception:
            raise HTTPException(status_code=404, detail="Задача не найдена.")
        return {"objective_ref": task.objective_ref}

    @app.get("/api/projects/{project_id}/situation")
    def project_situation(project_id: str) -> Any:
        return to_primitive(query_service.project_situation(project_id))

    @app.get("/api/projects/{project_id}/timeline")
    def project_timeline(project_id: str, after_sequence: int = 0) -> Any:
        return to_primitive(query_service.project_timeline(project_id, after_sequence=after_sequence))

    # --- v3.0 — Decision ledger ---------------------------------------------

    @app.get("/api/projects/{project_id}/decisions")
    def project_decisions(
        project_id: str,
        level: str | None = None,
        status: str | None = None,
        include_details: bool = True,
    ) -> Any:
        """Реестр решений проекта.

        Опциональные query-параметры:
        - ``level``: business | architecture | detail
        - ``status``: proposed | accepted_default | user_overridden | deferred | locked_in | superseded
        - ``include_details``: false returns lightweight items; use detail
          endpoint for alternatives/rationale/description.

        Возвращает items + агрегаты по уровням и статусам (агрегаты
        считаются по всему реестру, не по отфильтрованному виду).
        """
        return to_primitive(
            query_service.project_decisions(
                project_id,
                level=level,
                status=status,
                include_details=include_details,
            )
        )

    @app.get("/api/projects/{project_id}/artifacts/{artifact_id}/decisions")
    def project_artifact_decisions(
        project_id: str,
        artifact_id: str,
        include_details: bool = True,
    ) -> Any:
        """Решения, принятые при сборке конкретного артефакта (v3.0).

        Связь — через ``Decision.affected_artifact_ids``. Используется в
        ArtifactDetailPage для отдельной вкладки «Решения».
        """
        return to_primitive(
            query_service.decisions_for_artifact(
                project_id,
                artifact_id,
                include_details=include_details,
            )
        )

    @app.get("/api/projects/{project_id}/decisions/export.pdf")
    def download_decisions_pdf(project_id: str) -> Response:
        """v3.5 — выгрузка реестра решений в виде PDF-таблицы.

        Берёт текущий реестр (без фильтров), сериализует в широкую таблицу
        markdown и прогоняет через общий PDF-pipeline. Landscape применяется
        автоматически если строки длинные (см. pdf_export._enhance_tables_in_html).
        """
        view = query_service.project_decisions(project_id)
        shell = query_service.project_shell(project_id)
        decisions_payload = to_primitive(view.items)
        pdf_bytes = render_decisions_pdf(
            decisions=decisions_payload,  # type: ignore[arg-type]
            project_name=shell.name or project_id,
            mode=view.mode,
        )
        filename = _safe_pdf_filename(
            f"Реестр решений — {shell.name or project_id}", project_id
        )
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": _content_disposition_header(filename),
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/projects/{project_id}/decisions/{decision_id}")
    def project_decision_detail(project_id: str, decision_id: str) -> Any:
        """Детали решения по id.

        Возвращает 404 при отсутствии (внутренний NotFoundError по
        дефолту даёт 409 через глобальный handler, но для GET по id 404
        семантически правильнее — повторяем паттерн download.pdf).
        """
        from ..common.errors import NotFoundError
        try:
            return to_primitive(query_service.decision_detail(project_id, decision_id))
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.post("/api/projects/{project_id}/decisions/{decision_id}/verify")
    def project_decision_verify(
        project_id: str, decision_id: str, body: dict[str, Any] | None = None
    ) -> Any:
        """v3.4 — пометить рискованное решение как «просмотрено и согласовано».

        Снимает индикатор `is_low_confidence` в UI без изменения самого
        решения (ни choice, ни alternatives). Это аудит-метка для случаев,
        когда дефолт LLM на самом деле адекватен, а низкая уверенность —
        артефакт общей неопределённости задачи.

        Body (optional):
            ``{"verified": true}`` — поставить метку (default).
            ``{"verified": false}`` — снять метку.

        Returns:
            Обновлённое решение в формате DecisionItemView.
        """
        from ..common.errors import NotFoundError

        verified = True if body is None else bool(body.get("verified", True))
        # Сначала валидируем принадлежность решения проекту (404 если нет).
        try:
            query_service.decision_detail(project_id, decision_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        workspace = app.state.query_service._load_context(project_id).workspace  # type: ignore[attr-defined]
        checkpoint_service.set_decision_verified(
            workspace,
            decision_id=decision_id,
            verified=verified,
        )
        return to_primitive(query_service.decision_detail(project_id, decision_id))

    # --- v3.0 — Checkpoint sessions -----------------------------------------

    @app.get("/api/projects/{project_id}/checkpoints")
    def project_checkpoints(project_id: str) -> Any:
        """Все checkpoint-сессии проекта + pending_count для UI-бейджа."""
        return to_primitive(query_service.project_checkpoints(project_id))

    @app.get("/api/projects/{project_id}/checkpoints/{session_id}")
    def project_checkpoint_detail(project_id: str, session_id: str) -> Any:
        """Развёрнутая сессия: метаданные + Decision-карточки.

        Returns 404 если сессии нет или она принадлежит другому проекту.
        """
        from ..common.errors import NotFoundError
        try:
            return to_primitive(query_service.checkpoint_session_detail(project_id, session_id))
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.post("/api/projects/{project_id}/checkpoints/{session_id}/answer")
    def project_checkpoint_answer(
        project_id: str, session_id: str, body: dict[str, Any]
    ) -> Any:
        """Применить ответы пользователя на checkpoint-сессию.

        Body:
            ``{ "answers": [{ "decision_id": "...", "kind": "accept_default" | "select_alternative" | "free_text" | "defer", "selected_option_id": "..." | None, "free_text": "..." | None }] }``

        Все ответы применяются атомарно. На решения сессии, по которым
        ответа не передано — применяется ``accept_default`` (массовое
        подтверждение оставшихся при закрытии сессии).

        Returns:
            Финализированная сессия (status=finalized) с обновлёнными
            Decision-объектами.
        """
        from ..common.errors import NotFoundError
        from ..domain.checkpoints import CheckpointAnswer

        # Проверяем, что сессия принадлежит проекту, до применения
        # (вызов нужен ради side-effect — бросает NotFoundError, если нет).
        try:
            query_service.checkpoint_session_detail(project_id, session_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

        raw_answers = body.get("answers") or []
        if not isinstance(raw_answers, list):
            raise HTTPException(status_code=400, detail="'answers' должен быть массивом")
        answers: list[CheckpointAnswer] = []
        for raw in raw_answers:
            if not isinstance(raw, dict):
                raise HTTPException(status_code=400, detail="каждый answer — объект")
            try:
                answers.append(
                    CheckpointAnswer(
                        decision_id=str(raw["decision_id"]),
                        kind=str(raw["kind"]),  # type: ignore[arg-type]
                        selected_option_id=(
                            str(raw["selected_option_id"])
                            if raw.get("selected_option_id") is not None
                            else None
                        ),
                        free_text=(
                            str(raw["free_text"])
                            if raw.get("free_text") is not None
                            else None
                        ),
                    )
                )
            except KeyError as exc:
                raise HTTPException(status_code=400, detail=f"answer без поля {exc}")

        workspace = app.state.query_service._load_context(project_id).workspace  # type: ignore[attr-defined]
        # Во время отката проект заморожен — мутации отклоняются (409).
        ensure_project_unlocked(runtime, workspace)
        checkpoint_service.submit_answers(
            workspace, session_id=session_id, answers=tuple(answers)
        )

        # v3.0 auto-continue: после финализации сессии задача переведена в
        # ready (см. CheckpointService.submit_answers). Сразу запускаем новый
        # workflow run, чтобы пользователь не нажимал «Run» вручную.
        #
        # Если active run уже есть — не запускаем повторный (он сам подхватит
        # ready-task на следующей итерации планировщика). Provider/model берём
        # из последнего запущенного run проекта (если нет — None = режим
        # «из настроек»).
        try:
            already_active = workflow_runner_service.latest_active_run(workspace, project_id)
            if already_active is None:
                runs = workflow_runner_service.list_runs(workspace, project_id=project_id, limit=1)
                last_run = runs[0] if runs else None
                workflow_runner_service.start_run_until_blocked(
                    workspace,
                    project_id,
                    provider=last_run.provider if last_run else None,
                    model=last_run.model if last_run else None,
                    max_steps=1000,
                )
        except Exception:  # noqa: BLE001
            # Не блокируем submit, если auto-continue не сработал — пользователь
            # сможет вручную нажать «Run». Логирование внутри runner'а.
            pass

        # Перечитываем финализированную сессию через query_service,
        # чтобы UI получил тот же view-формат, что и при GET
        return to_primitive(query_service.checkpoint_session_detail(project_id, session_id))

    @app.post("/api/projects/{project_id}/decisions/answer")
    def project_decisions_answer(project_id: str, body: dict[str, Any]) -> Any:
        """Ответить на ОТКРЫТЫЕ решения проекта скопом (единый экран решений).

        В параллельном режиме несколько шагов могут одновременно ждать
        решений. Пользователь видит все открытые решения единым списком
        (``GET /decisions?status=proposed``) и отвечает разом — без понятия
        «сессия». Тело то же, что у per-session answer, но decision_id'ы
        могут принадлежать разным сессиям:

            ``{ "answers": [{ "decision_id", "kind", "selected_option_id"?, "free_text"? }] }``

        Все затронутые pending-сессии финализируются (неотвеченные решения —
        accept_default), затем auto-continue запускает новый run.
        """
        from ..domain.checkpoints import CheckpointAnswer

        raw_answers = body.get("answers") or []
        if not isinstance(raw_answers, list):
            raise HTTPException(status_code=400, detail="'answers' должен быть массивом")
        answers: list[CheckpointAnswer] = []
        for raw in raw_answers:
            if not isinstance(raw, dict):
                raise HTTPException(status_code=400, detail="каждый answer — объект")
            try:
                answers.append(
                    CheckpointAnswer(
                        decision_id=str(raw["decision_id"]),
                        kind=str(raw["kind"]),  # type: ignore[arg-type]
                        selected_option_id=(
                            str(raw["selected_option_id"])
                            if raw.get("selected_option_id") is not None
                            else None
                        ),
                        free_text=(
                            str(raw["free_text"]) if raw.get("free_text") is not None else None
                        ),
                    )
                )
            except KeyError as exc:
                raise HTTPException(status_code=400, detail=f"answer без поля {exc}")

        workspace = app.state.query_service._load_context(project_id).workspace  # type: ignore[attr-defined]
        # Во время отката проект заморожен — мутации отклоняются (409).
        ensure_project_unlocked(runtime, workspace)
        checkpoint_service.submit_decision_answers(
            workspace, project_id=project_id, answers=tuple(answers)
        )

        # Auto-continue: задачи финализированных сессий переведены в ready;
        # запускаем новый run, если активного нет (тот же паттерн, что в
        # per-session answer).
        try:
            if workflow_runner_service.latest_active_run(workspace, project_id) is None:
                runs = workflow_runner_service.list_runs(workspace, project_id=project_id, limit=1)
                last_run = runs[0] if runs else None
                workflow_runner_service.start_run_until_blocked(
                    workspace,
                    project_id,
                    provider=last_run.provider if last_run else None,
                    model=last_run.model if last_run else None,
                    max_steps=1000,
                )
        except Exception:  # noqa: BLE001
            pass

        # Возвращаем обновлённый единый список открытых решений.
        return to_primitive(query_service.project_decisions(project_id, status="proposed"))

    @app.get("/api/projects/{project_id}/artifacts")
    def project_artifacts(project_id: str) -> Any:
        return to_primitive(query_service.project_artifacts(project_id))

    # Литеральный маршрут — ДО /{artifact_id}, иначе "archive" попадёт в него.
    @app.get("/api/projects/{project_id}/artifacts/archive")
    def project_archived_artifacts(project_id: str) -> Any:
        """Архив проекта: артефакты, заархивированные откатом, и заменённые
        более новой версией. Подраздел «Архив» во вкладке артефактов."""
        return to_primitive(query_service.project_archived_artifacts(project_id))

    @app.get("/api/projects/{project_id}/artifacts/{artifact_id}")
    def project_artifact_detail(project_id: str, artifact_id: str) -> Any:
        return to_primitive(query_service.artifact_detail(project_id, artifact_id))

    @app.get("/api/projects/{project_id}/artifacts/{artifact_id}/bundle/file")
    def project_artifact_bundle_file(project_id: str, artifact_id: str, path: str) -> Any:
        # #2: содержимое одного файла бандла (код) для просмотра в окне артефакта.
        try:
            return query_service.bundle_file_text(project_id, artifact_id, path)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/projects/{project_id}/artifacts/{artifact_id}/verify")
    def project_artifact_verify(
        project_id: str, artifact_id: str, body: dict[str, Any] | None = None
    ) -> Any:
        """Пометить низкоуверенный артефакт как «просмотрено и согласовано»
        (или снять метку). Снимает индикатор is_low_confidence в UI без
        изменения содержимого — зеркально /decisions/{id}/verify.

        Body (optional): ``{"verified": true}`` (default) | ``{"verified": false}``.
        Возвращает обновлённый ArtifactDetailView.
        """
        from ..common.errors import NotFoundError

        verified = True if body is None else bool(body.get("verified", True))
        # Валидируем принадлежность артефакта проекту (404 если нет).
        try:
            query_service.artifact_detail(project_id, artifact_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        workspace = query_service._load_context(project_id).workspace  # type: ignore[attr-defined]
        checkpoint_service.set_artifact_verified(
            workspace, artifact_id=artifact_id, verified=verified
        )
        log.info("артефакт подтверждён пользователем" if verified else "снята метка подтверждения артефакта")
        return to_primitive(query_service.artifact_detail(project_id, artifact_id))

    @app.post("/api/projects/{project_id}/artifacts/{artifact_id}/sign-off")
    def project_artifact_sign_off(
        project_id: str, artifact_id: str, body: dict[str, Any] | None = None
    ) -> Any:
        """Согласовать итоговый артефакт с заказчиком (sign-off) или снять
        согласование. Заменяет прежнее решение-согласование в реестре:
        прохождение human_approval-гейта считается по этой метке, и пока
        итоговый артефакт не согласован — переход на следующий этап закрыт.

        Body (optional): ``{"signed_off": true}`` (default) | ``{"signed_off": false}``.
        Возвращает обновлённый ArtifactDetailView.
        """
        from ..common.errors import NotFoundError

        signed_off = True if body is None else bool(body.get("signed_off", True))
        try:
            query_service.artifact_detail(project_id, artifact_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        workspace = query_service._load_context(project_id).workspace  # type: ignore[attr-defined]
        checkpoint_service.set_artifact_signed_off(
            workspace, artifact_id=artifact_id, signed_off=signed_off
        )
        log.info("артефакт согласован с заказчиком" if signed_off else "снято согласование артефакта")
        return to_primitive(query_service.artifact_detail(project_id, artifact_id))

    @app.get("/api/projects/{project_id}/artifacts/{artifact_id}/download.pdf")
    def download_artifact_pdf(project_id: str, artifact_id: str) -> Response:
        """Скачивание артефакта в формате PDF.

        Берёт уже отрендеренный markdown артефакта (см. render_markdown),
        прогоняет через markdown → HTML → PDF и отдаёт как `application/pdf`
        с `Content-Disposition: attachment`.
        """
        detail = query_service.artifact_detail(project_id, artifact_id)
        if not detail.markdown_content:
            raise HTTPException(
                status_code=404,
                detail="У артефакта нет markdown-представления для рендера в PDF.",
            )
        pdf_bytes = render_artifact_pdf(
            markdown_content=detail.markdown_content,
            title=detail.title or detail.artifact_role,
        )
        filename = _safe_pdf_filename(detail.title or detail.artifact_role, artifact_id)
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                # RFC 5987: ASCII fallback + UTF-8 percent-encoded variant
                # для имён с кириллицей (HTTP-заголовки — Latin-1 only).
                "Content-Disposition": _content_disposition_header(filename),
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/projects/{project_id}/attachments")
    def project_attachments(project_id: str) -> Any:
        return to_primitive(query_service.project_attachments(project_id))

    @app.post("/api/projects/{project_id}/attachments")
    async def upload_attachment(
        project_id: str,
        file: UploadFile = File(...),
        purpose: str = Form("input"),
    ) -> Any:
        """Загрузить файл проекта (multipart).

        ``purpose``: ``input`` — входной материал (по умолчанию; показывается во
        «Входных материалах»); ``requisite`` — файл, предоставленный в ответ на
        реквизит (отдельный бакет, в «Реквизиты»). Сохраняет со статусом
        ``pending`` и ставит извлечение текста в фон; отвечает быстро.
        """
        workspace = catalog.resolve_workspace(project_id).workspace
        # Размер ограничиваем при чтении потока, а не после полной материализации:
        # иначе гигантский upload занял бы всю память ещё до проверки лимита.
        max_bytes = attachment_service.max_file_bytes
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await file.read(1024 * 1024)  # 1 MiB
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"Файл превышает лимит размера ({max_bytes} байт).",
                )
            chunks.append(chunk)
        content = b"".join(chunks)
        record = attachment_service.upload(
            workspace,
            project_id,
            filename=file.filename or "file",
            content=content,
            mime_type=file.content_type,
            purpose="requisite" if purpose == "requisite" else "input",
        )
        return {
            "attachment_id": record.attachment_id,
            "original_filename": record.original_filename,
            "extraction_status": record.extraction_status,
        }

    @app.get("/api/projects/{project_id}/attachments/{attachment_id}/download")
    def download_attachment(
        project_id: str, attachment_id: str, inline: bool = False
    ) -> Response:
        """Отдать оригинал файла. ``?inline=1`` — для онлайн-просмотра во
        встроенном просмотрщике браузера (PDF в iframe); без него — скачивание."""
        workspace = catalog.resolve_workspace(project_id).workspace
        try:
            record = runtime.load_attachment(workspace, attachment_id)
            content = runtime.load_attachment_content(workspace, attachment_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(
            content=content,
            media_type=record.mime_type or "application/octet-stream",
            headers={
                "Content-Disposition": _content_disposition_header(
                    record.original_filename, inline=inline
                ),
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/projects/{project_id}/attachments/{attachment_id}/text")
    def attachment_text(project_id: str, attachment_id: str) -> Any:
        """Извлечённый текст вложения для онлайн-просмотра.

        Для форматов без браузерного рендера (например, .docx) текст —
        единственный способ показать содержимое прямо в интерфейсе. ``text``
        пуст, если извлечение не удалось/не поддержано.
        """
        workspace = catalog.resolve_workspace(project_id).workspace
        try:
            record = runtime.load_attachment(workspace, attachment_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        text = ""
        # extracted_text_ref — системный путь вида ``attachments/<id>.txt``
        # (не пользовательский ввод), traversal-риска нет.
        if record.extracted_text_ref:
            text_path = workspace / record.extracted_text_ref
            if text_path.exists():
                text = text_path.read_text(encoding="utf-8")
        return {
            "attachment_id": attachment_id,
            "extraction_status": record.extraction_status,
            "text": text,
        }

    @app.delete("/api/projects/{project_id}/attachments/{attachment_id}")
    def delete_attachment(project_id: str, attachment_id: str) -> Any:
        """Удалить вложение (только пока оно не использовано в контексте → иначе 409)."""
        workspace = catalog.resolve_workspace(project_id).workspace
        try:
            attachment_service.delete(workspace, attachment_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"status": "deleted", "attachment_id": attachment_id}

    @app.get("/api/projects/{project_id}/artifacts/{artifact_id}/download.md")
    def download_artifact_md(project_id: str, artifact_id: str) -> Response:
        """Скачивание артефакта в формате Markdown (готовый .md файл)."""
        try:
            detail = query_service.artifact_detail(project_id, artifact_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not detail.markdown_content:
            raise HTTPException(
                status_code=404,
                detail="У артефакта нет markdown-представления.",
            )
        filename = _safe_md_filename(detail.title or detail.artifact_role, artifact_id)
        return Response(
            content=detail.markdown_content,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": _content_disposition_header(filename),
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/projects/{project_id}/export.zip")
    def export_project_zip(project_id: str) -> Response:
        """Массовый экспорт: zip со всеми MD-артефактами проекта.

        В архив входят только артефакты, у которых есть markdown
        (артефакты без MD пропускаются с записью в MANIFEST.txt — не молча).
        Вложения в архив не входят (только сгенерированные артефакты).
        """
        archive_bytes, _ = _build_markdown_zip(query_service, project_id)
        filename = _safe_zip_filename(project_id)
        return Response(
            content=archive_bytes,
            media_type="application/zip",
            headers={
                "Content-Disposition": _content_disposition_header(filename),
                "Cache-Control": "no-store",
            },
        )

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

    @app.get("/api/projects/{project_id}/tasks/{task_id}/harness-trace")
    def task_harness_trace(project_id: str, task_id: str) -> Any:
        # Ф6: провенанс прогона узла-агента (адаптер/brief/транскрипт/гейты/
        # расход) — тем же паттерном, что methodology-trace.
        return to_primitive(query_service.task_harness_trace(project_id, task_id))

    # ------ L6 design extensions ------------------------------------------
    @app.get("/api/projects/{project_id}/artifacts/{artifact_id}/skeleton")
    def project_artifact_skeleton(project_id: str, artifact_id: str) -> Any:
        return to_primitive(query_service.artifact_skeleton(project_id, artifact_id))

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

    def _runs_with_task_titles(workspace: Path, payload: Any) -> Any:
        """Обогатить шаги прогона человеческим именем задачи по task_id.

        Имена берём из ВСЕХ задач workspace (включая прошлые/будущие гейты),
        поэтому лента «В работе/Выполнено» показывает названия задач любого
        гейта, а не только активного (иначе шаги прошлых гейтов отображались
        бы как id).
        """
        if payload is None:
            return None
        try:
            all_tasks = runtime.list_tasks(workspace)
            titles = {t.task_id: t.title for t in all_tasks}
            statuses = {t.task_id: t.status for t in all_tasks}
        except Exception:  # noqa: BLE001 — обогащение best-effort
            return payload
        runs = payload if isinstance(payload, list) else [payload]
        for run in runs:
            for step in run.get("steps", []) or []:
                tid = step.get("task_id")
                if not tid:
                    continue
                if titles.get(tid):
                    step["task_title"] = titles[tid]
                # #1: текущий статус задачи (по ВСЕМ гейтам) — лента сверяет
                # устаревший failed-шаг (разрыв сети / IncompleteRead) с реальным
                # статусом задачи, даже если задача из прошлого/неактивного гейта,
                # которого нет в активном графе после перезагрузки.
                if statuses.get(tid):
                    step["task_status"] = statuses[tid]
        return payload

    @app.get("/api/projects/{project_id}/workflow-runs/active")
    def workflow_runs_active(project_id: str) -> Any:
        workspace_ref = catalog.resolve_workspace(project_id)
        record = workflow_runner_service.latest_active_run(workspace_ref.workspace, project_id)
        return _runs_with_task_titles(
            workspace_ref.workspace, to_primitive(record) if record is not None else None
        )

    @app.get("/api/projects/{project_id}/workflow-runs")
    def workflow_runs_list(project_id: str, limit: int = 20) -> Any:
        workspace_ref = catalog.resolve_workspace(project_id)
        return _runs_with_task_titles(
            workspace_ref.workspace,
            to_primitive(
                workflow_runner_service.list_runs(
                    workspace_ref.workspace, project_id=project_id, limit=limit
                )
            ),
        )

    @app.get("/api/projects/{project_id}/workflow-runs/{run_id}")
    def workflow_run_detail(project_id: str, run_id: str) -> Any:
        workspace_ref = catalog.resolve_workspace(project_id)
        record = workflow_runner_service.get_run(workspace_ref.workspace, run_id)
        if record is None:
            return JSONResponse(status_code=404, content={"error": "run_not_found"})
        return _runs_with_task_titles(workspace_ref.workspace, to_primitive(record))

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

    @app.get("/api/projects/{project_id}/rollback/preview")
    def rollback_preview(project_id: str, target_task_id: str) -> Any:
        """Превью отката: какие шаги инвалидируются и какие артефакты уйдут в архив.

        Чистое чтение — состояние проекта не меняется. Используется UI для
        диалога подтверждения «что я потеряю».
        """
        return to_primitive(query_service.rollback_preview(project_id, target_task_id))

    @app.get("/api/projects/{project_id}/rollback/history")
    def rollback_history(project_id: str) -> Any:
        """История выполненных откатов проекта (аудит, свежие сверху)."""
        return to_primitive(query_service.rollback_history(project_id))

    @app.post("/api/projects/{project_id}/commands/rollback")
    def rollback_command(
        project_id: str, payload: dict[str, object] = Body(default_factory=dict)
    ) -> Any:
        """Откатить проект к состоянию ДО выбранного шага.

        Координатор берёт эксклюзивный замок проекта, форсированно гасит
        активный прогон и дожидается его оседания, выполняет откат и снимает
        замок (даже при ошибке). Пока идёт откат, конкурентные мутации
        проекта отклоняются (409).
        """
        target_task_id = _required_str(payload, "target_task_id")
        reason = _optional_str(payload, "reason") or ""
        workspace_ref = catalog.resolve_workspace(project_id)
        snapshot = registry_resolver.snapshot_for(workspace_ref.workspace)
        result = rollback_coordinator.rollback_step(
            workspace_ref.workspace,
            snapshot,
            project_id,
            target_task_id,
            reason=reason,
        )
        return to_primitive(result)

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

    @app.post("/api/projects/{project_id}/commands/set-clarification-mode")
    def set_clarification_mode(project_id: str, payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        mode = _required_str(payload, "mode")
        if mode not in {"autopilot", "balanced", "control", "expert"}:
            raise PovGeneratorError("Режим уточнений должен быть одним из: autopilot, balanced, control, expert.")
        result = command_service.set_clarification_mode(project_id, mode=mode)

        # v3.2 auto-continue: если смена режима разблокировала задачи —
        # сразу стартуем workflow run, чтобы пользователь не жал «Run» вручную.
        # Той же логикой пользуется submit-answers endpoint.
        try:
            workspace_ref = catalog.resolve_workspace(project_id)
            ws = workspace_ref.workspace
            already_active = workflow_runner_service.latest_active_run(ws, project_id)
            if already_active is None:
                # Эвристика: «есть смысл запустить» = есть failed-tasks
                # (mode change их auto-retry'ит на ready) или просто что-то
                # есть в очереди. start_run_until_blocked сам поймёт.
                runs = workflow_runner_service.list_runs(ws, project_id=project_id, limit=1)
                last_run = runs[0] if runs else None
                workflow_runner_service.start_run_until_blocked(
                    ws,
                    project_id,
                    provider=last_run.provider if last_run else None,
                    model=last_run.model if last_run else None,
                    max_steps=1000,
                )
        except Exception:  # noqa: BLE001
            pass

        return to_primitive(result)

    @app.post("/api/projects/{project_id}/commands/set-methodology")
    def set_methodology(project_id: str, payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        pack_ref = _required_str(payload, "pack_ref")
        return to_primitive(command_service.set_methodology(project_id, pack_ref=pack_ref))

    @app.post("/api/projects/{project_id}/commands/activate-next-objective")
    def activate_next_objective(project_id: str, payload: dict[str, object] = Body(default_factory=dict)) -> Any:
        new_objective_ref = _required_str(payload, "objective_ref")
        return to_primitive(
            command_service.activate_next_objective(
                project_id, new_objective_ref=new_objective_ref
            )
        )

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
                "attachments",
                "review",
                "state",
                # Aggregated L1 / L2 projections (W2 UI). realtime_token tracks
                # the workspace as a whole, so any change broadcasts these too,
                # which is exactly what L1 Mission Control needs.
                "overview",
                "methodology",
                # Степпер этапов (gate stepper) над вкладками — постоянный
                # статус-слой; меняется на любой записи воркспейса.
                "stages",
                # Прогресс workflow-ранов — первоклассная realtime-проекция.
                # realtime_token меняется на каждой записи runner'а, поэтому
                # клиент получает push и инвалидирует run-запросы без HTTP-
                # поллинга. (UI присылает свой projections-список; здесь —
                # дефолт для консистентности и клиентов без явного списка.)
                "workflow_runs",
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

        # HTML-оболочка SPA НЕ кэшируется (no-cache → браузер ревалидирует),
        # иначе застревает старый index.html (без favicon-link / со старыми
        # хэшами ассетов). Сами ассеты в /assets/* хэшированы и кэшируются.
        _NO_CACHE = {"Cache-Control": "no-cache"}

        @app.get("/", include_in_schema=False)
        def ui_index():
            return FileResponse(ui_dist_root / "index.html", headers=_NO_CACHE)

        @app.get("/{full_path:path}", include_in_schema=False)
        def ui_spa_fallback(full_path: str):
            if full_path.startswith(("api/", "docs", "openapi.json", "redoc", "assets/")):
                return HTMLResponse(status_code=404, content="Not found.")
            # Корневые статические файлы из dist (favicon.svg, robots.txt и т.п.)
            # отдаём напрямую — иначе SPA-fallback вернул бы index.html, и
            # browser получил бы text/html вместо иконки. Защита от ../ traversal:
            # резолвим и проверяем, что путь внутри dist.
            if full_path:
                candidate = (ui_dist_root / full_path).resolve()
                try:
                    candidate.relative_to(ui_dist_root.resolve())
                except ValueError:
                    candidate = None
                if candidate is not None and candidate.is_file():
                    return FileResponse(candidate)
            # Иначе — SPA-маршрут: отдаём index.html (client-side routing).
            index_file = ui_dist_root / "index.html"
            if index_file.exists():
                return FileResponse(index_file, headers=_NO_CACHE)
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


def _content_disposition_header(filename: str, *, inline: bool = False) -> str:
    """Сформировать ``Content-Disposition`` для скачивания или онлайн-просмотра.

    ``inline=True`` отдаёт ``inline`` (браузер рендерит файл во встроенном
    просмотрщике — например, PDF в iframe), иначе ``attachment`` (скачивание).

    HTTP-заголовки в Starlette кодируются как Latin-1; если в filename есть
    кириллица (или другие не-ASCII символы), пишем оба варианта по RFC 5987:
    ``filename="<ascii fallback>"; filename*=UTF-8''<percent-encoded>``.
    """
    import unicodedata
    import urllib.parse

    # ASCII fallback: транслитерация через NFKD-нормализацию + отбрасывание
    # combining-марок, всё, что не-ASCII, заменяется на '_'.
    ascii_fallback = (
        unicodedata.normalize("NFKD", filename)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    # Если содержательная часть имени потерялась при транслитерации (например,
    # чисто кириллический заголовок), берём родовое имя, СОХРАНЯЯ расширение
    # (.md / .zip / .pdf), чтобы legacy-клиенты не получили чужой суффикс.
    ext = filename[filename.rfind(".") :] if "." in filename else ""
    if not ext.isascii():
        ext = ""
    stem = ascii_fallback[: -len(ext)] if ext and ascii_fallback.endswith(ext) else ascii_fallback
    if not any(ch.isalnum() for ch in stem):
        ascii_fallback = f"artifact{ext}"

    percent_encoded = urllib.parse.quote(filename, safe="")
    disposition = "inline" if inline else "attachment"
    return (
        f'{disposition}; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{percent_encoded}"
    )


def _safe_pdf_filename(title: str, artifact_id: str) -> str:
    """Преобразовать заголовок артефакта в файлово-безопасное имя.

    Латиница / кириллица оставляются, остальное заменяется на `_`.
    Длина обрезается до 80 символов; ``artifact_id`` (короткая
    шестнадцатеричная часть) добавляется в конце для уникальности.
    """
    import re

    base = (title or "artifact").strip()
    # Кириллица + латиница + цифры + дефис + пробел; остальное → "_".
    safe = re.sub(r"[^\w\sа-яА-ЯёЁ-]", "_", base, flags=re.UNICODE)
    safe = re.sub(r"\s+", "_", safe).strip("._")[:80] or "artifact"
    short_id = artifact_id.replace("-", "")[:8]
    return f"{safe}_{short_id}.pdf"


def _safe_artifact_stem(title: str, artifact_id: str) -> str:
    """Файлово-безопасная основа имени артефакта без расширения."""
    import re

    base = (title or "artifact").strip()
    safe = re.sub(r"[^\w\sа-яА-ЯёЁ-]", "_", base, flags=re.UNICODE)
    safe = re.sub(r"\s+", "_", safe).strip("._")[:80] or "artifact"
    short_id = artifact_id.replace("-", "")[:8]
    return f"{safe}_{short_id}"


def _safe_md_filename(title: str, artifact_id: str) -> str:
    return f"{_safe_artifact_stem(title, artifact_id)}.md"


def _safe_zip_filename(project_id: str) -> str:
    return f"project_{project_id.replace('-', '')[:8]}_markdown.zip"


def _build_markdown_zip(query_service: WorkspaceQueryService, project_id: str) -> tuple[bytes, list[str]]:
    """Собрать zip со всеми MD-артефактами проекта.

    Возвращает (байты архива, список включённых имён). Артефакты без MD —
    пропускаются с пометкой в ``MANIFEST.txt`` (не молча). Дубли имён
    разводятся суффиксом ``artifact_id``.
    """
    import io
    import zipfile

    summaries = query_service.project_artifacts(project_id)
    included: list[str] = []
    skipped: list[str] = []
    used_names: set[str] = set()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for summary in summaries:
            if not summary.has_markdown:
                skipped.append(f"{summary.title} ({summary.artifact_role}) — нет markdown")
                continue
            detail = query_service.artifact_detail(project_id, summary.artifact_id)
            if not detail.markdown_content:
                skipped.append(f"{summary.title} ({summary.artifact_role}) — нет markdown")
                continue
            name = _safe_md_filename(summary.title or summary.artifact_role, summary.artifact_id)
            # На всякий случай разруливаем коллизии имён (имя уже содержит
            # short-id, но подстрахуемся).
            if name in used_names:
                name = f"{_safe_artifact_stem(summary.title or summary.artifact_role, summary.artifact_id)}_{len(used_names)}.md"
            used_names.add(name)
            archive.writestr(name, detail.markdown_content)
            included.append(name)

        manifest_lines = [
            f"Экспорт проекта {project_id}",
            f"Включено артефактов: {len(included)}",
            *(f"  + {name}" for name in included),
        ]
        if skipped:
            manifest_lines.append(f"Пропущено (без markdown): {len(skipped)}")
            manifest_lines.extend(f"  - {item}" for item in skipped)
        if not included and not skipped:
            manifest_lines.append("В проекте пока нет артефактов.")
        archive.writestr("MANIFEST.txt", "\n".join(manifest_lines) + "\n")
    return buffer.getvalue(), included


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


def _optional_str_keep_empty(payload: dict[str, object], key: str) -> str | None:
    """Как _optional_str, но допускает пустую строку (для api_key=""→сброс)."""
    if key not in payload:
        return None
    value = payload[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise PovGeneratorError(f"Поле '{key}' должно быть строкой.")
    return value


def _extract_extras(raw: object) -> dict[str, str]:
    """Привести extras-поле к dict[str, str] и обрезать значения."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PovGeneratorError("Поле 'extras' должно быть объектом.")
    return {str(k): str(v).strip() for k, v in raw.items() if v is not None}


def _provider_connection_to_dict(connection) -> dict[str, object]:
    """Сериализация ProviderConnection для API. api_key НЕ выводим — только
    маска и факт наличия."""
    api_key = connection.credentials.api_key or ""
    return {
        "connection_id": connection.connection_id,
        "provider_type": connection.provider_type,
        "display_name": connection.display_name,
        "has_api_key": bool(api_key),
        "api_key_preview": _mask_secret(api_key),
        "extras": dict(connection.extras),
        "source": connection.source,
        "created_at": connection.created_at,
        "last_tested_at": connection.last_tested_at,
        "last_test_status": connection.last_test_status,
        "last_test_message": connection.last_test_message,
    }


def _mask_secret(secret: str) -> str:
    """Маска для UI: 'sk-or-v1-d22a0...c945b' → 'sk-…c945b'."""
    if not secret:
        return ""
    if len(secret) <= 8:
        return "•" * len(secret)
    return f"{secret[:3]}…{secret[-5:]}"


def _test_result_to_dict(result) -> dict[str, object]:
    return {
        "status": result.status,
        "message": result.message,
        "latency_ms": result.latency_ms,
        "sample_response": result.sample_response,
        "tested_at": result.tested_at,
    }


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
    # access_log=False: HTTP-запросы логирует наш middleware (request_id,
    # тайминг, уровни) — uvicorn.access дублировал бы их.
    if args.reload or args.workers > 1:
        uvicorn.run(
            "pov_generator.interfaces.api:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=args.reload,
            workers=args.workers if args.workers > 1 and not args.reload else None,
            log_level=args.log_level,
            access_log=False,
        )
        return

    repo_root = Path(__file__).resolve().parents[3]
    uvicorn.run(
        create_app(repo_root=repo_root, runtime_root=repo_root / "runtime"),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=False,
    )


if __name__ == "__main__":
    main()
