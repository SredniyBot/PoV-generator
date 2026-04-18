# Спецификации платформы PoV Lab: обзор

> **Статус:** v1.1 · Draft · 2026-04-18
> **Авторы спецификации:** команда PoV Lab
> **Область применения:** полный пакет архитектурных спецификаций платформы PoV Lab.

Документ задаёт общий словарь, shared-типы, технологический стек и интеграционные правила для всех компонент платформы PoV Lab. Детали поведения каждого слоя вынесены в отдельные спеки:

- `01_template_registry.md` — хранение, индекс, валидация и read-only API шаблонов.
- `02_task_store.md` — lifecycle задач, DAG, event sourcing, FSM и очереди.
- `03_template_semantics.md` — канонический semantic contract шаблона; это интеллектуальное ядро системы.
- `04_problem_state.md` — структурированное состояние проблемы и semantic memory проекта.
- `05_planning_coordinator.md` — тонкий детерминированный координатор problem loop.
- `06_artifact_context.md` — артефакты, summaries, retrieval и сборка task-local context.
- `07_execution_runtime.md` — LLM/script/tool runtime и execution traces.
- `08_validation_governance.md` — validation, critique, stage gates и human escalation.

Нормативное правило пакета: **семантика работы с проблемой живёт в шаблонах; координатор только применяет эту семантику, но не дублирует её в коде**.

---

## 1. Место компонентов в архитектуре

Архитектура PoV Lab опирается на [ТЗ Архитектура.md](ТЗ%20Архитектура.md) и [PoV.md](PoV.md). Целевая реализация — **модульный монолит** с жёсткими контрактами между пакетами. Вынесение компонент в отдельные сервисы допускается позже, но **не является** частью базовой архитектуры.

Логические компоненты и их связи:

```
┌─────────────────────┐
│ User / Developer UI │
└──────────┬──────────┘
           │ intake / approvals / overrides
           ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ Problem State Store + Planning Coordinator                                  │
│ (specs/04 + specs/05)                                                       │
│ active gaps · decisions · risks · candidate templates · planning decisions  │
└──────────┬───────────────────────────────┬───────────────────────────────────┘
           │ load semantics                │ create/update tasks
           ▼                               ▼
┌──────────────────────┐        ┌─────────────────────────────────────────────┐
│ Template Registry    │        │ Task Store + Task Progression               │
│ (specs/01 + 03)      │        │ (specs/02)                                 │
│ source-of-truth YAML │        │ tasks · deps · events · transitions         │
└──────────┬───────────┘        └──────────────┬──────────────────────────────┘
           │ context policy / tools / effects                 │ ready tasks / outputs
           ▼                                                  ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ Artifact Store + Context Engine + Execution Runtime                         │
│ (specs/06 + specs/07)                                                       │
│ artifacts · summaries · chunks · manifests · runs · traces · tools          │
└──────────┬───────────────────────────────┬───────────────────────────────────┘
           │ validation inputs             │ task results / traces / patches
           ▼                               ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ Validation & Governance                                                     │
│ (specs/08)                                                                  │
│ contract validation · critique · stage gates · escalation                   │
└──────────────────────────────────────────────────────────────────────────────┘
```

- **Template Semantics** — первичный носитель problem-solving логики. Шаблон описывает не только runtime-задачу, но и то, с каким типом проблемы он работает, что закрывает и какой контекст ему нужен.
- **Planning Coordinator** — детерминированно выбирает следующий шаблон на основе открытых gaps, stage gate и activation rules шаблонов. Координатор не вызывает LLM и не содержит доменную методологию в коде.
- **Task Store** — источник истины о lifecycle задач и их зависимостях.
- **Artifact / Context Layer** — источник истины о blob-артефактах, их derived-представлениях и context manifests.
- **Execution Runtime** — единый контракт для LLM, скриптов, инструментов и сред исполнения.
- **Validation & Governance** — замыкает любой execution cycle проверками и human handoff.

---

## 2. Общие принципы

