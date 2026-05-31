# infrastructure/ — I/O

Слой ввода-вывода: персистентность состояния проекта (SQLite per-workspace),
системные настройки LLM (отдельная SQLite), загрузка YAML-реестра шаблонов,
шифрование секретов и адаптеры LLM-провайдеров. Зависит от `domain/` и
`common/`, бизнес-логики не содержит — только сериализация и внешние вызовы.

## Модули

- `sqlite_runtime.py` — основной репозиторий состояния проекта (per-workspace
  `runtime.db`). `SqliteRuntime` (`sqlite_runtime.py:404`). Множество свободных
  функций `_*_from_row` / `_*_to_dict` — сериализация доменных объектов.
- `llm_settings_store.py` — CRUD-репозиторий настроек LLM (system-wide
  `settings.db`). `SqliteSettingsStore` (`llm_settings_store.py:39`).
- `filesystem_registry.py` — YAML-loader реестра шаблонов.
  `FilesystemRegistryLoader` (`filesystem_registry.py:20`).
- `secret_box.py` — симметричное шифрование секретов. `SecretBox`
  (`secret_box.py:33`).
- `llm/` — подпакет адаптеров LLM: протокол, реестр-резолвер, providers/. См.
  «LLM-провайдеры».
- `openrouter_client.py`, `claude_sdk_client.py`, `claude_subscription_client.py`
  — низкоуровневые HTTP/SDK-клиенты. **НЕ legacy/мёртвый код**: используются
  адаптерами в `llm/providers/*` (см. «LLM-провайдеры»).

## Хранилище состояния

Две независимые SQLite-БД, схемы создаются лениво idempotent-скриптами
`create table if not exists` при первом `_connect`. Кэш «схема проверена»
держится в памяти на инстанс (`_schema_ensured`).

**Runtime (`SqliteRuntime`) — состояние одного проекта.** Файл
`<workspace>/runtime.db` (`DB_FILENAME = "runtime.db"`, `sqlite_runtime.py:405`)
плюс `project.json` — manifest на ФС (`sqlite_runtime.py:406`, `create_workspace`
`:413`). Соединение открывается на каждую операцию (`_connect`,
`sqlite_runtime.py:1775`), schema один раз на файл (`_ensure_schema` `:1796`).
Pragmas: `journal_mode=MEMORY`, `synchronous=OFF` — single-process, скорость в
ущерб crash-durability (`:1784`).

Это **НЕ event-sourcing**: текущее состояние хранится в snapshot-таблицах, а
не реконструируется из лога. Таблицы (`_ensure_schema`, `sqlite_runtime.py:1796`):

- `knowledge_snapshots`, `process_snapshots` — по одному snapshot на проект
  (Layer A «знания» = `ProjectKnowledge`, Layer B «процесс» = `ProcessState`),
  хранятся как JSON; апдейт через `apply_knowledge_patch` / `apply_process_patch`
  (`sqlite_runtime.py:540`, `:586`) — патч применяется в домене, snapshot
  перезаписывается.
- `state_events` — **append-only аудит** изменений Layer A/B (`layer`, `version`,
  `patch_type`, `payload_json`, `actor`, `reason`). Пишется тем же патч-методом
  параллельно со snapshot. Используется только для истории/аудита
  (`list_state_events` `:509`), state из него не пересобирается.
- `tasks` + `task_events` — задачи и append-only лог их переходов
  (`create_task` `:632`, `transition_task` `:683`, переход считается в домене
  `apply_task_command`; событие пишет `_insert_task_event` `:1758`).
- `artifacts` — метаданные артефактов (контент — отдельный файл по
  `storage_path`, `store_artifact` `:787`); граф связей через
  `input_artifact_ids_json` / `parent_artifact_id`, обход `downstream_artifacts`
  / `upstream_artifacts` (`:899`, `:931`); версионирование через
  `is_superseded`.
- `context_manifests` (+ `context_manifest_items`), `execution_runs` (+
  `execution_traces`), `planning_decisions`, `validation_runs`,
  `escalation_tickets`, `clarification_candidates`, `clarification_requests`,
  `workflow_runs` — записи аудита/трассировки исполнения, валидации,
  уточнений и асинхронных прогонов.

Эволюция схемы — без таблицы версий: после `create table if not exists`
прогоняются idempotent `_ensure_column` (`sqlite_runtime.py:2104`), добавляющие
недостающие колонки в старые БД через `ALTER TABLE` (visibility, auto_resolved,
is_superseded, input/child_artifact_ids_json и др.). `clarification_events` и
`workflow_runs` досоздаются отдельными `executescript` (`:2064`, `:2080`).

**Settings (`SqliteSettingsStore`) — system-wide, не per-workspace.** Файл
`<runtime_root>/settings.db` (`_DB_FILENAME`, `llm_settings_store.py:36`).
Три таблицы (`_ensure_schema` `:363`): `provider_connections`
(подключения провайдеров + зашифрованный `credentials_api_key_encrypted`),
`model_routings` (модель → connection с приоритетом, для fallback), 
`model_assignments` (purpose → model_name, UPSERT по purpose). `PRAGMA
foreign_keys = ON`, но CASCADE вручную: routings удаляются перед connection
(`delete_connection` `:130`).

## LLM-провайдеры

Двухуровневая структура: подпакет `llm/` (контракт + резолвер + адаптеры),
адаптеры делегируют низкоуровневым клиентам в корне `infrastructure/`.

