# Task Store — спецификация

> **Статус:** v1.0 · Draft · 2026-04-15
> **Зависимости:** [00_overview.md](00_overview.md), [01_template_registry.md](01_template_registry.md)
> **Область:** часть компонента State & Memory Broker из [ТЗ Архитектура.md](ТЗ%20Архитектура.md), отвечающая за задачи (не за артефакты и не за pipeline checkpoints).

---

## 1. Назначение и зона ответственности

### 1.1. Что делает
- Хранит **конкретные экземпляры задач** в рамках проектов.
- Управляет **FSM статусов** задачи согласно ТЗ (7 состояний).
- Хранит **DAG зависимостей** между задачами и входы/выходы-артефакты.
- Ведёт **append-only event log** (source of truth) с материализованными проекциями для быстрых выборок.
- Предоставляет **конкурентно-безопасные** операции: взятие в работу (SKIP LOCKED), bubble-up, инвалидации.

### 1.2. Чего НЕ делает
- Не маршрутизирует задачи (это Task Router — потребитель).
- Не выполняет задачи (это бизнес-модули).
- Не хранит содержимое артефактов (это S3, здесь только ссылки `ArtifactRef`).
- Не резолвит шаблоны (вызывает Template Registry при создании задачи).
- Не собирает контекст (отдаёт `input_requirements` Context Engine).
- Не управляет pipeline-графом LangGraph (checkpoints — отдельная подсистема).

### 1.3. Отношения с соседями

| Компонент | Направление | Контракт |
|---|---|---|
| Template Registry | Task Store → | `load(id, version)` при `create_task` |
| Task Router | → Task Store | `get_ready`, `mark_*`, `create_task` |
| Business Modules | → Task Store | `mark_in_progress/completed/failed/waiting` |
| Context Engine | → Task Store | `get_task_inputs`, `get_outputs_of_ancestors` |
| Stage-Gate Manager | → Task Store | агрегат статусов по `stage_gate` |
| Interruption Gateway | ← Task Store | подписка на `escalation_required` события |
| Artifact Store (S3+pg) | Task Store → | сохранение `ArtifactRef` при `mark_completed` |

---

## 2. Модель данных

### 2.1. DDL (PostgreSQL 15+)