### 2.1. Source-of-truth и проекции
- Для **Template Registry** source of truth — YAML-файлы в Git-репозитории проекта. Таблицы в PostgreSQL — индекс для быстрых выборок, **не** авторитетный источник.
- Для **Task Store** source of truth — таблица `task_events` (append-only Event Sourcing). `tasks`, `task_status_transitions`, `task_dependencies` — материализованные проекции, которые должны быть полностью восстановимы реплеем `task_events`.
- Для **Problem State Store** source of truth — таблица `problem_state_events` (append-only). `problem_state_snapshots`, `problem_gaps`, `problem_decisions` и related projections полностью восстанавливаются реплеем.
- Для **Artifact Store** source of truth — blob в S3/MinIO + неизменяемая metadata-запись в PostgreSQL. Любой summary, chunk-set или extraction — новый derived artifact, а не overwrite.
- Для **Execution Runtime** source of truth — `execution_runs`, `tool_invocations` и trace artifacts. Runtime-трасса не заменяет бизнес-артефакт и не может подменять task output.

### 2.2. Иммутабельность и версионирование
- Опубликованная версия шаблона иммутабельна. Правки создают новую версию. Откат возможен через выбор версии.
- Задача иммутабельна в терминах своего `id`: изменения статуса, атрибутов и результатов логируются как события, но сама строка `tasks` представляет текущую материализованную проекцию.
- Snapshot `ProblemState` иммутабелен по `(project_id, version)`. Новое понимание проблемы всегда фиксируется новой версией состояния.
- Артефакт иммутабелен по `artifact_id`; summary, извлечение и normalization — самостоятельные артефакты с provenance.

### 2.3. Async/Sync
- Template Registry — **синхронный** API (файлы + кеш + PostgreSQL индекс; latency ≤ 5 ms на чтение, ≤ 200 ms на `reload()`).
- Task Store — **асинхронный** API (`asyncpg`, async SQLAlchemy). Все операции возвращают awaitable.
- Planning Coordinator — **асинхронный**, но детерминированный и CPU-light; не делает сетевых вызовов кроме чтения из Registry/Stores.
- Artifact Store и Context Engine — **асинхронные** API.
- Execution Runtime — **асинхронный** API с долгоживущими run/session объектами.

### 2.4. Timezone и сериализация
- Все timestamp'ы — UTC, тип `TIMESTAMPTZ` в PostgreSQL, `datetime` с `tzinfo=UTC` в Python.
- JSON-сериализация — строгая: timestamp'ы в ISO 8601 с суффиксом `Z`; UUID — в канонической строке; enum — в `snake_case` строке.
- Кодировка файлов — UTF-8, без BOM. Разделитель строк — LF. Отступ в YAML — 2 пробела.

### 2.5. Идентификаторы
| Тип | Формат | Генерация | Пример |
|---|---|---|---|
| `TemplateId` | slug `[a-z][a-z0-9_]{2,63}` | ручной выбор автора шаблона | `rag_index_build` |
| `TemplateVersion` | SemVer 2.0 | автор шаблона | `1.2.0` |
| `TaskId` | UUIDv7 | `uuid_utils.uuid7()` на стороне сервиса | `018f3b5a-...-...` |
| `ProjectId` | UUIDv7 | на стороне сервиса | `018f3b5a-...-...` |
| `WorkerId` | `module_name:instance_id` | бизнес-модуль | `codegen_agent:a1b2` |
| `PlanningRunId` | UUIDv7 | Planning Coordinator | `018f3b5a-...-...` |
| `ProblemStateVersion` | `int >= 1` | Problem State Store | `7` |
| `ContextManifestId` | UUIDv7 | Context Engine | `018f3b5a-...-...` |
| `ExecutionRunId` | UUIDv7 | Execution Runtime | `018f3b5a-...-...` |
| `ArtifactRef` | см. §3.4 | Task Store | — |

---

## 3. Общие типы

### 3.1. Статусы задачи (enum)
```python
class TaskStatus(StrEnum):
    BLOCKED = "blocked"                            # ждёт зависимости
    QUEUED = "queued"                              # готова к взятию в работу
    IN_PROGRESS = "in_progress"                    # выполняется
    COMPLETED = "completed"                        # успех, артефакты сохранены
    FAILED = "failed"                              # ошибка, лимиты исчерпаны
    WAITING_FOR_CHILDREN = "waiting_for_children"  # декомпозирована, ждёт детей
    OBSOLETE = "obsolete"                          # инвалидирована перепланом
```