- **Протокол** `LLMProvider` (`llm/protocol.py:15`, PEP 544
  `@runtime_checkable`): атрибуты `name: str`, `model: str | None` + метод
  `chat_json(*, system_prompt, user_prompt, schema) -> dict`. Реализации не
  должны читать env (это задача реестра) и не должны делать switch по
  провайдерам.
- **Резолвер** `LLMProviderRegistry` (`llm/registry.py:104`) — единственное
  место switch по типу провайдера. Два пути:
  - `get(provider=..., model=..., complexity=...)` (`registry.py:122`) —
    низкоуровневый, по имени, кредиты из env (`_ENV_BUILDERS` `:74`); legacy/тесты.
  - `resolve_for_purpose(purpose, ...)` (`registry.py:175`) — **основной путь**:
    `purpose → ModelAssignment → ModelRouting(по priority) → ProviderConnection`
    через `SqliteSettingsStore`, строит адаптер `from_connection`
    (`_build_from_connection` `:244`). Fail loudly: при отсутствии назначения /
    routings / connection — `ConflictError`; перебирает routings по приоритету,
    auto-fallback по моделям не делает.
  - Маппинг `provider_type → имя`: `openrouter→openrouter`,
    `anthropic→claude_sdk`, `claude_cli→claude_subscription`
    (`_PROVIDER_TYPE_TO_NAME` `:84`).

Три адаптера в `llm/providers/`, каждый строится через прямой конструктор /
`from_env` / `from_connection` и реализует `chat_json`:

- `openrouter` — `OpenRouterProvider` (`llm/providers/openrouter.py:16`). Делегирует
  `OpenRouterClient`. HTTP к OpenRouter (`urllib`), structured output через
  `response_format: json_schema` strict (`openrouter_client.py:40`). Ключ из
  connection/env (`POV_OPENROUTER_API_KEY`).
- `claude_sdk` — `ClaudeSdkProvider` (`llm/providers/claude_sdk.py:13`). Делегирует
  `ClaudeSdkClient` — **Anthropic Messages API** через SDK `anthropic`.
  Structured output эмулируется tool-use: tool с `input_schema=schema` и
  `tool_choice` форсирующий его (`claude_sdk_client.py:77`). Требует API-ключ
  (`POV_ANTHROPIC_API_KEY` / `ANTHROPIC_API_KEY`).
- `claude_subscription` — `ClaudeSubscriptionProvider`
  (`llm/providers/claude_subscription.py:16`). Делегирует
  `ClaudeSubscriptionClient` — **локальный CLI `claude`** через
  `claude_agent_sdk` (авторизация по сессии `claude login`, без API-ключа).
  Схема передаётся текстом в промпте, JSON вытаскивается регэкспом
  (`_extract_json`, `claude_subscription_client.py:253`); sync `chat_json`
  оборачивает async через `asyncio.run` с retry/backoff на транзиентные сбои CLI
  (`:111`). Важно: явно резолвит системный CLI (`_resolve_cli_path` `:277`),
  чтобы SDK не ушёл в незалогиненный bundled-бинарник.

Stub-провайдера в `llm/` **нет**: `stub` — не LLM-провайдер реестра, а
fixture-замена, обрабатываемая в `application/execution_service` (`_execute_stub`,
`_load_stub_fixture` — payload'ы из `templates/stub_fixtures/*.json`). В тестах
реальные провайдеры подменяются мок-классами под `@runtime_checkable`-протокол.

**Как добавить провайдера:** (1) низкоуровневый клиент (по образцу
`*_client.py`) при необходимости; (2) адаптер в `llm/providers/` с `name`,
`model`, `chat_json` и билдерами `from_env`/`from_connection`; (3)
зарегистрировать env-билдер в `_ENV_BUILDERS` (`registry.py:74`), маппинг типа
в `_PROVIDER_TYPE_TO_NAME` (`:84`) и ветку в `_build_from_connection` (`:244`).

**Отношение корневых `*_client.py` к `llm/providers/*` (важно):** это НЕ
дубликаты. `llm/providers/*` — тонкие адаптеры под протокол `LLMProvider`,
которые **импортируют и оборачивают** соответствующие `*_client.py`
(`openrouter.py:10`, `claude_sdk.py:10`, `claude_subscription.py:9`). Вся
сетевая/SDK/CLI-логика и `model_for_complexity` живут в клиентах; адаптеры
дают единый интерфейс и три способа конструирования. Клиенты также напрямую
тестируются (`tests/test_methodology_and_clients.py`). Tech debt: дублируется
логика `from_env` (есть и в клиенте, и в адаптере) и `model_for_complexity`
определена в двух клиентах с разной семантикой (sdk возвращает дефолт-модель,
subscription — `None`).

## Шифрование настроек

`SecretBox` (`secret_box.py:33`) — Fernet (AES-CBC + HMAC-SHA256, пакет
`cryptography`). Ключ резолвится в порядке (`_resolve_key`, `secret_box.py:81`):
(1) env **`POV_SECRET_KEY`** (готовый base64-urlsafe Fernet-ключ 32 байта,
валидируется); (2) файл `<runtime_root>/.secret_key` (генерируется при первом
запуске, права 0600). Без обоих источников ключ всё равно создаётся в файле —
implicit plaintext исключён. `decrypt` битого токена → `ConflictError` с
подсказкой про ротацию ключа (`:67`). Используется `SqliteSettingsStore` для
шифрования `api_key` провайдеров (encrypt при записи, decrypt при чтении —
прозрачно для вызывающего).