```sql
-- =====================================================
-- Extensions
-- =====================================================
CREATE EXTENSION IF NOT EXISTS pgcrypto;      -- gen_random_uuid (fallback)
-- UUIDv7 генерируется на стороне приложения (uuid-utils).

-- =====================================================
-- Projects
-- =====================================================
CREATE TABLE projects (
    project_id       UUID         PRIMARY KEY,
    name             TEXT         NOT NULL,
    status           TEXT         NOT NULL CHECK (status IN ('active','closed','aborted')),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    created_by       TEXT         NOT NULL,
    closed_at        TIMESTAMPTZ,
    metadata         JSONB        NOT NULL DEFAULT '{}'::jsonb
);

-- =====================================================
-- Tasks (materialised projection)
-- =====================================================
CREATE TABLE tasks (
    task_id             UUID         PRIMARY KEY,
    project_id          UUID         NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    parent_id           UUID         REFERENCES tasks(task_id) ON DELETE RESTRICT,
    template_id         TEXT         NOT NULL,
    template_version    TEXT         NOT NULL,          -- SemVer, иммутабелен
    stage_gate          TEXT         NOT NULL,
    status              TEXT         NOT NULL CHECK (status IN
                                         ('blocked','queued','in_progress',
                                          'completed','failed',
                                          'waiting_for_children','obsolete')),
    priority            INT          NOT NULL DEFAULT 0,           -- FIFO с tiebreaker
    queue_position      BIGSERIAL    UNIQUE,                       -- строгая FIFO-очередь
    attempt             INT          NOT NULL DEFAULT 0,
    escalation_count    INT          NOT NULL DEFAULT 0,
    locked_by           TEXT,                                      -- WorkerId
    locked_at           TIMESTAMPTZ,
    lock_expires_at     TIMESTAMPTZ,                               -- lease
    payload             JSONB        NOT NULL DEFAULT '{}'::jsonb, -- входные данные, резолвленные ссылки
    summary             TEXT,                                      -- короткое человекочитаемое описание
    error               JSONB,                                     -- при Failed: {code, message, details}
    version             INT          NOT NULL DEFAULT 1,           -- optimistic lock
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    CONSTRAINT chk_completed_after_started
        CHECK (completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at),
    CONSTRAINT chk_status_timestamps
        CHECK (
            (status IN ('completed','failed') AND completed_at IS NOT NULL)
            OR status NOT IN ('completed','failed')
        )
);

CREATE INDEX idx_tasks_project_status        ON tasks (project_id, status);
CREATE INDEX idx_tasks_status_queue          ON tasks (queue_position) WHERE status = 'queued';
CREATE INDEX idx_tasks_parent                ON tasks (parent_id) WHERE parent_id IS NOT NULL;
CREATE INDEX idx_tasks_stage_gate            ON tasks (project_id, stage_gate, status);
CREATE INDEX idx_tasks_template              ON tasks (template_id, template_version);
CREATE INDEX idx_tasks_lock_expires          ON tasks (lock_expires_at) WHERE status = 'in_progress';
CREATE INDEX idx_tasks_payload_gin           ON tasks USING GIN (payload jsonb_path_ops);

-- =====================================================
-- Task Dependencies (DAG edges)
-- =====================================================
CREATE TABLE task_dependencies (
    dep_id              UUID         PRIMARY KEY,
    project_id          UUID         NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    from_task_id        UUID         NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,  -- producer
    to_task_id          UUID         NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,  -- consumer
    kind                TEXT         NOT NULL CHECK (kind IN ('hard','soft','semantic')),
    input_name          TEXT         NOT NULL,          -- соответствует InputRequirement.name у consumer'а
    producer_output     TEXT,                           -- имя output у producer (если применимо)
    artifact_ref        UUID,                           -- заполняется после resolve
    resolved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (to_task_id, input_name),                    -- у consumer'а один input_name уникален
    CONSTRAINT chk_self_loop CHECK (from_task_id <> to_task_id)
);

CREATE INDEX idx_task_deps_to     ON task_dependencies (to_task_id);
CREATE INDEX idx_task_deps_from   ON task_dependencies (from_task_id);
CREATE INDEX idx_task_deps_kind   ON task_dependencies (to_task_id, kind);

-- =====================================================
-- Task Inputs / Outputs (artifact refs)
-- =====================================================
CREATE TABLE task_inputs (
    task_input_id       UUID         PRIMARY KEY,
    task_id             UUID         NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    input_name          TEXT         NOT NULL,
    artifact_id         UUID         NOT NULL,           -- ссылка в artifact registry
    artifact_type       TEXT         NOT NULL,
    source              TEXT         NOT NULL CHECK (source IN ('dependency','semantic','bootstrap')),
    resolved_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (task_id, input_name, artifact_id)
);

CREATE TABLE task_outputs (
    task_output_id      UUID         PRIMARY KEY,
    task_id             UUID         NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    output_name         TEXT         NOT NULL,
    artifact_id         UUID         NOT NULL,
    artifact_type       TEXT         NOT NULL,
    schema_valid        BOOLEAN      NOT NULL DEFAULT true,
    produced_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (task_id, output_name, artifact_id)
);

CREATE INDEX idx_task_outputs_task  ON task_outputs (task_id);
CREATE INDEX idx_task_outputs_type  ON task_outputs (artifact_type, produced_at DESC);

-- =====================================================
-- Event Log (append-only, source of truth)
-- =====================================================
CREATE TABLE task_events (
    event_id            UUID         PRIMARY KEY,
    project_id          UUID         NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    task_id             UUID         NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    sequence_number     BIGSERIAL    UNIQUE,                   -- монотонно растёт
    event_type          TEXT         NOT NULL,                 -- см. §6.1
    payload             JSONB        NOT NULL,
    actor               TEXT         NOT NULL,
    correlation_id      UUID,
    occurred_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    causation_id        UUID         REFERENCES task_events(event_id)
);

CREATE INDEX idx_events_task_seq    ON task_events (task_id, sequence_number);
CREATE INDEX idx_events_project_seq ON task_events (project_id, sequence_number);
CREATE INDEX idx_events_type        ON task_events (event_type, occurred_at DESC);
CREATE INDEX idx_events_correlation ON task_events (correlation_id) WHERE correlation_id IS NOT NULL;
-- Запрет UPDATE и DELETE средствами триггера (§2.2)

-- =====================================================
-- Status Transitions (быстрые выборки для UI/метрик)
-- =====================================================
CREATE TABLE task_status_transitions (
    transition_id       UUID         PRIMARY KEY,
    task_id             UUID         NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    from_status         TEXT,                                  -- NULL для первой записи
    to_status           TEXT         NOT NULL,
    reason              TEXT,
    actor               TEXT         NOT NULL,
    event_id            UUID         NOT NULL REFERENCES task_events(event_id) ON DELETE RESTRICT,
    transitioned_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX idx_transitions_task   ON task_status_transitions (task_id, transitioned_at);
CREATE INDEX idx_transitions_to     ON task_status_transitions (to_status, transitioned_at DESC);

-- =====================================================
-- Subscriptions (для внешних потребителей событий)
-- =====================================================
CREATE TABLE event_subscriptions (
    subscription_id     UUID         PRIMARY KEY,
    subscriber          TEXT         NOT NULL,                 -- "interruption_gateway"
    event_types         TEXT[]       NOT NULL,                 -- ['escalation_required','task_failed']
    last_seen_sequence  BIGINT       NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now()
);
```

### 2.2. Триггеры
```sql
-- Запрет UPDATE/DELETE на task_events
CREATE OR REPLACE FUNCTION task_events_immutable() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'task_events is append-only (op=%, event_id=%)',
                    TG_OP, OLD.event_id;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_task_events_no_update BEFORE UPDATE ON task_events
    FOR EACH ROW EXECUTE FUNCTION task_events_immutable();
CREATE TRIGGER trg_task_events_no_delete BEFORE DELETE ON task_events
    FOR EACH ROW EXECUTE FUNCTION task_events_immutable();

-- updated_at на tasks
CREATE OR REPLACE FUNCTION tasks_set_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_tasks_updated_at BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION tasks_set_updated_at();
```