### 3.2. Типы шаблонов (enum)
```python
class TemplateType(StrEnum):
    COMPOSITE = "composite"
    EXECUTABLE = "executable"
    DYNAMIC = "dynamic"
```

### 3.3. Типы требований на входе (enum)
```python
class RequirementKind(StrEnum):
    HARD = "hard"          # без артефакта задача → Failed до вызова LLM
    SOFT = "soft"          # опционально, best-effort
    SEMANTIC = "semantic"  # поиск через Context Engine (RAG)
```

### 3.4. `ArtifactRef`

Неизменяемая ссылка на артефакт, хранящийся в S3 (blob) с метаданными в PostgreSQL.

```python
class ArtifactRef(BaseModel):
    artifact_id: UUID              # UUIDv7
    artifact_type: str             # "schema", "code", "report", "dataset", ...
    mime_type: str                 # "application/json", "text/x-python", ...
    s3_uri: str                    # "s3://pov-lab/<project>/<artifact_id>"
    sha256: str                    # 64 hex chars
    size_bytes: int
    created_at: datetime
    created_by_task_id: UUID | None
```

Принцип: `ArtifactRef` однозначно идентифицирует конкретный байтовый снимок. Перезапись создаёт новый `artifact_id`.

### 3.5. `Provenance`

Метаданные происхождения любой записи (шаблон, задача, событие).

```python
class Provenance(BaseModel):
    actor: str                     # "system", "worker:codegen_agent:a1b2", "engineer:g.orlov"
    timestamp: datetime            # UTC
    reason: str | None = None      # свободный текст (для переходов/правок)
    correlation_id: UUID | None    # trace-id для сквозного трейсинга
```

### 3.6. `SchemaRef`

Ссылка на JSON Schema, проверяющую артефакт или поле. Формат:
```
<scope>:<name>@<version>
```
Примеры: `pov_lab:input.questionnaire@1.0.0`, `project:db_schema@1.1.0`. Резолвинг — через Template Registry (для `pov_lab:*`) или через Task Store (для `project:*`, встраиваются в артефакты).

### 3.7. `StageGate`

Макро-фазы пайплайна. `StageGate` — governance-модель, а не механизм problem-solving.

```python
StageGate = Literal[
    "intake",
    "requirements",
    "architecture",
    "implementation",
    "validation",
    "delivery",
]
```

### 3.8. `TemplateRef`

Ссылка на конкретную опубликованную версию шаблона.

```python
class TemplateRef(BaseModel):
    template_id: str
    template_version: str
```

### 3.9. `ProblemStateRef`

Ссылка на конкретную версию problem state.

```python
class ProblemStateRef(BaseModel):
    project_id: UUID
    version: int
```

### 3.10. `ContextManifestRef`

Ссылка на конкретный context bundle, использованный для execution.

```python
class ContextManifestRef(BaseModel):
    manifest_id: UUID
    project_id: UUID
    task_id: UUID
    created_at: datetime
```

### 3.11. `ExecutionTraceRef`

Ссылка на trace/runtime artifact конкретного execution run.

```python
class ExecutionTraceRef(BaseModel):
    execution_run_id: UUID
    artifact_id: UUID
    trace_kind: Literal["request", "response", "tool_log", "session_transcript", "summary"]
```

### 3.12. ID aliases

```python
PlanningRunId = UUID
ContextManifestId = UUID
ExecutionRunId = UUID
```

---

## 4. Технологический стек

