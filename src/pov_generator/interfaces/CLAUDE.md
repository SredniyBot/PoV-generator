# interfaces/ — входные точки

Слой адаптеров: REST/WebSocket API (`api.py`, `povgen-api`) и CLI (`cli.py`, `povgen`).
Оба собирают application-сервисы поверх `SqliteRuntime` и реестра шаблонов; бизнес-логики здесь нет, только парсинг запроса → вызов сервиса → сериализация (`to_primitive` / `json_dumps`).

## HTTP/WebSocket API (api.py)

Фабрика `create_app()` (`api.py:38`) инстанцирует все сервисы, монтирует роуты и делает startup-recovery зомби-`workflow_runs`/`tasks` от перезапущенного процесса (`api.py:107-168`). DI прокинут в `app.state` (`api.py:101-105`). Глобальный handler `PovGeneratorError → 409` (`api.py:170`). Все REST-пути под префиксом `/api`.

Группы эндпоинтов:

| Группа | Пути (примеры) | Назначение | Сервис |
|---|---|---|---|
| health | `GET /api/health` (`api.py:174`) | статус + runtime_root | — |
| settings (LLM) | `/api/settings/purposes\|providers\|models\|routings\|assignments\|diagnostics`, `…/test`, `…/sync-models` (`api.py:189-349`) | CRUD провайдеров/моделей/назначений, test_connection/test_model, диагностика резолюции | `ProviderSettingsService` |
| projects (create/list) | `GET/POST /api/projects` (`api.py:351-369`) | список и создание проекта | `WorkspaceQueryService` / `WorkspaceCommandService` |
| registry | `GET /api/registry/objectives\|domain-packs\|methodology-packs` (`api.py:371-381`) | каталоги для UI | `WorkspaceQueryService` |
| project projections (read) | `/api/projects/{id}/shell\|overview\|task-graph\|situation\|timeline\|clarifications\|artifacts\|review\|state\|debug\|decisions\|artifact-versions\|failure-pins` (`api.py:383-481`) | проекции для UI (см. spec 11 §2, L1/L2 §6) | `WorkspaceQueryService` |
| artifacts | `GET …/artifacts/{aid}` (`api.py:415`), `GET …/artifacts/{aid}/download.pdf` (`api.py:419`), `GET …/artifacts/{aid}/download.md`, `GET …/export.zip`, `…/skeleton` (`api.py:466`) | детали + экспорт PDF (`render_artifact_pdf`, RFC 5987 имя файла), MD (text/markdown) и zip всех MD-артефактов (+ MANIFEST.txt) | query + `pdf_export` |
| attachments | `POST/GET …/attachments`, `GET …/attachments/{aid}/download`, `DELETE …/attachments/{aid}` | загрузка/список входных файлов, скачивание, удаление-до-использования | `AttachmentService` |
| commands (write) | `POST /api/projects/{id}/commands/run-next\|run-until-blocked\|cancel-workflow\|retry-task\|set-goal\|close-gap\|set-readiness\|enable-domain-pack\|answer-clarification\|accept-assumption\|set-clarification-mode\|set-methodology\|defer-clarification\|reopen-clarification` (`api.py:482-707`) | мутации workflow/clarifications (spec 11 §4-5) | `WorkspaceCommandService`, `WorkflowRunnerService`, `ClarificationService` |
| workflow runs (async) | `POST …/commands/run-until-blocked` (`api.py:492`) запускает async-run и сразу возвращает `WorkflowRunRecord(status=pending)`; `GET …/workflow-runs/active` (`api.py:525`), `GET …/workflow-runs` (`api.py:531`), `GET …/workflow-runs/{rid}` (`api.py:538`) | запуск/наблюдение/отмена прогона; runner крутит daemon-поток | `WorkflowRunnerService` |
| clarifications flow | `GET …/clarifications/{cid}/events` (`api.py:709`), `GET …/clarifications/next` (`api.py:716`) | навигация wizard'а | `ClarificationService` / `runtime` |

Особенности команд:
- `run-until-blocked` (`api.py:492`): async; `max_steps` дефолт 1000 (sanity-ceiling, не лимит UX).
- `answer-clarification` / `accept-assumption` дёргают `_autoresume_workflow_if_unblocked` (`api.py:583`) — авто-возобновляют workflow, когда не осталось blocking-open вопросов; provider/model берутся из последнего run проекта.

### WebSocket
`WS /ws/projects/{project_id}` (`api.py:735`). При подключении шлёт `snapshot` со списком проекций и их signature, далее поллит `query_service.realtime_token` каждые `poll_interval` (дефолт 0.75с, `api.py:42`/`api.py:105`) и при смене токена рассылает по `projection_changed` на каждую проекцию (signature-based, не дельты). Набор проекций — из query-param `projections` или дефолтный список (`api.py:743-757`,
включает `attachments`).