### 2.3. Обоснование схемы
| Решение | Причина |
|---|---|
| `UUIDv7` | сортируется по времени, хорошо ведёт себя в B-tree индексах |
| `BIGSERIAL queue_position` | строгая FIFO без race conditions |
| Partial index `WHERE status='queued'` | быстрый dispatcher даже при миллионах задач |
| Отдельная `task_events` + триггер-иммутабельность | Event Sourcing: реплей полностью восстанавливает `tasks` |
| `locked_by/locked_at/lock_expires_at` | механизм lease: если worker умер, lock автоматически отпускается |
| `version INT` | optimistic locking для защиты FSM-переходов |
| JSONB `payload/metadata/error` | гибкость при сохранении полноценных типов (валидируется на application-level) |
| GIN `payload` | поиск по полям в payload для отладки и аналитики |

---

## 3. FSM статусов

### 3.1. Диаграмма переходов

```
                ┌──────────────┐
                │   Blocked    │─── hard deps completed ──┐
                └──────┬───────┘                          │
                       │                                  ▼
                       │                          ┌──────────────┐
                       │                          │   Queued     │
                       │                          └──────┬───────┘
                       │                                 │ Router picks (SKIP LOCKED)
                       │                                 ▼
                       │                          ┌──────────────┐
                       │              ┌───────────│  In_Progress │──── exec ok ──┐
                       │              │           └──────┬───────┘               │
                       │              │                  │ dynamic decompose     │
                       │              │                  ▼                       │
                       │              │     ┌──────────────────────┐             │
                       │              │     │ Waiting_for_Children │             │
                       │              │     └────────┬─────────────┘             │
                       │              │              │ all children completed    │
                       │              │              ▼                           ▼
                       │              │        ┌────────────┐            ┌────────────┐
                       │              │        │ Completed  │◄───────────│ Completed  │
                       │              │        └────────────┘            └────────────┘
                       │              │ limit exceeded
                       │              ▼
                       │        ┌────────────┐
                       │        │   Failed   │
                       │        └────────────┘
                       │
                       ▼  replan / parent obsolete
                 ┌──────────────┐
                 │   Obsolete   │ (terminal)
                 └──────────────┘
```

### 3.2. Таблица переходов