| Слой | Библиотека | Версия (constraint) | Обоснование |
|---|---|---|---|
| Язык | Python | `>=3.11,<3.13` | Совместимость с LangGraph/Mem0 экосистемой, StrEnum, typing.Self |
| Модели данных | `pydantic` | `~=2.7` | Строгая валидация входов/выходов, сериализация в JSON Schema |
| Асинхронный драйвер PG | `asyncpg` | `~=0.29` | Нативный, высокопроизводительный |
| ORM/Core | `sqlalchemy` | `~=2.0` | 2.0 style, async поддержка, `AsyncSession` |
| Миграции | `alembic` | `~=1.13` | Стандарт для SQLAlchemy |
| YAML | `ruamel.yaml` | `~=0.18` | Сохраняет порядок/комментарии, безопасный loader |
| JSON Schema | `jsonschema` | `~=4.21` | Draft 2020-12 |
| UUIDv7 | `uuid-utils` | `~=0.9` | Пока `uuid.uuid7` отсутствует в stdlib ≤3.12 |
| FSM (опц.) | `transitions` | `~=0.9` | Декларативная FSM с проверкой инвариантов |
| File watcher (опц.) | `watchfiles` | `~=0.21` | Hot-reload шаблонов в dev |
| Логирование | `structlog` | `~=24.1` | JSON логи с контекстом |
| Метрики | `prometheus-client` | `~=0.20` | Экспорт метрик |
| Трейсинг | `opentelemetry-sdk`, `opentelemetry-instrumentation-sqlalchemy` | `~=1.24` / `~=0.45b0` | Стандарт для distributed tracing |
| БД | PostgreSQL | `>=15` | JSONB, partial indexes, SKIP LOCKED, generated columns |
| Объектное хранилище | S3 (MinIO совместимо) | — | Артефакты |
| Векторный индекс | `pgvector` | `>=0.6` | semantic retrieval по chunks и summaries |
| Контейнеры | Docker Engine / compatible runtime | `>=24` | изоляция tool/script execution |

Привязка к конкретным framework-оркестраторам допускается только на уровне business modules / execution adapters. Core platform не зависит от LangGraph, Mem0, Graphiti, LangChain и аналогов.

---

## 5. Структура исходников (target)

```
pov_lab/
├── specs/                              # эта спецификация
│   ├── 00_overview.md
│   ├── 01_template_registry.md
│   ├── 02_task_store.md
│   ├── 03_template_semantics.md
│   ├── 04_problem_state.md
│   ├── 05_planning_coordinator.md
│   ├── 06_artifact_context.md
│   ├── 07_execution_runtime.md
│   ├── 08_validation_governance.md
│   ├── schemas/
│   │   ├── template.schema.json
│   │   └── task.schema.json
│   └── examples/
│       ├── templates/
│       └── tasks/
├── templates/                          # YAML-шаблоны (source of truth)
│   └── <domain>/<template_id>.yaml
├── src/
│   ├── pov_lab_common/                 # общие типы из §3
│   ├── pov_lab_templates/              # Template Registry
│   └── pov_lab_tasks/                  # Task Store
│   ├── pov_lab_problem/                # Problem State + Planning
│   ├── pov_lab_context/                # Artifact Store + Context Engine
│   ├── pov_lab_execution/              # LLM / script / tool runtime
│   └── pov_lab_validation/             # Validation + Governance
├── migrations/                         # Alembic
└── tests/
```

---

## 6. Сквозные инварианты и правила

### 6.1. Идентичность
Везде, где задача ссылается на шаблон, используется пара `(template_id, template_version)`. Алиас `latest` разрешается в конкретную версию при создании задачи и фиксируется в `tasks.template_version` — это делает задачу воспроизводимой даже после появления новой версии шаблона.

### 6.2. Валидация
- Шаблон перед публикацией проходит валидацию: JSON Schema + структурные правила (см. §4 в `01_template_registry.md`).
- Семантические поля шаблона проходят валидацию по `03_template_semantics.md`; Planner не имеет права использовать шаблон, если semantic contract невалиден.
- Артефакт перед сохранением в `task_outputs` валидируется по `output_contract` соответствующего шаблона. При невалидности задача → `Failed`.
- Любой execution run обязан иметь `ContextManifest`; run без manifest считается невалидным и не может приводить к `Completed`.

### 6.3. Observability
Обязательный минимум для всех компонентов:
- **Structured logs**: `structlog` с полями `component`, `action`, `project_id`, `task_id`, `template_id`, `template_version`, `correlation_id`.
- **Metrics (Prometheus)**: см. конкретные списки в каждой спеке.
- **Tracing (OpenTelemetry)**: каждый публичный метод API — отдельный span с аттрибутами `project_id`, `task_id`.