Расхождение со spec 11 §4: спека описывает канал `/api/projects/{id}/events` и типизированные сообщения (`snapshot_ready`, `task_changed`, `artifact_changed`, …) с per-projection версиями; реализация проще — единый `realtime_token` на весь workspace и только два типа сообщений (`snapshot`, `projection_changed`). При коде истина — реализация.

### UI (статика)
Если есть `ui/workspace/dist` (`api.py:49`): `GET /assets/{path}` (`api.py:797`), `GET /` → `index.html` (`api.py:804`), SPA-fallback `GET /{full_path}` (`api.py:808`, исключает `api/`, `docs`, `openapi.json`, `assets/`). Если билда нет — `/` отдаёт заглушку «UI не собран» со ссылкой на `/docs` (`api.py:817`).

### main() — povgen-api (`api.py:961`)
Запускает uvicorn. Флаги/env: `--host`/`POV_HOST` (`127.0.0.1`), `--port`/`POV_PORT` (8788), `--reload`/`POV_RELOAD`, `--workers`/`POV_WORKERS`, `--log-level`/`POV_LOG_LEVEL`. При `--reload` или `workers>1` запускает по import-string с `factory=True` (`api.py:1014`), иначе передаёт готовый объект приложения.

## CLI (cli.py)

`main()` (`cli.py:23`) собирает сервисы и диспатчит на `_dispatch` (`cli.py:60`); `PovGeneratorError → stderr + exit 1`. Вывод — JSON через `json_dumps`. Парсер — `_build_parser` (`cli.py:345`). Структура `povgen <entity> <action>`; почти у всех action есть `--workspace`.

| entity action | Назначение | dispatch |
|---|---|---|
| `registry validate` | валидация реестра шаблонов (JSON-отчёт) | `cli.py:74` |
| `registry show-template\|show-objective\|show-domain-pack` | резолв и вывод объекта реестра | `cli.py:85-93` |
| `project init` | инициализация проекта + expand_graph + auto/manual выбор domain pack | `cli.py:96` |
| `project show` | манифест проекта | `cli.py:165` |
| `problem show\|history\|goal-set\|gap-open\|gap-close\|readiness-set\|fact-add\|domain-pack-enable` | работа с problem/project state | `cli.py:169-228` |
| `plan dry-run\|apply\|history` | планирование графа задач | `cli.py:230-243` |
| `tasks list\|events\|transition` | задачи и переходы state-machine | `cli.py:245-255` |
| `artifacts list\|show` | артефакты (record + content) | `cli.py:257-266` |
| `context build` | сборка execution-контекста для задачи | `cli.py:273` |
| `execute task\|runs\|traces` | исполнение задачи + журналы прогонов | `cli.py:282-300` |
| `validation runs\|escalations` | прогоны валидации и эскалации | `cli.py:304-309` |
| `workflow run-next\|run-until-blocked` | прогон workflow (синхронно, в отличие от API) | `cli.py:316-340` |

Аргументы прогонов: `execute task` / `workflow *` — `--provider` (дефолт `stub`) и `--model`; `workflow run-until-blocked --max-steps` дефолт 20 (`cli.py:469`) — заметно меньше API-дефолта 1000. Многие команды требуют валидный registry (иначе ошибка с подсказкой `povgen registry validate`).

### main() — povgen (`cli.py:23`)
Console-script. CLI создаёт `LLMProviderRegistry()` без settings-store (`cli.py:33`), т.е. не использует системные настройки провайдеров из БД (в отличие от API, `api.py:57`).

## Gotchas
- Точки входа (`pyproject.toml` `[project.scripts]`): `povgen = pov_generator.interfaces.cli:main`, `povgen-api = pov_generator.interfaces.api:main`.
- Порт API по умолчанию — **8788** (`api.py:989`); UI-dev (vite) — 5173.
- Dev-связка UI↔API: `ui/workspace/vite.config.ts` проксирует `/api` → `http://127.0.0.1:8788` и `/ws` → `ws://127.0.0.1:8788` (`changeOrigin`, `ws:true`). В проде UI отдаётся самим FastAPI из `ui/workspace/dist`; собирается `npm run build` (`tsc && vite build`).
- Версии запинены в `pyproject.toml`: `fastapi>=0.115.0,<1.0`, `starlette>=0.40,<0.42` (`pyproject.toml:16`), `uvicorn>=0.35.0,<1.0`, `claude-agent-sdk>=0.0.20,<1.0`. Верхняя граница starlette продиктована транзитивной зависимостью `claude-agent-sdk → mcp`, которая тянет starlette 1.0.0 и ломает fastapi 0.115 (`Router.__init__` kwargs) — см. инлайн-комментарий `pyproject.toml:14-15`. В lock зафиксированы `starlette==0.41.3`, `sse-starlette==3.0.3`.
- API делает идемпотентный bootstrap настроек LLM из env при старте (`ensure_default_settings` + `sync_all_connections`, `api.py:63-76`); сбой не критичен (настраивается через `/settings`).
