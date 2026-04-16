# Спецификации Template Registry и Task Store: обзор

> **Статус:** v1.0 · Draft · 2026-04-15
> **Авторы спецификации:** команда PoV Lab
> **Область применения:** два независимо реализуемых компонента платформы PoV Lab.

Документ связывает две спецификации (`01_template_registry.md`, `02_task_store.md`) и фиксирует общие для них контракты, терминологию, технологический стек и сквозные правила. Всё, что относится к зоне ответственности конкретного компонента, описывается в его документе; здесь — только пересечения.

---

## 1. Место компонентов в архитектуре

Архитектура PoV Lab описана в [ТЗ Архитектура.md](../Downloads/ТЗ%20Архитектура.md). Компоненты, с которыми взаимодействуют Template Registry и Task Store:

```
                      ┌──────────────────────┐
                      │  Stage-Gate Manager  │  (макро-фазы)
                      └──────────┬───────────┘
                                 │ opens/closes gate
                                 ▼
┌────────────────┐     ┌────────────────────┐     ┌──────────────────┐
│ Template       │ ◄── │    Task Router     │ ──► │  Business        │
│ Registry       │     │  (Event Loop,      │     │  Modules         │
│ (specs/01)     │     │   FIFO dispatcher) │     │  (agents)        │
└───────┬────────┘     └────────┬───────────┘     └────────┬─────────┘
        │ resolve()             │ get_ready() /            │ results
        │                       │ mark_*()                 │
        ▼                       ▼                          ▼
┌────────────────────────────────────────────────────────────────────┐
│                        Task Store (specs/02)                       │
│  tasks · task_dependencies · task_inputs · task_outputs ·          │
│  task_events (append-only) · task_status_transitions               │
└───────┬──────────────────────────────────────────────────┬─────────┘
        │ read input_requirements                          │ subscribe
        ▼                                                  ▼
┌────────────────┐                              ┌──────────────────┐
│ Context Engine │                              │ Interruption     │
│ (RAG, Mem0)    │                              │ Gateway          │
└────────────────┘                              └──────────────────┘
```

- **Template Registry** — каталог типов задач. Источник истины о том, *что* задача должна делать (вход, выход, исполнитель, правила декомпозиции).
- **Task Store** — реестр конкретных задач в рамках проектов. Источник истины о том, *в каком состоянии* находится каждая задача.
- **Task Router** — потребитель обоих: создаёт задачи по шаблонам, диспетчеризует готовые, получает результаты.
- **Context Engine** — потребитель Template Registry (читает `input_requirements`) и Task Store (читает артефакты зависимостей).
- **Stage-Gate Manager** — потребитель Task Store (агрегат статусов по `stage_gate`).
- **Interruption Gateway** — слушает события `escalation_required` из Task Store.

---

## 2. Общие принципы

### 2.1. Source-of-truth и проекции
- Для **Template Registry** source of truth — YAML-файлы в Git-репозитории проекта. Таблицы в PostgreSQL — индекс для быстрых выборок, **не** авторитетный источник.
- Для **Task Store** source of truth — таблица `task_events` (append-only Event Sourcing). `tasks`, `task_status_transitions`, `task_dependencies` — материализованные проекции, которые должны быть полностью восстановимы реплеем `task_events`.

### 2.2. Иммутабельность и версионирование
- Опубликованная версия шаблона иммутабельна. Правки создают новую версию. Откат возможен через выбор версии.
- Задача иммутабельна в терминах своего `id`: изменения статуса, атрибутов и результатов логируются как события, но сама строка `tasks` представляет текущую материализованную проекцию.

### 2.3. Async/Sync
- Template Registry — **синхронный** API (файлы + кеш + PostgreSQL индекс; latency ≤ 5 ms на чтение, ≤ 200 ms на `reload()`).
- Task Store — **асинхронный** API (`asyncpg`, async SQLAlchemy). Все операции возвращают awaitable.

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

Макро-фазы пайплайна. Реализация Stage-Gate Manager вне области данных спецификаций; Task Store хранит только строковое значение.

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

Привязка к LangGraph/Mem0/Graphiti находится на уровне бизнес-модулей и Context Engine, а не внутри Template Registry/Task Store. Эти два компонента остаются автономными.

---

## 5. Структура исходников (target)

```
pov_lab/
├── specs/                              # эта спецификация
│   ├── 00_overview.md
│   ├── 01_template_registry.md
│   ├── 02_task_store.md
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
├── migrations/                         # Alembic
└── tests/
```

---

## 6. Сквозные инварианты и правила

### 6.1. Идентичность
Везде, где задача ссылается на шаблон, используется пара `(template_id, template_version)`. Алиас `latest` разрешается в конкретную версию при создании задачи и фиксируется в `tasks.template_version` — это делает задачу воспроизводимой даже после появления новой версии шаблона.

### 6.2. Валидация
- Шаблон перед публикацией проходит валидацию: JSON Schema + структурные правила (см. §4 в `01_template_registry.md`).
- Артефакт перед сохранением в `task_outputs` валидируется по `output_contract` соответствующего шаблона. При невалидности задача → `Failed`.

### 6.3. Observability
Обязательный минимум для обоих компонентов:
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

---

## 7. Минимальный совместный сценарий

```
1. Stage-Gate Manager открывает фазу "requirements" для project P.
2. Task Router создаёт корневую задачу T0 из template "requirements_intake@1.0.0":
     registry.load("requirements_intake", "1.0.0")  # Template Registry
     store.create_task(spec)                         # Task Store → status=Queued
3. Task Router вызывает store.get_ready(project_id=P, limit=1).
4. Task Store помечает T0 как In_Progress (SELECT FOR UPDATE SKIP LOCKED) и возвращает.
5. Бизнес-модуль выполняет задачу, регистрирует артефакты:
     store.mark_completed(T0, outputs=[questionnaire_ref])
6. При динамической декомпозиции:
     store.mark_waiting(parent=T0, children=[T1, T2])   # T0 → Waiting_for_Children
7. После завершения всех children:
     bubble-up: T0 автоматически переходит в Completed, проверка output_contract.
8. Stage-Gate Manager агрегирует по stage_gate колонке, при готовности — закрывает gate.
```

---

## 8. Как читать дальше

- **[01_template_registry.md](01_template_registry.md)** — модель шаблона, хранение, API, валидация, версионирование.
- **[02_task_store.md](02_task_store.md)** — модель задачи, DDL, FSM, API, Event Sourcing, алгоритмы bubble-up и invalidate_subgraph.
- **[schemas/template.schema.json](schemas/template.schema.json)** — исполняемая JSON Schema валидации шаблонов.
- **[schemas/task.schema.json](schemas/task.schema.json)** — JSON Schema payload'ов Task Store.
- **[examples/](examples/)** — эталонные примеры: 3 шаблона (по одному на тип) и 2 задачи (в разных статусах).

Критерии готовности спецификации см. в [плане](../../.claude/plans/snug-wandering-sketch.md), раздел "Проверка готовности".