### 6.4. Ошибки
Базовые классы в `pov_lab_common.errors`:
```python
class PovLabError(Exception): ...
class ValidationError(PovLabError): ...
class NotFoundError(PovLabError): ...
class ConflictError(PovLabError): ...       # optimistic lock, invalid FSM transition, ...
class IntegrityError(PovLabError): ...      # нарушение инвариантов (циклы, недостающие refs)
class ExternalDependencyError(PovLabError): ...
```
Каждая спека расширяет их своими типами.

### 6.5. Конкурентность
- **Template Registry**: `reload()` атомарен; параллельные `load()` могут видеть либо старый, либо новый снимок, но никогда — частичное состояние.
- **Task Store**: FSM-переходы атомарны (одна транзакция: UPDATE + INSERT event + INSERT transition); `get_ready()` использует `SELECT ... FOR UPDATE SKIP LOCKED` для эксклюзивного взятия.
- **Problem State Store**: `apply_patch()` атомарен; duplicate `patch_id` идемпотентен; optimistic lock по `(project_id, version)`.
- **Context Engine**: manifest сборки идемпотентен по `(task_id, template_ref, problem_state_ref, input_fingerprint)`.
- **Execution Runtime**: каждый run идемпотентен по `execution_run_id`; duplicate completion не может создать второй набор output artifacts.

---

## 7. Минимальный совместный сценарий

```
1. User создаёт project P и прикладывает исходные данные.
2. Intake adapter формирует `ProblemState v1` и открывает gaps:
     common.unclear_success_criteria
     rag.missing_data_profile
3. Planning Coordinator делает `plan_once(project_id=P)`:
     registry.list(active_only=True)
     evaluate_activation_rules(problem_state=v1)
     choose template "requirements_alignment@1.0.0"
4. Task Builder создаёт T0 по шаблону requirements_alignment.
5. Context Engine собирает `ContextManifest M0` по `context_policy` шаблона.
6. Task Router dispatch'ит T0; Execution Runtime выполняет run R0 с manifest M0.
7. Runtime сохраняет outputs + problem_state_patch; Validation слой проверяет output contract.
8. Problem State Store применяет patch → `ProblemState v2`; gap `common.unclear_success_criteria` закрыт.
9. Planning Coordinator запускается снова, выбирает следующий шаблон по оставшимся gaps.
10. Stage-Gate Evaluator закрывает gate только когда exit criteria фазы выполнены.
```

---

## 8. Как читать дальше

- **[01_template_registry.md](01_template_registry.md)** — модель шаблона, хранение, API, валидация, версионирование.
- **[02_task_store.md](02_task_store.md)** — модель задачи, DDL, FSM, API, Event Sourcing, алгоритмы bubble-up и invalidate_subgraph.
- **[03_template_semantics.md](03_template_semantics.md)** — semantic contract шаблона; обязательна к чтению перед реализацией planner и domain templates.
- **[04_problem_state.md](04_problem_state.md)** — структура problem state, patches, gaps, decisions и store API.
- **[05_planning_coordinator.md](05_planning_coordinator.md)** — deterministic planning loop и materialization rules.
- **[06_artifact_context.md](06_artifact_context.md)** — artifact store, summaries, semantic index, context manifests.
- **[07_execution_runtime.md](07_execution_runtime.md)** — LLM/script/tool runtime, adapters, traces и execution contracts.
- **[08_validation_governance.md](08_validation_governance.md)** — validation pipeline, critique, stage-gate governance и escalation.
- **[schemas/template.schema.json](schemas/template.schema.json)** — текущая JSON Schema нижнего слоя шаблонов; для семантических полей из `03_template_semantics.md` требуется schema v2.
- **[schemas/task.schema.json](schemas/task.schema.json)** — JSON Schema payload'ов Task Store.
- **[examples/](examples/)** — эталонные примеры: 3 шаблона (по одному на тип) и 2 задачи (в разных статусах).