| From | To | Кто вызывает | Предусловия | События | Ошибки |
|---|---|---|---|---|---|
| `(none)` | `blocked` | Task Store (`create_task`) | у задачи есть хотя бы один `hard` dep в статусе ≠ `completed` | `task_created`, `status_changed` | `ValidationError` при невалидной спеке |
| `(none)` | `queued` | Task Store (`create_task`) | все `hard` deps уже `completed` (или их нет) | `task_created`, `status_changed` | — |
| `blocked` | `queued` | Task Store (`on_dependency_completed`, триггерится из `mark_completed` producer'а) | все `hard` deps completed | `dependencies_satisfied`, `status_changed` | — |
| `queued` | `in_progress` | Task Router (`mark_in_progress`) | `SELECT FOR UPDATE SKIP LOCKED`; worker подписался | `task_taken`, `status_changed` | `ConflictError` если уже заблокирована |
| `in_progress` | `completed` | Business Module (`mark_completed`) | все `required` `output_contract` заполнены и валидны; не превышены лимиты | `outputs_registered`, `status_changed` | `ValidationError` при невалидных outputs |
| `in_progress` | `waiting_for_children` | Business Module (`mark_waiting`) | шаблон `dynamic` или `composite` с динамическим child'ом; создан список children | `children_created`, `status_changed` | `ConflictError` если шаблон `executable` без декомпозиции |
| `waiting_for_children` | `completed` | Task Store (bubble-up, `on_child_completed`) | все children в `completed` И bubble-up выходы валидны | `children_completed`, `outputs_registered`, `status_changed` | `ValidationError` |
| `waiting_for_children` | `failed` | Task Store (bubble-up) | любой `hard`-child в `failed` И нет recovery-политики | `child_failed`, `status_changed` | — |
| `in_progress` | `failed` | Business Module / Task Store | превышены лимиты, output-контракт не собрать | `task_failed`, `escalation_required`, `status_changed` | — |
| `blocked` | `failed` | Task Store | любой `hard` dep перешёл в `failed`/`obsolete` и нет recovery | `dependency_failed`, `status_changed` | — |
| `any non-terminal` | `obsolete` | Task Store (`invalidate_subgraph`) | предок/источник артефакта инвалидирован | `invalidated`, `status_changed` | — |
| `in_progress` | `queued` | Task Store (lease expired) | `lock_expires_at < now()` И `attempt < max_attempts` | `lock_expired`, `status_changed` | — |

### 3.3. Терминальные статусы
`completed`, `failed`, `obsolete` — терминальные. Из них **нет** переходов, кроме инвалидации: `completed/failed → obsolete`.

### 3.4. Инварианты FSM
| I# | Инвариант |
|---|---|
| I1 | `status = 'in_progress'` ⇒ `locked_by IS NOT NULL AND locked_at IS NOT NULL` |
| I2 | `status = 'queued'` ⇒ `locked_by IS NULL` |
| I3 | `status = 'completed'` ⇒ для каждого `required=true` output'а есть запись в `task_outputs` |
| I4 | `status = 'blocked'` ⇒ существует хотя бы один `hard` dep, у producer'а которого `status ≠ completed` |
| I5 | `status = 'waiting_for_children'` ⇒ существует хотя бы один child (`parent_id = this.task_id`) |
| I6 | `attempt > 0` ⇒ в event log есть хотя бы одна запись `task_retried` |
| I7 | `escalation_count > escalation.max_attempts` ⇒ `status IN ('failed','obsolete')` |
| I8 | DAG `task_dependencies` ацикличен в пределах проекта |

Инвариант I8 проверяется при каждом добавлении ребра: если новое ребро создаёт цикл → `IntegrityError`.

---

## 4. Python API

### 4.1. Протокол

```python
# src/pov_lab_tasks/ports.py
class TaskStore(Protocol):
    # ---- Создание ----
    async def create_project(self, spec: NewProjectSpec) -> Project: ...
    async def create_task(self, spec: NewTaskSpec) -> Task: ...
    async def create_subgraph(
        self,
        parent_id: TaskId | None,
        tasks: list[NewTaskSpec],           # упорядочен, порядок = FIFO внутри batch
        dependencies: list[NewDependencySpec],
    ) -> list[Task]: ...                    # атомарно

    # ---- Чтение ----
    async def get(self, task_id: TaskId) -> Task: ...
    async def list_by_project(
        self,
        project_id: ProjectId,
        *,
        status: TaskStatus | set[TaskStatus] | None = None,
        stage_gate: StageGate | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Task]: ...
    async def get_ready(
        self,
        *,
        project_id: ProjectId | None = None,
        limit: int = 1,
        lock_ttl_seconds: int = 300,
        worker: WorkerId,
    ) -> list[Task]: ...                    # одновременно переводит в in_progress
    async def get_children(self, task_id: TaskId) -> list[Task]: ...
    async def get_ancestors(self, task_id: TaskId) -> list[Task]: ...
    async def get_dependencies(self, task_id: TaskId) -> list[TaskDependency]: ...
    async def get_dependents(self, task_id: TaskId) -> list[TaskDependency]: ...

    # ---- FSM-переходы ----
    async def mark_in_progress(
        self,
        task_id: TaskId,
        worker: WorkerId,
        expected_version: int,
        lock_ttl_seconds: int = 300,
    ) -> Task: ...
    async def heartbeat(self, task_id: TaskId, worker: WorkerId) -> Task: ...  # продлевает lease
    async def mark_completed(
        self,
        task_id: TaskId,
        outputs: list[TaskOutputSpec],
        worker: WorkerId,
        expected_version: int,
    ) -> Task: ...
    async def mark_failed(
        self,
        task_id: TaskId,
        reason: FailureReason,
        worker: WorkerId,
        expected_version: int,
    ) -> Task: ...
    async def mark_waiting(
        self,
        task_id: TaskId,
        children: list[NewTaskSpec],
        dependencies: list[NewDependencySpec],
        worker: WorkerId,
        expected_version: int,
    ) -> list[Task]: ...
    async def invalidate_subgraph(
        self,
        task_id: TaskId,
        reason: str,
        actor: str,
    ) -> InvalidationReport: ...
    async def retry(self, task_id: TaskId, actor: str) -> Task: ...  # Failed → Queued (если attempt < max)

    # ---- Артефакты ----
    async def add_input(
        self,
        task_id: TaskId,
        input_name: str,
        artifact_ref: ArtifactRef,
        source: InputSource,
    ) -> TaskInput: ...
    async def get_inputs(self, task_id: TaskId) -> list[TaskInput]: ...
    async def get_outputs(self, task_id: TaskId) -> list[TaskOutput]: ...

    # ---- Event log ----
    async def events(
        self,
        task_id: TaskId,
        *,
        after_sequence: int | None = None,
        types: set[str] | None = None,
    ) -> list[TaskEvent]: ...
    async def subscribe(
        self,
        subscriber: str,
        event_types: list[str],
    ) -> EventStream: ...                   # yields TaskEvent

    # ---- Housekeeping ----
    async def reap_expired_leases(self) -> int: ...  # фоновая задача, возвращает кол-во reaped

    # ---- Агрегаты ----
    async def stage_gate_summary(self, project_id: ProjectId) -> StageGateSummary: ...
```

### 4.2. Спецификации входов

```python
class NewProjectSpec(BaseModel):
    name: str
    created_by: str
    metadata: dict[str, Any] = {}

class NewTaskSpec(BaseModel):
    project_id: ProjectId
    parent_id: TaskId | None = None
    template_ref: TemplateRef              # (id, version|"latest")
    stage_gate: StageGate
    payload: dict[str, Any] = {}
    summary: str | None = None
    priority: int = 0
    correlation_id: UUID | None = None
    actor: str

class NewDependencySpec(BaseModel):
    from_task_id: TaskId
    to_task_id: TaskId
    kind: RequirementKind
    input_name: str                        # у consumer
    producer_output: str | None = None     # у producer

class TaskOutputSpec(BaseModel):
    output_name: str
    artifact_ref: ArtifactRef
    schema_valid: bool                     # валидация выполнена вызывающей стороной

class FailureReason(BaseModel):
    code: FailureCode                      # enum (см. §4.4)
    message: str
    details: dict[str, Any] = {}
    llm_trace_ref: ArtifactRef | None = None
```

### 4.3. Основная реализация
```python
class PostgresTaskStore(TaskStore):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        registry: TemplateRegistry,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ): ...
```

- SQLAlchemy 2.0 Core + ORM по месту.
- Все операции — `AsyncSession` с явным `await session.commit()`.
- `get_ready()` использует raw SQL (для `FOR UPDATE SKIP LOCKED` и оптимизации).

### 4.4. Коды ошибок исполнения
```python
class FailureCode(StrEnum):
    LLM_LIMIT_EXCEEDED = "llm_limit_exceeded"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
    WALL_CLOCK_EXCEEDED = "wall_clock_exceeded"
    OUTPUT_VALIDATION_FAILED = "output_validation_failed"
    HARD_INPUT_MISSING = "hard_input_missing"
    EXECUTOR_CRASHED = "executor_crashed"
    DEPENDENCY_FAILED = "dependency_failed"
    ESCALATION_REQUIRED = "escalation_required"
    UNKNOWN = "unknown"
```

---

## 5. Алгоритмы

### 5.1. `create_task`
```
1. registry.load(template_ref) → template                      (если "latest" → резолвится в конкретную версию)
2. Валидация: stage_gate из спеки = template.metadata.stage_gate (или subset согласно §5.6)
3. INSERT INTO tasks(..., status='blocked', version=1, ...)
4. FOR EACH input_requirement:
     IF kind='hard' AND есть известный producer (NewTaskSpec родом из composite parent):
         INSERT INTO task_dependencies(...)                     (может быть несколько)
5. COMPUTE initial_status:
     IF нет hard-deps OR все hard-deps completed → 'queued'
     ELSE → 'blocked'
6. UPDATE tasks SET status = initial_status, queue_position = default WHERE id = ...
7. INSERT INTO task_events (task_created, status_changed)      (атомарно в той же TX)
8. INSERT INTO task_status_transitions
9. COMMIT
```

### 5.2. `get_ready` (Task Router вызывает)
```sql
WITH candidate AS (
    SELECT task_id, version
      FROM tasks
     WHERE status = 'queued'
       AND (:project_id IS NULL OR project_id = :project_id)
     ORDER BY priority DESC, queue_position ASC
     LIMIT :limit
     FOR UPDATE SKIP LOCKED
)
UPDATE tasks t
   SET status = 'in_progress',
       locked_by = :worker,
       locked_at = now(),
       lock_expires_at = now() + make_interval(secs => :lock_ttl_seconds),
       started_at = COALESCE(t.started_at, now()),
       attempt = t.attempt + 1,
       version = t.version + 1
  FROM candidate c
 WHERE t.task_id = c.task_id
RETURNING t.*;
```
Далее — `INSERT INTO task_events` и `task_status_transitions` для каждой взятой задачи (в той же транзакции).

### 5.3. `mark_completed`
```
1. SELECT tasks FOR UPDATE WHERE task_id = :id AND version = :expected_version
     → если нет строки → ConflictError
     → если status ≠ 'in_progress' → ConflictError
2. Резолв template_ref = (task.template_id, task.template_version)
3. Template.output_contract → FOR EACH required=true output:
     найти соответствие в TaskOutputSpec[] → иначе ValidationError
4. INSERT INTO task_outputs ...
5. UPDATE tasks SET status = 'completed', completed_at = now(), locked_by = NULL, locked_at = NULL,
                     version = version+1
6. INSERT INTO task_events (outputs_registered, status_changed)
7. Bubble-up:
     IF parent_id IS NOT NULL → evaluate_parent_completion(parent_id)
     FOR EACH dependent IN task_dependencies WHERE from_task_id = :id:
         evaluate_consumer_readiness(dependent.to_task_id)
8. COMMIT
```

### 5.4. `evaluate_consumer_readiness(task_id)`
```
SELECT COUNT(*) FROM task_dependencies d
  JOIN tasks p ON p.task_id = d.from_task_id
 WHERE d.to_task_id = :task_id AND d.kind = 'hard'
   AND p.status <> 'completed';
IF count = 0 AND tasks.status = 'blocked' → UPDATE tasks SET status='queued', version+=1
                                          INSERT task_events(dependencies_satisfied, status_changed)
```

### 5.5. `evaluate_parent_completion(parent_id)` — bubble-up
```
parent = SELECT * FROM tasks WHERE task_id = :parent_id FOR UPDATE
IF parent.status ≠ 'waiting_for_children' → return

children = SELECT * FROM tasks WHERE parent_id = :parent_id
all_completed = все children в ('completed','obsolete')
any_hard_failed = существует child в 'failed' с kind='hard' в dependency от parent

IF any_hard_failed:
    UPDATE parent SET status='failed', error = {...}
    INSERT task_events(child_failed, status_changed)
    propagate_failure(parent.parent_id)
ELIF all_completed:
    Соберём bubble_up_outputs из composite template:
        для каждого output_contract parent'а:
            найти child+output согласно template.composite.bubble_up_outputs
            INSERT INTO task_outputs(parent.task_id, output_name, artifact_ref)
    UPDATE parent SET status='completed', completed_at=now()
    INSERT task_events(children_completed, outputs_registered, status_changed)
    (рекурсивно bubble-up для parent.parent_id)
```

### 5.6. `mark_waiting`
```
1. SELECT task FOR UPDATE WHERE task_id = :id AND version = :expected_version
2. Если template.type = 'executable' → ConflictError
3. FOR EACH new_child IN children:
     new_child.parent_id := :id
     create_task(new_child)    (рекурсивно, в той же TX)
4. FOR EACH dep IN dependencies:
     INSERT INTO task_dependencies(...)
     (проверка: не создаёт цикл — §5.9)
5. UPDATE tasks SET status='waiting_for_children', locked_by=NULL, ..., version+=1
6. INSERT task_events(children_created, status_changed)
7. COMMIT
```

### 5.7. `invalidate_subgraph`
```
seed = :task_id
visited = {}
queue = [seed]
WHILE queue не пусто:
    t = queue.pop_front()
    IF t in visited → continue
    visited.add(t)
    SELECT task_id FROM tasks WHERE task_id = t FOR UPDATE
    IF task.status in ('obsolete'): continue
    UPDATE tasks SET status='obsolete', version+=1
    INSERT task_events(invalidated {reason, source=seed}, status_changed)
    children = SELECT task_id FROM tasks WHERE parent_id = t
    downstream = SELECT to_task_id FROM task_dependencies WHERE from_task_id = t
    queue.extend(children ∪ downstream)
COMMIT
RETURN InvalidationReport(seed, affected=visited)
```

### 5.8. `retry`
```
1. SELECT task FOR UPDATE WHERE task_id = :id
2. IF status ≠ 'failed' → ConflictError
3. IF attempt >= template.escalation.max_attempts → ConflictError
4. UPDATE tasks SET status='queued', locked_by=NULL, error=NULL, version+=1,
                     queue_position = DEFAULT
5. INSERT task_events(task_retried, status_changed)
6. COMMIT
```

### 5.9. Проверка ацикличности при добавлении ребра
```
Перед INSERT INTO task_dependencies(from=F, to=T):
  Существует ли путь T →* F по существующим ребрам?
  WITH RECURSIVE reach(node) AS (
      SELECT to_task_id FROM task_dependencies WHERE from_task_id = :T
      UNION
      SELECT d.to_task_id FROM task_dependencies d JOIN reach r ON d.from_task_id = r.node
  ) SELECT 1 FROM reach WHERE node = :F LIMIT 1;
  Если есть → IntegrityError (cycle).
```
Оптимизация: LIMIT глубины равной `settings.MAX_DAG_DEPTH` (по умолчанию 32), при превышении → ошибка.

### 5.10. `reap_expired_leases`
Фоновая задача (запускается отдельным воркером или pg-cron):
```sql
WITH expired AS (
    SELECT task_id FROM tasks
     WHERE status = 'in_progress'
       AND lock_expires_at < now()
     FOR UPDATE SKIP LOCKED
)
UPDATE tasks t
   SET status = 'queued',
       locked_by = NULL,
       locked_at = NULL,
       lock_expires_at = NULL,
       version = version + 1
  FROM expired e
 WHERE t.task_id = e.task_id;
```
Каждую возвращённую строку сопровождает `INSERT INTO task_events(lock_expired)`.

---

## 6. Event Sourcing

### 6.1. Перечень событий

| `event_type` | Когда | Обязательные поля `payload` |
|---|---|---|
| `task_created` | при `create_task` | `template_id`, `template_version`, `parent_id` |
| `status_changed` | при каждом FSM-переходе | `from`, `to`, `reason?` |
| `dependencies_satisfied` | `blocked → queued` | `resolved_deps: [dep_id]` |
| `dependency_failed` | при каскаде от failed producer | `producer_task_id`, `dep_id` |
| `task_taken` | `queued → in_progress` | `worker`, `lock_ttl_seconds` |
| `heartbeat` | продление lease | `worker`, `new_expires_at` |
| `lock_expired` | reap | `previous_worker` |
| `outputs_registered` | при `mark_completed` | `outputs: [{name, artifact_id}]` |
| `children_created` | `mark_waiting` | `children: [task_id]`, `deps: [dep_id]` |
| `children_completed` | bubble-up, все children готовы | `children: [task_id]` |
| `child_failed` | bubble-up при hard fail | `child_task_id`, `code` |
| `task_retried` | `retry` | `attempt`, `previous_error` |
| `task_failed` | `* → failed` | `code`, `message`, `details` |
| `escalation_required` | вместе с `task_failed` когда `escalation_count+1 > limit` | `reason` |
| `invalidated` | `invalidate_subgraph` | `seed_task_id`, `reason` |
| `input_resolved` | запись в `task_inputs` | `input_name`, `artifact_id`, `source` |

### 6.2. Правила
- Все FSM-методы в одной транзакции: `UPDATE tasks` + `INSERT task_events` + `INSERT task_status_transitions`.
- `causation_id` связывает события причинно-следственно (например, `child_failed` ← `dependency_failed`).
- `correlation_id` — единый id для цепочки связанных операций (trace-id).
- `sequence_number` — глобальный порядок через BIGSERIAL.
- Реплей: из `task_events` по `task_id ORDER BY sequence_number` вычисляется итоговое состояние (`tasks` строка). Соответствие проверяется отдельным инструментом `pov-lab-tasks replay-check`.

### 6.3. Потребители событий
- **Interruption Gateway**: подписан на `escalation_required`, `task_failed` с определёнными кодами.
- **Metrics exporter**: подписан на `status_changed` для метрик.
- **Audit log**: все события архивируются в долговременное хранилище (вне этой спеки).

Механизм доставки: `LISTEN/NOTIFY` на канал `task_events_channel`, полезная нагрузка — `{event_id, event_type, task_id}`. Потребитель читает строку из `task_events` по id.

---

## 7. Политики lease и конкурентности

### 7.1. Lease
- При `mark_in_progress` задача получает lease на `lock_ttl_seconds` (по умолчанию 300 с).
- Worker должен периодически вызывать `heartbeat` (по умолчанию — каждые 60 с).
- `heartbeat` продлевает `lock_expires_at` на `lock_ttl_seconds` от текущего момента.
- Если worker умер, `reap_expired_leases` возвращает задачу в `queued`.
- Повторный dispatch увеличивает `attempt`. При `attempt >= max_attempts` задача сразу → `failed` с кодом `executor_crashed`.

### 7.2. Optimistic locking
- Каждая запись `tasks` имеет `version INT`.
- Все `mark_*` методы принимают `expected_version`. Несоответствие → `ConflictError`.
- Вызывающая сторона должна перечитать `Task` и решить: retry операции, abort, или вмешательство оператора.

### 7.3. Транзакционная изоляция
- Уровень — `READ COMMITTED` (default). Строковые локи через `FOR UPDATE` достаточны.
- Для `invalidate_subgraph` используется advisory lock на `project_id` для предотвращения параллельных инвалидаций: `SELECT pg_advisory_xact_lock(hashtext('invalidate:' || project_id))`.

---

## 8. Интеграция с Template Registry

### 8.1. Резолвинг шаблона
- `create_task(NewTaskSpec{template_ref=(id, "latest")})` — Task Store вызывает `registry.resolve(TemplateRef("latest"))`, получает конкретную `version`, сохраняет её в `tasks.template_version`.
- Дальнейшее использование — всегда по `(template_id, template_version)`, даже если в реестре вышла новая версия. Это обеспечивает воспроизводимость.

### 8.2. Использование `input_requirements`
При `create_task`:
```
FOR EACH ir IN template.input_requirements:
    MATCH ir.selector:
        case SelectorByArtifactId:
            INSERT task_inputs (task_id, ir.name, ir.artifact_id, source='bootstrap')
        case SelectorByProducer | SelectorByType:
            IF найден producer в рамках parent subgraph:
                INSERT task_dependencies (from=producer, to=this, kind=ir.kind, input_name=ir.name)
            ELIF ir.kind = 'hard':
                FAIL task immediately (code=HARD_INPUT_MISSING) — до вызова LLM
            ELSE (kind='soft'):
                оставить без зависимости — Context Engine попробует найти на этапе исполнения
        case SelectorSemantic:
            оставить без зависимости — Context Engine выполнит поиск
```

### 8.3. Использование `output_contract`
При `mark_completed` Task Store **не** валидирует содержимое артефактов (это задача вызывающего), но:
- Проверяет, что все `required=true` выходы присутствуют по `output_name`.
- Проверяет, что `artifact_ref.artifact_type` совпадает с `output_contract[*].artifact_type`.
- Запись `task_outputs.schema_valid = true` означает, что вызывающий подтвердил валидацию. Task Store просто сохраняет.

### 8.4. Использование `escalation`
При `mark_failed`:
```
task.escalation_count += 1
IF task.escalation_count > template.escalation.max_attempts:
    INSERT task_events(escalation_required, payload={reason=failure_reason})
    → Interruption Gateway получает и принимает решение (retry/human/abort)
```

---

## 9. Интеграция со Stage-Gate Manager

```python
class StageGateSummary(BaseModel):
    project_id: ProjectId
    per_gate: dict[StageGate, StageGateStats]

class StageGateStats(BaseModel):
    total: int
    by_status: dict[TaskStatus, int]
    earliest_created_at: datetime | None
    latest_completed_at: datetime | None
    blocking_tasks: list[TaskSummary]        # tasks в failed/blocked долго
```

Метод `stage_gate_summary`:
```sql
SELECT stage_gate, status, COUNT(*) FROM tasks WHERE project_id = :pid GROUP BY 1,2;
```
Плюс дополнительные выборки для `blocking_tasks`.

Stage-Gate Manager закрывает gate, когда для текущего `stage_gate`:
- Все задачи в `{completed, obsolete}`.
- Нет задач в `failed`, кроме тех, для которых эскалация одобрена оператором.

---

## 10. Observability

### 10.1. Метрики (Prometheus)
| Метрика | Тип | Labels |
|---|---|---|
| `pov_tasks_created_total` | counter | `project_id`, `template_id`, `template_version` |
| `pov_tasks_transitioned_total` | counter | `from`, `to`, `template_id` |
| `pov_tasks_by_status` | gauge | `project_id`, `status`, `stage_gate` |
| `pov_tasks_queue_depth` | gauge | `project_id` |
| `pov_tasks_queue_wait_seconds` | histogram | — |
| `pov_tasks_execution_seconds` | histogram | `template_id` |
| `pov_tasks_escalations_total` | counter | `code` |
| `pov_tasks_invalidations_total` | counter | `reason` |
| `pov_tasks_leases_expired_total` | counter | — |
| `pov_tasks_dag_depth` | histogram | `project_id` |
| `pov_tasks_bubbleup_duration_seconds` | histogram | — |
| `pov_tasks_optimistic_lock_conflicts_total` | counter | `method` |

### 10.2. Логи
Пример:
```json
{
  "timestamp": "2026-04-15T13:05:42Z",
  "level": "info",
  "component": "task_store",
  "action": "mark_completed",
  "project_id": "...",
  "task_id": "...",
  "template_id": "rag_index_build",
  "template_version": "1.2.0",
  "worker": "codegen_agent:a1b2",
  "duration_ms": 47,
  "correlation_id": "..."
}
```

### 10.3. Tracing
- Span name: `task_store.<method>`.
- Аттрибуты: `task.id`, `task.status.from/to`, `template.ref`, `project.id`.
- Каждое событие `task_events` также создаёт span event для сквозного трейсинга.

---

## 11. Тестовая стратегия

### 11.1. Unit (без БД)
- FSM-валидатор: позитив/негатив для каждого перехода.
- Builders для `NewTaskSpec`/`NewDependencySpec`.
- Алгоритм bubble-up на мок-задачах.
- Parser/validator выражений (если вынесен).

### 11.2. Integration (Testcontainers Postgres)
- Полный цикл: `create_project` → `create_task` × N → `get_ready` → `mark_completed` → bubble-up.
- Конкурентный `get_ready` (N worker'ов): каждая задача взята ровно один раз.
- Lease expiry: искусственно сдвигаем `clock`, вызываем `reap_expired_leases`.
- `invalidate_subgraph` на графе 50 узлов — проверка корректности и за < 1 с.
- Детектор циклов: попытка создать цикл → `IntegrityError`.
- Event replay: восстановить состояние только из `task_events` = текущее состояние `tasks`.

### 11.3. Property-based (Hypothesis)
- Генератор случайных DAG'ов (≤ 30 узлов, ≤ 60 рёбер) → проверка:
  - ацикличности после добавления случайной последовательности рёбер;
  - что bubble-up терминирует за ≤ depth шагов;
  - что после `invalidate_subgraph(root)` все транзитивные потомки — `obsolete`.

### 11.4. Нагрузка
- pgbench-подобный сценарий: 1000 задач/сек на `get_ready` на машине 8 vCPU / 32 GB / PG 15 (локально).
- p95 `get_ready` ≤ 20 ms.
- p95 `mark_completed` ≤ 50 ms (без учёта bubble-up на глубокое дерево).

---

## 12. Производительность и ограничения

| Параметр | Лимит |
|---|---|
| Задач в одном проекте | до 100 000 |
| Глубина DAG | ≤ 32 |
| Children одной задачи | ≤ 50 |
| Input deps одной задачи | ≤ 50 |
| Размер `payload` | ≤ 1 MiB (валидируется) |
| Сохранение задач ≥ 90 дней после `completed` | хранятся со всеми событиями; архив — отдельная спецификация |
| `task_events` per task | практически без ограничения, но warnings при > 1000 |

### 12.1. Партиционирование (на перспективу)
`task_events` партиционируется по `occurred_at` (по месяцам) при превышении 100M строк. Миграция — отдельным RFC.

---

## 13. Ошибки

```python
# src/pov_lab_tasks/errors.py
class TaskStoreError(PovLabError): ...
class TaskNotFoundError(TaskStoreError, NotFoundError): ...
class InvalidTransitionError(TaskStoreError, ConflictError): ...
class DagIntegrityError(TaskStoreError, IntegrityError): ...      # циклы, превышение глубины
class OptimisticLockError(TaskStoreError, ConflictError): ...
class DependencyUnresolvedError(TaskStoreError, IntegrityError): ...
class LeaseExpiredError(TaskStoreError, ConflictError): ...
class OutputContractViolationError(TaskStoreError, ValidationError): ...
class InvalidPayloadError(TaskStoreError, ValidationError): ...
```

---

## 14. Миграции (Alembic)

Структура:
```
migrations/tasks/
    env.py
    versions/
        001_projects.py
        002_tasks.py
        003_task_dependencies.py
        004_task_inputs_outputs.py
        005_task_events_and_transitions.py
        006_event_subscriptions.py
        007_triggers.py
```

Правила:
- Одна миграция = одна таблица (или одна логическая группа).
- Downgrade обязателен до версии `007` включительно.
- После `007` добавление новых миграций без downgrade-ов запрещено (seal-point).

---

## 15. Конфигурация

```toml
# config/task_store.toml
[database]
dsn = "postgresql+asyncpg://pov:...@db:5432/pov_lab"
pool_size = 20
max_overflow = 10

[lease]
default_ttl_seconds = 300
heartbeat_interval_seconds = 60
reaper_interval_seconds = 30

[limits]
max_dag_depth = 32
max_children_per_task = 50
max_deps_per_task = 50
max_payload_bytes = 1048576
default_escalation_max_attempts = 3

[events]
notify_channel = "task_events_channel"

[telemetry]
service_name = "pov-lab-task-store"
```

---

## 16. CLI и инструменты

```
pov-lab-tasks show <task_id>
pov-lab-tasks list --project <pid> [--status queued]
pov-lab-tasks dag <task_id> [--format dot]
pov-lab-tasks events <task_id> [--after-sequence N]
pov-lab-tasks replay-check <project_id>     # проверяет, что tasks = replay(events)
pov-lab-tasks reap                          # принудительный reap expired leases
pov-lab-tasks invalidate <task_id> --reason "..."
```

---

## 17. Что вне области этой спеки

- **Артефактный сервис** (хранение blob'ов в S3, метаданные артефактов) — отдельная спецификация. Здесь только ссылки.
- **Context Engine** — как резолвит `soft`/`semantic` требования. Task Store только фиксирует `input_resolved` события.
- **LangGraph-интеграция**: конкретные узлы pipeline-графа. Task Store предоставляет API; узел — потребитель.
- **Пользовательский интерфейс** для задач.
- **Распределённая доставка событий** поверх `LISTEN/NOTIFY` (Kafka/NATS) — адресуется отдельным RFC.
- **Retention / архивирование** старых задач и событий.
