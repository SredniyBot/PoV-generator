## 1. Consistency analysis

Наследуются без переименования и без дублирования: двухуровневая оркестрация `macro stage-gate + micro DAG event loop`, фиксированные типы шаблонов `composite / executable / dynamic`, семисостояний lifecycle задачи, контрактная модель зависимостей `hard / soft / semantic`, эскалация через `Interruption Gateway`, изоляция LLM/агентов как строго типизированных вычислительных узлов. Новые модули продолжают эти контракты, а не создают параллельные.

Из Vision обязательны прозрачность, воспроизводимость, управляемость, самоконтроль и доменная нейтральность beyond DS/RAG/simple ML. Поэтому `Spec A` отделяет pure transition rules от persistence/scheduling, а `Spec B` изолирует vendor/runtime-специфику на уровне adapters/capabilities и делает trace/provenance first-class contract.

Из existing specs 2/3 сохраняются design conventions:

* `pydantic v2`, `StrEnum`, `Protocol`, `snake_case`, UTC/ISO-8601, UUIDv7, SemVer.
* `Template Registry` — source of truth для типа/контракта задачи.
* `Task Store` — source of truth для task lifecycle и event log.
* `task_events` append-only; `tasks` и `task_status_transitions` — проекции.
* `latest` всегда резолвится в конкретную версию при создании задачи.
* `ArtifactRef`, `SchemaRef`, `TemplateRef`, `Provenance`, `TaskStatus`, `TemplateType`, `RequirementKind`, `StageGate`, `FailureCode` не меняются.

Обязательное для совместимости:

| Область                    | Что сохраняется                                                                                                                      |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| Shared enums/types         | `TaskStatus`, `TemplateType`, `RequirementKind`, `StageGate`, `FailureCode`, `ArtifactRef`, `Provenance`, `TemplateRef`, `SchemaRef` |
| Persistence model          | `task_events` append-only, replay должен восстанавливать `tasks`                                                                     |
| Queue semantics            | `priority DESC, queue_position ASC`, `SKIP LOCKED`, lease/heartbeat                                                                  |
| Output validation boundary | Task Store проверяет наличие/тип/contract match outputs, но не содержимое артефакта                                                  |
| Escalation boundary        | Task Store эмитит `escalation_required`; решение о human handoff не принимает FSM и не принимает LLM Gateway                         |
| LLM isolation              | business modules не должны видеть vendor-specific transport contracts                                                                |

---

## 2. Assumptions / resolved ambiguities

| Неоднозначность                                                                                                                | Принятое решение                                                                                                                                                       | Compatibility impact                                            |
| ------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| `02_task_store` уже содержит FSM-логику                                                                                        | `Spec A` становится каноническим pure-domain progression engine; `Task Store` остаётся persistence/apply layer                                                         | Внешний API Task Store сохраняется, внутренняя логика выносится |
| `composite` в `01_template_registry` объявлен неисполняемым контейнером, но `02_task_store` допускает путь через `in_progress` | `composite` task никогда не попадает в `queued/in_progress`; при создании parent сразу фиксируется в `waiting_for_children`, а child subgraph материализуется атомарно | Это корректирующее уточнение, согласованное с архитектурой      |
| Для `dynamic` parent текущая bubble-up логика завершает parent автоматически                                                   | Для `dynamic`: после завершения всех blocking children parent переходит `waiting_for_children -> queued` и повторно берётся в работу для finalize phase                | Требуется расширение transition table                           |
| `failed` объявлен terminal, но есть `retry()`                                                                                  | `failed` terminal для execution epoch, но не для administrative lifecycle; `retry` открывает новый execution epoch на том же `task_id`                                 | Поведение `retry()` сохраняется, но формализуется               |
| Какие children считаются blocking                                                                                              | В MVP все фактически созданные children blocking; optional child моделируется как не созданный child, а не отдельный edge/state                                        | Не требует новых enum/status                                    |
| Что делать, если child стал `obsolete`, пока parent в `waiting_for_children`                                                   | Parent не считается успешно “settled”; `obsolete` child переводит waiting-parent в `obsolete` через parent evaluation                                                  | Нужна явная ветка `waiting_for_children -> obsolete`            |
| Cross-stage child creation не описано                                                                                          | В MVP child tasks, созданные из parent, обязаны иметь тот же `stage_gate`; задачи следующего gate создаёт `Stage-Gate Manager`                                         | Не смешиваются macro и micro orchestration                      |
| Идемпотентность мутаций не описана                                                                                             | Все mutating task commands получают `command_id`; все LLM invocations — `invocation_id`                                                                                | Требуются dedup projections                                     |
| `FailureReason.llm_trace_ref` в Python-модели и JSON schema описан по-разному                                                  | Канонический runtime type: `ArtifactRef`; в task JSON schema/projection допускается хранение только `artifact_id` как компактной формы                                 | Потребуется schema sync v1.1                                    |

---

## 3. Spec A — Task Progression State Machine

### 3.1. Назначение и зона ответственности

`Task Progression State Machine` (`TPSM`) — канонический модуль движения задачи внутри micro-level orchestration.

`TPSM` отвечает за:

* допустимые команды над задачей;
* guards/preconditions переходов;
* machine-readable reason codes;
* deterministic side-effect intents;
* idempotency semantics команд;
* bubble-up, retry, invalidation, lease-expiry, parent/child semantics;
* replay-compatible transition logic.

`TPSM` не является storage, scheduler или executor.

### 3.2. Что делает / чего не делает

**Делает**

* определяет initial state по `TemplateType` и dependency snapshot;
* валидирует переходы для `executable`, `dynamic`, `composite`;
* выдаёт `TransitionPlan = state_patch + events + follow_ups`;
* проверяет invariants;
* даёт deterministic replay правил.

**Не делает**

* не пишет в БД;
* не выбирает задачи из очереди;
* не вызывает бизнес-модуль, LLM или script runtime;
* не строит `CompositeExpansionPlan` сам;
* не открывает/закрывает stage gates;
* не исполняет follow-ups, только декларирует их.

### 3.3. Границы с соседними компонентами

| Компонент              | Граница                                                                                                     |
| ---------------------- | ----------------------------------------------------------------------------------------------------------- |
| `Task Store`           | Загружает snapshot/context, вызывает `TPSM.decide(...)`, затем атомарно применяет `TransitionPlan`          |
| `Task Router`          | Никогда не реализует FSM-правила сам; он только инициирует `take/heartbeat` через Task Store                |
| `Business Modules`     | Выполняют задачу и подают `complete/fail/spawn_children`; status напрямую не меняют                         |
| `Template Registry`    | Даёт `TemplateType`, `output_contract`, `escalation`, `bubble_up_outputs`, `dynamic.max_depth/max_children` |
| `Context Engine`       | Резолвит inputs и пишет `task_inputs`; FSM читает только факт готовности/неготовности deps                  |
| `Stage-Gate Manager`   | Gate state — внешний guard dispatch/create policies, не часть FSM                                           |
| `Interruption Gateway` | Реагирует на `escalation_required` и инициирует admin commands (`retry/invalidate`)                         |
| `Observer API`         | Read-only потребитель `task_events`, `task_status_transitions`, `task_commands`                             |

### 3.4. Domain model / core abstractions

```python
class TaskSnapshot(BaseModel):
    task_id: TaskId
    project_id: ProjectId
    parent_id: TaskId | None
    template_id: TemplateId
    template_version: TemplateVersion
    stage_gate: StageGate
    status: TaskStatus
    priority: int
    queue_position: int | None
    attempt: int
    escalation_count: int
    locked_by: WorkerId | None
    locked_at: datetime | None
    lock_expires_at: datetime | None
    payload: dict[str, Any]
    summary: str | None
    error: FailureReason | None
    version: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
```

```python
class TaskProgressionPolicy(BaseModel):
    template_type: TemplateType
    output_contract: list[OutputArtifactSpec]
    escalation: EscalationPolicy

    # composite-only
    bubble_up_outputs: dict[str, OutputBinding] = {}

    # dynamic-only
    dynamic_max_depth: int | None = None
    dynamic_max_children: int | None = None

    # system caps
    max_children_per_task: int
    max_dag_depth: int
```

```python
class DependencyState(BaseModel):
    dep_id: UUID
    from_task_id: TaskId
    to_task_id: TaskId
    kind: RequirementKind
    input_name: str
    producer_output: str | None
    producer_status: TaskStatus
```

```python
class ChildState(BaseModel):
    task_id: TaskId
    status: TaskStatus
    template_id: TemplateId
    stage_gate: StageGate
```

```python
class CompositeExpansionPlan(BaseModel):
    child_tasks: list[NewTaskSpec]
    child_dependencies: list[NewDependencySpec]
    enabled_aliases: list[str]
```

```python
class TransitionContext(BaseModel):
    now: datetime
    actor: str
    correlation_id: UUID | None = None
    causation_event_id: UUID | None = None
    gate_open: bool = True
    dependencies: list[DependencyState] = []
    children: list[ChildState] = []
    policy: TaskProgressionPolicy
    composite_expansion: CompositeExpansionPlan | None = None
    current_dynamic_depth: int = 0
```

```python
class TaskCommandEnvelope(BaseModel):
    command_id: UUID
    command_type: TaskCommandType
    task_id: TaskId | None = None          # None only for create
    expected_version: int | None = None
    actor: str
    issued_at: datetime
    correlation_id: UUID | None = None
    causation_event_id: UUID | None = None
    payload: dict[str, Any] = {}
```

```python
class PlannedEvent(BaseModel):
    event_type: str
    payload: dict[str, Any]
    actor: str
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    command_id: UUID | None = None
    reason_code: str | None = None
```

```python
class FollowUpAction(BaseModel):
    action: FollowUpActionType
    payload: dict[str, Any]
```

```python
class TransitionPlan(BaseModel):
    command_id: UUID
    task_id: TaskId | None
    from_status: TaskStatus | None
    to_status: TaskStatus | None
    state_patch: dict[str, Any]
    events: list[PlannedEvent]
    follow_ups: list[FollowUpAction]
    idempotency: Literal["new", "replayed"]
```

### 3.5. Data model / schemas / enums

#### Reused shared enums

`TaskStatus`, `TemplateType`, `RequirementKind`, `StageGate`, `FailureCode`.

#### New enums

```python
class TaskCommandType(StrEnum):
    CREATE = "create"
    EVALUATE_DEPENDENCIES = "evaluate_dependencies"
    TAKE = "take"
    HEARTBEAT = "heartbeat"
    COMPLETE = "complete"
    SPAWN_CHILDREN = "spawn_children"      # dynamic only
    EVALUATE_CHILDREN = "evaluate_children"
    FAIL = "fail"
    LEASE_EXPIRED = "lease_expired"
    RETRY = "retry"
    INVALIDATE = "invalidate"
```

```python
class FollowUpActionType(StrEnum):
    EVALUATE_DEPENDENTS = "evaluate_dependents"
    EVALUATE_PARENT = "evaluate_parent"
    EMIT_ESCALATION = "emit_escalation"
    PROPAGATE_INVALIDATION = "propagate_invalidation"
    REGISTER_OUTPUTS = "register_outputs"
    CREATE_CHILDREN = "create_children"
    CREATE_DEPENDENCIES = "create_dependencies"
```

```python
class TransitionReasonCode(StrEnum):
    INITIAL_BLOCKED = "initial_blocked"
    INITIAL_QUEUED = "initial_queued"
    INITIAL_COMPOSITE_WAITING = "initial_composite_waiting"
    DEPENDENCIES_SATISFIED = "dependencies_satisfied"
    DEPENDENCY_FAILED = "dependency_failed"
    DISPATCHED = "dispatched"
    HEARTBEAT_RENEWED = "heartbeat_renewed"
    COMPLETED = "completed"
    DYNAMIC_DECOMPOSED = "dynamic_decomposed"
    COMPOSITE_BUBBLE_UP = "composite_bubble_up"
    DYNAMIC_FINALIZE_READY = "dynamic_finalize_ready"
    CHILD_FAILED = "child_failed"
    CHILD_OBSOLETED = "child_obsoleted"
    LEASE_REQUEUED = "lease_requeued"
    LEASE_EXHAUSTED = "lease_exhausted"
    RETRY_REQUESTED = "retry_requested"
    INVALIDATED = "invalidated"
```

#### Allowed statuses by template type

| Template type | Allowed statuses                                                                              |
| ------------- | --------------------------------------------------------------------------------------------- |
| `executable`  | `blocked`, `queued`, `in_progress`, `completed`, `failed`, `obsolete`                         |
| `dynamic`     | `blocked`, `queued`, `in_progress`, `waiting_for_children`, `completed`, `failed`, `obsolete` |
| `composite`   | `waiting_for_children`, `completed`, `failed`, `obsolete`                                     |

#### New schemas

* `specs/schemas/task_command.schema.json`
* `specs/schemas/task_transition_plan.schema.json`

#### Persistence additions for idempotency

```sql
CREATE TABLE task_commands (
    command_id         UUID         PRIMARY KEY,
    project_id         UUID         NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    task_id            UUID         REFERENCES tasks(task_id) ON DELETE RESTRICT,
    command_type       TEXT         NOT NULL,
    command_hash       CHAR(64)     NOT NULL,
    actor              TEXT         NOT NULL,
    expected_version   INT,
    accepted           BOOLEAN      NOT NULL,
    resulting_status   TEXT,
    result_event_id    UUID         REFERENCES task_events(event_id) ON DELETE RESTRICT,
    issued_at          TIMESTAMPTZ  NOT NULL,
    recorded_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX idx_task_commands_task_recorded
    ON task_commands (task_id, recorded_at DESC);

ALTER TABLE task_events
    ADD COLUMN command_id UUID,
    ADD COLUMN reason_code TEXT;

CREATE INDEX idx_task_events_command_id
    ON task_events (command_id)
    WHERE command_id IS NOT NULL;
```

`command_hash` = SHA-256 canonical JSON envelope без полей, не влияющих на бизнес-семантику (`issued_at`, observability-only metadata).

### 3.6. API / protocol / interfaces

```python
class TaskProgressionEngine(Protocol):
    def decide(
        self,
        *,
        snapshot: TaskSnapshot | None,
        command: TaskCommandEnvelope,
        context: TransitionContext,
    ) -> TransitionPlan: ...

    def replay(
        self,
        *,
        task_id: TaskId,
        events: Iterable[TaskEvent],
        policy: TaskProgressionPolicy,
    ) -> TaskSnapshot: ...

    def validate_snapshot(
        self,
        *,
        snapshot: TaskSnapshot,
        context: TransitionContext,
    ) -> None: ...
```

Интеграционное правило для Task Store:

* все mutating методы принимают `command_id: UUID | None = None`;
* если `command_id` не передан, сервис генерирует его сам;
* exact-once гарантия для клиента есть только при caller-supplied `command_id`;
* duplicate `command_id` + same `command_hash` возвращает уже записанный результат;
* duplicate `command_id` + different `command_hash` → `IdempotencyConflictError`.

### 3.7. Core algorithms / execution semantics

#### 3.7.1. State diagram

```text
executable/dynamic create:
(none) -> blocked | queued

composite create:
(none) -> waiting_for_children

blocked --deps_ready--> queued --take--> in_progress --complete--> completed
                              |                    |
                              |                    +--fail------------------> failed
                              |                    |
                              |                    +--spawn_children-------> waiting_for_children
                              |                                                   |
                              |                                                   +--all children completed, composite--> completed
                              |                                                   |
                              |                                                   +--all children completed, dynamic----> queued
                              |                                                   |
                              |                                                   +--any child failed-------------------> failed
                              |                                                   |
                              |                                                   +--any child obsolete-----------------> obsolete
                              |
                              +--invalidate-------------------------------------> obsolete

in_progress --lease_expired--> queued | failed
failed --retry--> queued
any non-obsolete --invalidate--> obsolete
```

#### 3.7.2. Transition table

| Command                           | From                                               | To                     | Guards                                                                                               | Emitted events                                               | Follow-ups                                                   |
| --------------------------------- | -------------------------------------------------- | ---------------------- | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| `create` (`executable`/`dynamic`) | none                                               | `blocked` / `queued`   | template resolved; policy valid; dependencies snapshot known                                         | `task_created`, `status_changed`                             | none                                                         |
| `create` (`composite`)            | none                                               | `waiting_for_children` | `CompositeExpansionPlan` валиден                                                                     | `task_created`, `children_created`, `status_changed`         | `CREATE_CHILDREN`, `CREATE_DEPENDENCIES`                     |
| `evaluate_dependencies`           | `blocked`                                          | `queued`               | все `hard` deps = `completed`                                                                        | `dependencies_satisfied`, `status_changed`                   | none                                                         |
| `evaluate_dependencies`           | `blocked`                                          | `failed`               | есть `hard` dep в `failed` или `obsolete`                                                            | `dependency_failed`, `task_failed`, `status_changed`         | `EMIT_ESCALATION` при необходимости                          |
| `take`                            | `queued`                                           | `in_progress`          | gate open; lease отсутствует; `expected_version` совпадает                                           | `task_taken`, `status_changed`                               | none                                                         |
| `heartbeat`                       | `in_progress`                                      | `in_progress`          | actor = lease owner; lease не истёк                                                                  | `heartbeat`                                                  | none                                                         |
| `complete`                        | `in_progress`                                      | `completed`            | actor = lease owner; required outputs присутствуют/совместимы с contract                             | `outputs_registered`, `status_changed`                       | `REGISTER_OUTPUTS`, `EVALUATE_DEPENDENTS`, `EVALUATE_PARENT` |
| `spawn_children`                  | `in_progress` (`dynamic` only)                     | `waiting_for_children` | template=`dynamic`; children>0; depth/count limits не нарушены; parent ещё не спавнил children ранее | `children_created`, `status_changed`                         | `CREATE_CHILDREN`, `CREATE_DEPENDENCIES`                     |
| `evaluate_children` (`composite`) | `waiting_for_children`                             | `completed`            | все blocking children = `completed`; bubble-up outputs собраны                                       | `children_completed`, `outputs_registered`, `status_changed` | `REGISTER_OUTPUTS`, `EVALUATE_DEPENDENTS`, `EVALUATE_PARENT` |
| `evaluate_children` (`dynamic`)   | `waiting_for_children`                             | `queued`               | все blocking children = `completed`                                                                  | `children_completed`, `status_changed`                       | none                                                         |
| `evaluate_children`               | `waiting_for_children`                             | `failed`               | есть blocking child в `failed`                                                                       | `child_failed`, `task_failed`, `status_changed`              | `EMIT_ESCALATION` при необходимости                          |
| `evaluate_children`               | `waiting_for_children`                             | `obsolete`             | есть blocking child в `obsolete`                                                                     | `invalidated`, `status_changed`                              | `PROPAGATE_INVALIDATION`                                     |
| `fail`                            | `in_progress` / `blocked` / `waiting_for_children` | `failed`               | причина нормализована                                                                                | `task_failed`, `status_changed`                              | `EMIT_ESCALATION` при необходимости                          |
| `lease_expired`                   | `in_progress`                                      | `queued`               | lease истёк; `attempt < max_attempts`                                                                | `lock_expired`, `status_changed`                             | none                                                         |
| `lease_expired`                   | `in_progress`                                      | `failed`               | lease истёк; `attempt >= max_attempts`                                                               | `lock_expired`, `task_failed`, `status_changed`              | `EMIT_ESCALATION` при необходимости                          |
| `retry`                           | `failed`                                           | `queued`               | `attempt < max_attempts`                                                                             | `task_retried`, `status_changed`                             | none                                                         |
| `invalidate`                      | любой, кроме `obsolete`                            | `obsolete`             | valid admin/system reason                                                                            | `invalidated`, `status_changed`                              | `PROPAGATE_INVALIDATION`                                     |

#### 3.7.3. Deterministic execution rules

1. `TPSM` не реализует queue ordering; ordering остаётся в `Task Store`.
2. `TPSM` не строит composite graph. Для `composite` expansion plan должен быть уже вычислен интеграционным слоем по `Template Registry`.
3. `TPSM` не создаёт `ArtifactRef`; он только валидирует наличие/contract match outputs и формирует intent `REGISTER_OUTPUTS`.
4. Успешная child completion для waiting-parent означает: все blocking children = `completed`. `obsolete` не считается success path.
5. Для `dynamic` повторный `take` после `waiting_for_children -> queued` считается новой dispatch attempt.
6. `retry` и `lease_expired -> queued` всегда присваивают новый `queue_position`.
7. `invalidate` имеет приоритет над late success/failure: команда с устаревшей версией отклоняется как `StaleCommandError`.
8. `escalation_count` увеличивается не внутри pure FSM, а в apply-layer при фактической записи `escalation_required`.

#### 3.7.4. Composite semantics

* `composite` не имеет execution epoch.
* `composite` не получает lease.
* `composite` не dispatchится Router’ом.
* Его lifecycle:

  * `create -> waiting_for_children`
  * `waiting_for_children -> completed|failed|obsolete`
* Bubble-up использует только `template.composite.bubble_up_outputs`.
* Если required parent output нельзя собрать из completed children, parent → `failed` с `code=output_validation_failed`.

#### 3.7.5. Dynamic semantics

* `dynamic` — исполняемая задача.
* Внутри `in_progress` business module принимает решение `execute` vs `decompose`.
* `spawn_children` разрешён только один раз на конкретный parent task.
* После completion всех children parent возвращается в `queued` для finalize phase.
* Finalize phase не выделяется отдельным status; executor определяет её по наличию completed children у task.

#### 3.7.6. Concurrency semantics

* Все mutating commands над существующей задачей, кроме специальных batch invalidate walkers, требуют `expected_version`.
* `take/heartbeat/complete/spawn_children/fail` требуют row lock.
* `heartbeat/complete/spawn_children` требуют `actor == locked_by`.
* `lease_expired` исполняется только lease reaper’ом при `lock_expires_at < now()`.
* System-generated commands используют deterministic `command_id = UUIDv5(namespace, f"{task_id}:{command_type}:{causation_event_id}")`.

### 3.8. Invariants and validation rules

| Код | Инвариант                                                                                           |
| --- | --------------------------------------------------------------------------------------------------- |
| A1  | `status='in_progress'` ⇒ `locked_by`, `locked_at`, `lock_expires_at` заполнены                      |
| A2  | `status in {'queued','waiting_for_children','completed','failed','obsolete'}` ⇒ lease отсутствует   |
| A3  | `status='completed'` ⇒ для всех required outputs есть `task_outputs`                                |
| A4  | `status='failed'` ⇒ `error IS NOT NULL` и `completed_at IS NOT NULL`                                |
| A5  | `status='waiting_for_children'` ⇒ child set не пуст                                                 |
| A6  | `template_type='composite'` ⇒ `status NOT IN {'blocked','queued','in_progress'}`                    |
| A7  | `template_type='dynamic'` ⇒ `spawn_children` не может быть вызван повторно для того же `task_id`    |
| A8  | `attempt` увеличивается только на переходе `queued -> in_progress`                                  |
| A9  | `retry` очищает `error`, `completed_at`, lease-поля и присваивает новый `queue_position`            |
| A10 | Все children одного parent имеют тот же `project_id` и тот же `stage_gate` (MVP)                    |
| A11 | `command_id` идемпотентен и не может использоваться с другим `command_hash`                         |
| A12 | `obsolete` — terminal administrative state; возврат в non-obsolete запрещён                         |
| A13 | Bubble-up использует только `completed` children; `obsolete` child не участвует в bubble-up success |
| A14 | `blocked` существует только при наличии незавершённых `hard` deps                                   |
| A15 | `escalation_count` увеличивается только при фактической записи `escalation_required`                |

### 3.9. Error model / retries / escalation

```python
class TaskProgressionError(PovLabError): ...
class CommandRejectedError(TaskProgressionError, ConflictError): ...
class StaleCommandError(TaskProgressionError, ConflictError): ...
class LeaseOwnershipError(TaskProgressionError, ConflictError): ...
class IdempotencyConflictError(TaskProgressionError, IntegrityError): ...
class BubbleUpContractError(TaskProgressionError, ValidationError): ...
class UnsupportedTemplateLifecycleError(TaskProgressionError, ValidationError): ...
class StageGatePolicyError(TaskProgressionError, ConflictError): ...
```

Правила:

* hidden FSM retries нет;
* transport retry клиента допустим только с тем же `command_id`;
* runtime retry задачи — только explicit `retry`;
* automatic recovery по lease expiry — отдельный переход `lease_expired`;
* `EMIT_ESCALATION` — follow-up intent, а не decision о human handoff.

### 3.10. Observability (logs, metrics, tracing)

**Metrics**

* `pov_fsm_commands_total{command_type,result}`
* `pov_fsm_transitions_total{from,to,reason_code,template_type}`
* `pov_fsm_guard_rejections_total{command_type,reason}`
* `pov_fsm_idempotent_replays_total{command_type}`
* `pov_fsm_bubbleup_total{template_type,result}`
* `pov_fsm_decision_seconds`

**Logs**
Обязательные поля:
`component=task_progression`, `command_id`, `command_type`, `task_id`, `template_id`, `template_version`, `from_status`, `to_status`, `reason_code`, `correlation_id`, `causation_event_id`.

**Tracing**

* span: `task_progression.decide`
* attrs: `task.id`, `task.status.from`, `task.status.to`, `task.command_type`, `task.command_id`, `template.type`
* при idempotent replay: `idempotency.replayed=true`

### 3.11. Testing strategy

1. Exhaustive matrix: `command × from_status × template_type`.
2. Golden tests для `TransitionPlan`, event payloads и `reason_code`.
3. Property-based tests:

   * replay(events) == materialized snapshot;
   * duplicate `command_id` не меняет state;
   * composite никогда не попадает в `in_progress`;
   * obsolete task не re-enters queue.
4. Integration tests через Task Store:

   * late completion after invalidate;
   * concurrent retry vs invalidate;
   * concurrent lease expiry vs heartbeat.
5. Bubble-up tests:

   * composite success;
   * composite missing bubble-up output;
   * dynamic children all completed -> requeue finalize;
   * child obsolete -> parent obsolete.

### 3.12. Performance / limits / non-goals

| Параметр                       | Цель     |
| ------------------------------ | -------- |
| `decide()` p99                 | ≤ 0.5 ms |
| `replay()` на 1000 events      | ≤ 20 ms  |
| Additional memory per decision | ≤ 64 KiB |

Ограничения:

* один `TransitionPlan` не должен содержать > 256 events/follow-ups;
* `spawn_children` ограничен `min(dynamic_max_children, system cap)`.

Non-goals:

* queue scanning;
* SQL persistence;
* context resolution;
* distributed orchestration across projects.

### 3.13. Migration / compatibility notes

1. `02_task_store` становится persistence/apply спецификацией поверх `TPSM`, а не вторым местом описания переходов.
2. Public Task Store API сохраняется; mutating methods получают optional `command_id`.
3. Добавляются:

   * `task_commands`;
   * `task_events.command_id`;
   * `task_events.reason_code`.
4. Нормативные корректировки:

   * `composite` parent создаётся сразу в `waiting_for_children`;
   * `dynamic waiting_for_children -> queued` после completion всех children;
   * `obsolete` child под waiting-parent не считается success path;
   * `retry()` очищает `completed_at`.
5. Shared enums/status names не меняются.

### 3.14. Out of scope

* Macro-level state machine `Stage-Gate Manager`.
* UI/manual workflow engine.
* Cross-project orchestration.
* Distributed dedup beyond одной БД/service boundary.
* Artifact content validation beyond contract presence/type.

---

## 4. Spec B — LLM Communication Interface Module

### 4.1. Назначение и зона ответственности

`LLM Communication Interface Module` (`LLM Gateway`) — provider-agnostic/runtime-agnostic execution layer для всех LLM/agent/runtime вызовов внутри workers/business modules.

Модуль отвечает за:

* unified request/response contract;
* adapter isolation;
* capability discovery;
* deterministic settings surface;
* retries / backoff / fallback / circuit-breaking;
* structured outputs;
* tool calling;
* streaming;
* sessions / multi-turn / agent runtimes;
* cancellation;
* provenance / trace / usage accounting.

### 4.2. Что делает / чего не делает

**Делает**

* принимает fully materialized request;
* выбирает compatible adapter/model;
* выполняет provider/runtime calls с policy-driven retries/fallbacks;
* нормализует ответы, usage, finish reasons и errors;
* валидирует structured outputs;
* поддерживает caller-managed tools, gateway-managed tools и native agent runtimes;
* пишет request/response/trace artifacts и `llm_exchanges`.

**Не делает**

* не рендерит prompt templates и не собирает контекст;
* не меняет task status;
* не хранит бизнес-артефакты проекта, кроме trace/request/response artifacts;
* не валидирует domain correctness generated artifact;
* не заменяет `Context Engine`, `Task Store`, `Workspace Manager`.

### 4.3. Границы с соседними компонентами

| Компонент                          | Граница                                                                                                        |
| ---------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `Business Modules / Worker`        | Формируют request/messages, выбирают mode, получают normalized result/trace                                    |
| `Template Registry`                | Даёт declarative defaults: `model`, `prompt_ref`, `tool_allowlist`, `token_budget`, `temperature`, `json_mode` |
| `Context Engine`                   | Даёт уже собранный контекст; Gateway не тянет зависимости сам                                                  |
| `Workspace Manager / Tool Runtime` | Для managed tools предоставляет tool runners / workspace handles                                               |
| `External resources`               | Вызываются только через registered tool runners или native agent adapter                                       |
| `Task Store`                       | Получает `trace_ref` и нормализованный failure mapping                                                         |
| `Interruption Gateway`             | Не вызывается напрямую; эскалация идёт через business module + Task Store                                      |
| `Observer API`                     | Читает `llm_exchanges`, `llm_sessions`, trace artifacts                                                        |

### 4.4. Domain model / core abstractions

```python
class InvocationMode(StrEnum):
    SINGLE_TURN = "single_turn"
    CALLER_MANAGED_TOOLS = "caller_managed_tools"
    MANAGED_AGENT = "managed_agent"
    SESSION_TURN = "session_turn"

class CapabilityFlag(StrEnum):
    TEXT_OUTPUT = "text_output"
    JSON_OBJECT = "json_object"
    JSON_SCHEMA = "json_schema"
    TOOL_CALLING = "tool_calling"
    STREAMING = "streaming"
    NATIVE_SESSION = "native_session"
    NATIVE_AGENT_RUNTIME = "native_agent_runtime"
    CANCELLATION = "cancellation"
    FILE_INPUT = "file_input"
    IMAGE_INPUT = "image_input"

class SideEffectClass(StrEnum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    EXTERNAL_WRITE = "external_write"
    COMMAND_EXECUTION = "command_execution"
```

```python
class ContentPart(BaseModel):
    kind: Literal["text", "json", "artifact"]
    text: str | None = None
    json_value: Any | None = None
    artifact_ref: ArtifactRef | None = None

class Message(BaseModel):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    parts: list[ContentPart]
    name: str | None = None
    tool_call_id: str | None = None
```

```python
class ResponseFormat(BaseModel):
    mode: Literal["text", "json_object", "json_schema"] = "text"
    schema_ref: SchemaRef | None = None
    json_schema: dict[str, Any] | None = None
    strict: bool = True
```

```python
class DeterministicGeneration(BaseModel):
    temperature: float = 0.0
    top_p: float | None = None
    stop_sequences: list[str] = []
    max_output_tokens: int | None = None
    seed: int | None = None

class InvocationBudget(BaseModel):
    input_tokens_max: int
    output_tokens_max: int
    total_tokens_max: int
    wall_clock_seconds: int

class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    execution_mode: Literal["caller_managed", "gateway_managed"] = "caller_managed"
    side_effect_class: SideEffectClass = SideEffectClass.READ_ONLY
    timeout_seconds: int = 30
    idempotent: bool = False

class ToolChoice(BaseModel):
    mode: Literal["none", "auto", "required", "named"] = "none"
    tool_name: str | None = None
```

```python
class ModelSelector(BaseModel):
    explicit_model: str | None = None            # existing "provider:model"
    policy_hint: str | None = None
    required_capabilities: set[CapabilityFlag] = set()
    preferred_providers: list[str] = []

class GatewayPolicy(BaseModel):
    timeout_seconds: int = 300
    max_retries: int = 2
    base_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 8.0
    allow_fallback: bool = False
    retry_on_schema_failure: bool = False
    max_tool_rounds: int = 8
    allow_side_effect_tools: bool = False
    tool_failure_strategy: Literal["fail_fast", "return_error_to_model"] = "fail_fast"
    circuit_breaker_enabled: bool = True
    store_raw_provider_payload: bool = True
```

```python
class GenerateRequest(BaseModel):
    invocation_id: UUID
    project_id: ProjectId | None = None
    task_id: TaskId | None = None
    template_ref: TemplateRef | None = None
    prompt_ref: SchemaRef | None = None
    selector: ModelSelector
    mode: InvocationMode = InvocationMode.SINGLE_TURN
    messages: list[Message]
    response_format: ResponseFormat = ResponseFormat()
    tools: list[ToolSpec] = []
    tool_choice: ToolChoice = ToolChoice()
    generation: DeterministicGeneration = DeterministicGeneration()
    budget: InvocationBudget
    policy: GatewayPolicy = GatewayPolicy()
    metadata: dict[str, str] = {}
    correlation_id: UUID | None = None
```

```python
class ModelCapability(BaseModel):
    provider_id: str
    model_id: str
    execution_modes: set[InvocationMode]
    features: set[CapabilityFlag]
    context_window: int | None = None
    max_output_tokens: int | None = None
    allowed_side_effects: set[SideEffectClass] = set()
    supports_temperature: bool = True
    supports_top_p: bool = True
    supports_seed: bool = False
```

```python
class Usage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

class ToolCall(BaseModel):
    call_id: str
    name: str
    arguments: dict[str, Any]

class AttemptSummary(BaseModel):
    attempt_no: int
    provider_id: str
    model_id: str
    started_at: datetime
    completed_at: datetime | None
    error_code: LlmErrorCode | None = None
    finish_reason: FinishReason | None = None
    usage: Usage = Usage()

class GenerateResult(BaseModel):
    invocation_id: UUID
    session_id: UUID | None = None
    provider_id: str
    model_id: str
    status: Literal["completed", "failed", "cancelled", "partial"]
    finish_reason: FinishReason
    assistant_message: Message | None = None
    structured_output: Any | None = None
    tool_calls: list[ToolCall] = []
    usage: Usage = Usage()
    attempts: list[AttemptSummary]
    schema_validated: bool = False
    trace_ref: ArtifactRef
    started_at: datetime
    completed_at: datetime | None
```

```python
class OpenSessionRequest(BaseModel):
    project_id: ProjectId | None = None
    task_id: TaskId | None = None
    selector: ModelSelector
    mode: InvocationMode = InvocationMode.SINGLE_TURN
    bootstrap_messages: list[Message] = []
    default_response_format: ResponseFormat = ResponseFormat()
    default_generation: DeterministicGeneration = DeterministicGeneration()
    default_budget: InvocationBudget
    policy: GatewayPolicy = GatewayPolicy()
    metadata: dict[str, str] = {}

class SessionTurnRequest(BaseModel):
    invocation_id: UUID
    session_id: UUID
    append_messages: list[Message]
    response_format: ResponseFormat | None = None
    tools: list[ToolSpec] | None = None
    tool_choice: ToolChoice | None = None
    generation: DeterministicGeneration | None = None
    budget: InvocationBudget | None = None
    policy: GatewayPolicy | None = None
    correlation_id: UUID | None = None
```

```python
class GatewayEventType(StrEnum):
    RESPONSE_STARTED = "response_started"
    TEXT_DELTA = "text_delta"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_READY = "tool_call_ready"
    USAGE = "usage"
    RESPONSE_COMPLETED = "response_completed"
    RESPONSE_FAILED = "response_failed"

class GatewayEvent(BaseModel):
    invocation_id: UUID
    session_id: UUID | None = None
    sequence_no: int
    type: GatewayEventType
    delta_text: str | None = None
    tool_call: ToolCall | None = None
    usage: Usage | None = None
    finish_reason: FinishReason | None = None
    error_code: LlmErrorCode | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
```

### 4.5. Data model / schemas / enums

#### New enums

```python
class FinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALL = "tool_call"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    CONTENT_FILTER = "content_filter"
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    ERROR = "error"
```

```python
class LlmErrorCode(StrEnum):
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    MODEL_NOT_FOUND = "model_not_found"
    INVALID_REQUEST = "invalid_request"
    CONTEXT_WINDOW_EXCEEDED = "context_window_exceeded"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    TIMEOUT = "timeout"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    NETWORK_ERROR = "network_error"
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    CIRCUIT_OPEN = "circuit_open"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
```

```python
class SessionStatus(StrEnum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
```

#### New schemas

* `specs/schemas/llm_request.schema.json`
* `specs/schemas/llm_response.schema.json`
* `specs/schemas/llm_trace.schema.json`
* `specs/schemas/llm_session.schema.json`

#### Persistence

```sql
CREATE TABLE llm_exchanges (
    invocation_id        UUID         PRIMARY KEY,
    project_id           UUID,
    task_id              UUID         REFERENCES tasks(task_id) ON DELETE SET NULL,
    session_id           UUID,
    mode                 TEXT         NOT NULL,
    provider_id          TEXT         NOT NULL,
    model_id             TEXT         NOT NULL,
    adapter_kind         TEXT         NOT NULL,   -- completion/chat/agent_runtime
    status               TEXT         NOT NULL,   -- completed/failed/cancelled/partial
    finish_reason        TEXT,
    request_hash         CHAR(64)     NOT NULL,
    prompt_ref           TEXT,
    correlation_id       UUID,
    request_artifact_id  UUID         NOT NULL,
    response_artifact_id UUID,
    trace_artifact_id    UUID         NOT NULL,
    input_tokens         INT,
    output_tokens        INT,
    total_tokens         INT,
    retry_count          INT          NOT NULL DEFAULT 0,
    fallback_depth       INT          NOT NULL DEFAULT 0,
    started_at           TIMESTAMPTZ  NOT NULL,
    completed_at         TIMESTAMPTZ,
    error_code           TEXT,
    error_message        TEXT
);

CREATE INDEX idx_llm_exchanges_task_started
    ON llm_exchanges (task_id, started_at DESC)
    WHERE task_id IS NOT NULL;

CREATE INDEX idx_llm_exchanges_corr
    ON llm_exchanges (correlation_id)
    WHERE correlation_id IS NOT NULL;

CREATE TABLE llm_sessions (
    session_id              UUID         PRIMARY KEY,
    project_id              UUID,
    task_id                 UUID         REFERENCES tasks(task_id) ON DELETE SET NULL,
    runtime_kind            TEXT         NOT NULL,   -- gateway_managed/native_provider
    provider_id             TEXT         NOT NULL,
    model_id                TEXT         NOT NULL,
    status                  TEXT         NOT NULL,
    native_handle           TEXT,
    transcript_artifact_id  UUID         NOT NULL,
    opened_at               TIMESTAMPTZ  NOT NULL,
    last_activity_at        TIMESTAMPTZ  NOT NULL,
    expires_at              TIMESTAMPTZ  NOT NULL,
    metadata                JSONB        NOT NULL DEFAULT '{}'::jsonb
);
```

#### Trace artifact contract

`trace_artifact_id` должен ссылаться на `ArtifactRef` типа `llm_trace` и содержать:

* canonical request snapshot;
* candidate list и policy decisions;
* per-attempt timing/usage/errors;
* retry/fallback chain;
* structured validation result;
* tool-call/tool-result log;
* native runtime step summaries, если adapter их отдаёт;
* final normalized response hash.

### 4.6. API / protocol / interfaces

#### Public gateway API

```python
class LlmGateway(Protocol):
    async def generate(self, request: GenerateRequest) -> GenerateResult: ...

    async def stream(
        self,
        request: GenerateRequest | SessionTurnRequest,
    ) -> AsyncIterator[GatewayEvent]: ...

    async def open_session(self, request: OpenSessionRequest) -> SessionHandle: ...

    async def session_turn(self, request: SessionTurnRequest) -> GenerateResult: ...

    async def cancel_invocation(
        self,
        invocation_id: UUID,
        reason: str | None = None,
    ) -> CancellationResult: ...

    async def cancel_session(
        self,
        session_id: UUID,
        reason: str | None = None,
    ) -> CancellationResult: ...

    async def capabilities(
        self,
        selector: ModelSelector | None = None,
    ) -> list[ModelCapability]: ...

    async def health(self) -> GatewayHealth: ...
```

#### Adapter API

```python
class LlmAdapter(Protocol):
    def describe_models(self) -> list[ModelCapability]: ...
    async def invoke(self, request: GenerateRequest) -> GenerateResult: ...
```

```python
class StreamingAdapter(LlmAdapter, Protocol):
    async def stream(self, request: GenerateRequest) -> AsyncIterator[GatewayEvent]: ...
```

```python
class NativeSessionAdapter(LlmAdapter, Protocol):
    async def open_native_session(self, request: OpenSessionRequest) -> SessionHandle: ...
    async def native_session_turn(self, request: SessionTurnRequest) -> GenerateResult: ...
```

```python
class AgentRuntimeAdapter(LlmAdapter, Protocol):
    async def run_agent(self, request: GenerateRequest) -> GenerateResult: ...
```

```python
class CancelableAdapter(LlmAdapter, Protocol):
    async def cancel(self, provider_invocation_id: str) -> None: ...
```

Минимальный нормализованный contract:

* каждый adapter обязан поддерживать `single_turn` text generation;
* usage/timing/error normalization обязателен;
* structured output, tool calling, streaming, native sessions, native agent runtime, cancellation — только через declared capabilities;
* gateway-level sessions поддерживаются всегда; native sessions — только при `CapabilityFlag.NATIVE_SESSION`.

### 4.7. Core algorithms / execution semantics

#### 4.7.1. Request normalization and idempotency

1. Входной request canonicalize’ится.
2. `request_hash = SHA-256(canonical_request_without_observability_only_fields)`.
3. Duplicate `invocation_id`:

   * same `request_hash` → вернуть уже сохранённый `GenerateResult`;
   * different `request_hash` → `IdempotencyConflictError`.
4. Поля `project_id`, `task_id`, `template_ref`, `correlation_id`, `metadata` не входят в request hash.

#### 4.7.2. Capability resolution and candidate selection

1. Required capabilities выводятся из:

   * `mode`;
   * `response_format.mode`;
   * наличия `tools`;
   * требования к streaming/session.
2. Если указан `selector.explicit_model`, gateway обязан либо использовать его, либо отклонить запрос как `MODEL_NOT_FOUND` / `UNSUPPORTED_CAPABILITY`.
3. Если explicit model не задан, policy selector строит ordered candidate list.
4. Кандидаты с открытым circuit breaker исключаются, если только explicit model не форсирован.

#### 4.7.3. Budget preflight

1. Gateway оценивает input tokens tokenizer’ом adapter’а или fallback estimator’ом.
2. Если estimate > model context window → `CONTEXT_WINDOW_EXCEEDED`.
3. Если estimate > `budget.input_tokens_max` или `budget.total_tokens_max` → `TOKEN_BUDGET_EXCEEDED`.
4. `max_output_tokens = min(request.generation.max_output_tokens or budget.output_tokens_max, model.max_output_tokens)`.

#### 4.7.4. Attempt loop

Псевдокод:

```text
remaining_total_tokens = budget.total_tokens_max
deadline = now + min(policy.timeout_seconds, budget.wall_clock_seconds)

for candidate in ordered_candidates:
    for retry_no in range(0, policy.max_retries + 1):
        if now >= deadline: fail(TIMEOUT)
        if circuit_open(candidate): maybe_skip

        start attempt span
        invoke adapter/native runtime
        normalize response
        decrement remaining_total_tokens by actual usage if available

        if success:
            persist request/response/trace artifacts
            persist llm_exchanges row
            return result

        if error retriable on same candidate and time/budget remain:
            sleep deterministic backoff
            continue

        break

    if policy.allow_fallback and error is fallback-eligible:
        continue

persist failed trace + llm_exchanges row
raise normalized error / return failed result
```

Правила:

* cumulative tokens across retries, fallback attempts and tool-loop turns считаются против одного `budget.total_tokens_max`;
* cumulative wall clock across all attempts считается против одного `budget.wall_clock_seconds`;
* deterministic backoff: `min(base_backoff_seconds * 2**retry_no, max_backoff_seconds)` без jitter по умолчанию.

#### 4.7.5. Structured output semantics

| Mode          | Поведение                                                                                                                                                                                             |
| ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `text`        | Ответ без schema validation                                                                                                                                                                           |
| `json_object` | Если provider поддерживает native JSON mode — использовать его; иначе `strict=False` допускает локальную проверку JSON parse, `strict=True` — reject                                                  |
| `json_schema` | Если provider поддерживает native JSON Schema — использовать; иначе возможна локальная валидация поверх JSON object только при `strict=False`; при `strict=True` и отсутствии native support — reject |

Дополнительно:

* `retry_on_schema_failure=True` разрешает один repair retry на том же candidate;
* repair retry учитывается как новый LLM call и уменьшает тот же budget;
* при окончательном провале → `SCHEMA_VALIDATION_FAILED`.

#### 4.7.6. Tool and agent modes

| Mode                   | Требование                                       | Gateway исполняет tools                                     | Завершение                                                       |
| ---------------------- | ------------------------------------------------ | ----------------------------------------------------------- | ---------------------------------------------------------------- |
| `single_turn`          | tools пусты                                      | нет                                                         | final assistant response                                         |
| `caller_managed_tools` | provider supports `TOOL_CALLING`                 | нет                                                         | `finish_reason=tool_call`, `tool_calls` возвращаются вызывающему |
| `managed_agent`        | либо `NATIVE_AGENT_RUNTIME`, либо `TOOL_CALLING` | да, если gateway-managed; иначе делегируется native runtime | final assistant response, budget/timeout limit или tool failure  |
| `session_turn`         | open session                                     | зависит от session mode                                     | как у underlying mode                                            |

Gateway-managed tool loop:

1. вызвать model/runtime;
2. получить `tool_calls`;
3. провалидировать `tool_name` и `arguments` against `ToolSpec.input_schema`;
4. проверить side-effect policy;
5. выполнить tool через registered runner;
6. append tool result/error as `tool` message;
7. повторять до final assistant response или `max_tool_rounds`.

Правила side-effects:

* mutating tools запрещены, пока `allow_side_effect_tools=False`;
* `ToolSpec.side_effect_class` должен быть допустим для выбранного adapter/runtime;
* default `tool_failure_strategy = fail_fast`;
* `return_error_to_model` сериализует tool failure как `tool` message и продолжает loop.

#### 4.7.7. Session semantics

* Gateway-level sessions поддерживаются всегда.
* Если adapter поддерживает native sessions и policy/config требует их, `llm_sessions.runtime_kind = native_provider`, иначе `gateway_managed`.
* Каждая turn внутри session получает новый `invocation_id`, но тот же `session_id`.
* Gateway-managed session хранит transcript snapshot в artifact store и при каждом turn materializes full request.
* `expired`, `closed`, `cancelled` session отклоняет новые turns.

#### 4.7.8. Streaming semantics

`stream()` эмитит sequence:

```text
response_started
[text_delta | tool_call_delta | usage]*
[tool_call_ready]*
response_completed | response_failed
```

Правила:

* у каждого stream ровно один terminal event;
* `response_completed` семантически эквивалентен `GenerateResult`;
* для `managed_agent` можно стримить model deltas и tool-call events; stdout/stderr tool’ов не стримится, если tool runtime не объявляет это явно;
* если adapter не поддерживает native streaming, gateway может fallback’нуть на buffered completion и отдать только `response_started + response_completed`.

#### 4.7.9. Cancellation semantics

* Cancellation адресуется по `invocation_id` или `session_id`.
* Если adapter поддерживает native cancel, gateway вызывает его.
* Если native cancel нет, cancellation остаётся best-effort local cancellation.
* После принятия cancellation gateway не должен записывать success result для этой invocation; поздний provider response помечается в trace как `late_response_discarded=true`.
* `cancel_session` переводит session в `cancelled`; новые turns запрещены.

### 4.8. Invariants and validation rules

| Код | Инвариант                                                                                                                  |
| --- | -------------------------------------------------------------------------------------------------------------------------- |
| B1  | `invocation_id` уникален в `llm_exchanges`                                                                                 |
| B2  | same `invocation_id` + different `request_hash` → hard conflict                                                            |
| B3  | `response_format.mode='json_schema'` ⇒ задан ровно один из `schema_ref/json_schema`                                        |
| B4  | `mode='single_turn'` ⇒ `tools=[]`                                                                                          |
| B5  | `mode='caller_managed_tools'` ⇒ candidate supports `TOOL_CALLING`                                                          |
| B6  | `mode='managed_agent'` ⇒ либо candidate supports `NATIVE_AGENT_RUNTIME`, либо `TOOL_CALLING` и есть исполняемые `ToolSpec` |
| B7  | cumulative total tokens across retries/fallback/tool rounds ≤ `budget.total_tokens_max`                                    |
| B8  | gateway never executes mutating tools unless `allow_side_effect_tools=True`                                                |
| B9  | every terminal exchange has `trace_ref`, `request_hash`, `started_at`, `status`                                            |
| B10 | `session_turn` требует session в статусе `open` и `now < expires_at`                                                       |
| B11 | public contract vendor-agnostic; vendor-specific transport params не входят в public API                                   |
| B12 | raw provider payload — audit data, не бизнес-артефакт                                                                      |

### 4.9. Error model / retries / escalation

```python
class LlmGatewayError(PovLabError): ...
class AdapterNotFoundError(LlmGatewayError, NotFoundError): ...
class UnsupportedCapabilityError(LlmGatewayError, ValidationError): ...
class LlmInvalidRequestError(LlmGatewayError, ValidationError): ...
class LlmTimeoutError(LlmGatewayError, ExternalDependencyError): ...
class LlmRateLimitError(LlmGatewayError, ExternalDependencyError): ...
class LlmQuotaError(LlmGatewayError, ExternalDependencyError): ...
class LlmProviderUnavailableError(LlmGatewayError, ExternalDependencyError): ...
class LlmSchemaValidationError(LlmGatewayError, ValidationError): ...
class ToolExecutionError(LlmGatewayError, ExternalDependencyError): ...
class InvocationCancelledError(LlmGatewayError, ConflictError): ...
class InvocationIdempotencyConflictError(LlmGatewayError, IntegrityError): ...
```

#### Retry / fallback matrix

| `LlmErrorCode`                                      | Retry same candidate                       | Fallback candidate | Примечание                                          |
| --------------------------------------------------- | ------------------------------------------ | ------------------ | --------------------------------------------------- |
| `NETWORK_ERROR`                                     | да                                         | да                 | если deadline позволяет                             |
| `PROVIDER_UNAVAILABLE`                              | да                                         | да                 | circuit breaker учитывает                           |
| `RATE_LIMITED`                                      | да                                         | да                 | `Retry-After` уважать, если укладывается в deadline |
| `QUOTA_EXCEEDED`                                    | нет                                        | да                 | тот же candidate бессмысленно повторять             |
| `TIMEOUT`                                           | да                                         | да                 | если осталось wall clock                            |
| `SCHEMA_VALIDATION_FAILED`                          | только если `retry_on_schema_failure=True` | нет                | repair retry максимум один                          |
| `TOOL_EXECUTION_FAILED`                             | нет                                        | нет                | если `tool_failure_strategy=fail_fast`              |
| `INVALID_REQUEST` / `UNSUPPORTED_CAPABILITY`        | нет                                        | нет                | caller bug или policy bug                           |
| `TOKEN_BUDGET_EXCEEDED` / `CONTEXT_WINDOW_EXCEEDED` | нет                                        | нет                | локальная preflight ошибка                          |
| `CANCELLED`                                         | нет                                        | нет                | terminal                                            |

#### Mapping к Task Store `FailureCode`

| Gateway error                                                                                     | Recommended Task Store failure |
| ------------------------------------------------------------------------------------------------- | ------------------------------ |
| `TOKEN_BUDGET_EXCEEDED`, `CONTEXT_WINDOW_EXCEEDED`                                                | `token_budget_exceeded`        |
| `TIMEOUT`                                                                                         | `wall_clock_exceeded`          |
| `SCHEMA_VALIDATION_FAILED`                                                                        | `output_validation_failed`     |
| `RATE_LIMITED`, `QUOTA_EXCEEDED`, `PROVIDER_UNAVAILABLE`, `NETWORK_ERROR` after policy exhaustion | `executor_crashed`             |
| repeated per-task LLM call exhaustion                                                             | `llm_limit_exceeded`           |

`LLM Gateway` сам не эмитит `escalation_required`; это решение business module / Task Store.

### 4.10. Observability (logs, metrics, tracing)

**Metrics**

* `pov_llm_requests_total{provider,model,mode,result}`
* `pov_llm_request_duration_seconds{provider,model,mode}`
* `pov_llm_input_tokens_total{provider,model}`
* `pov_llm_output_tokens_total{provider,model}`
* `pov_llm_retries_total{provider,model,error_code}`
* `pov_llm_fallbacks_total{from_provider,to_provider}`
* `pov_llm_schema_failures_total{provider,model}`
* `pov_llm_tool_rounds_total{provider,model}`
* `pov_llm_sessions_open`
* `pov_llm_circuit_open{provider,model}`

**Logs**
Обязательные поля:
`component=llm_gateway`, `invocation_id`, `session_id`, `task_id`, `template_ref`, `provider_id`, `model_id`, `mode`, `attempt_no`, `retry_count`, `fallback_depth`, `finish_reason`, `error_code`, `trace_artifact_id`, `correlation_id`.

**Tracing**

* root span: `llm_gateway.generate` / `llm_gateway.session_turn`
* child spans: `adapter.invoke`, `adapter.stream`, `adapter.native_session_turn`, `tool.execute`
* attrs: `llm.provider`, `llm.model`, `llm.mode`, `llm.retry_no`, `llm.fallback_depth`, `llm.input_tokens`, `llm.output_tokens`

### 4.11. Testing strategy

1. Adapter conformance tests against fake adapters and fixed fixtures.
2. Idempotency tests:

   * duplicate `invocation_id`;
   * duplicate `session_turn` replay.
3. Retry/fallback/circuit-breaker tests с fault injection.
4. Structured output tests:

   * valid json object;
   * native schema success;
   * local validation failure;
   * repair retry path.
5. Tool loop tests:

   * caller-managed tools;
   * gateway-managed success;
   * side-effect tool rejected;
   * tool timeout/failure.
6. Session tests:

   * native session path;
   * gateway-managed transcript path;
   * expiration / cancellation.
7. Streaming golden tests по `GatewayEvent` sequence.
8. Integration smoke tests для adapters вроде `openrouter`, `claude_code`, `direct_vendor`.

### 4.12. Performance / limits / non-goals

| Параметр                                    | Цель        |
| ------------------------------------------- | ----------- |
| Gateway overhead excluding provider latency | ≤ 5 ms p99  |
| `capabilities()` cached lookup              | ≤ 2 ms p95  |
| trace persistence overhead                  | ≤ 10 ms p95 |

Limits:

* request JSON after materialization ≤ 512 KiB;
* max tools per request = 32;
* max tool schema size per tool = 64 KiB;
* hard cap `max_tool_rounds = 16`;
* max session transcript snapshot = 2 MiB or 256 messages before caller-side summarization.

Non-goals:

* prompt authoring/rendering;
* cost optimization/routing by price;
* long-term memory summarization;
* tenant billing/quota system;
* secret management/safety policy engine.

### 4.13. Migration / compatibility notes

1. Existing `LlmExecutorConfig` maps directly:

   * `model` → `selector.explicit_model`
   * `prompt_ref` → `prompt_ref`
   * `tool_allowlist` → resolved `ToolSpec[]`
   * `token_budget` → `budget`
   * `temperature` → `generation.temperature`
   * `json_mode=true` → `response_format.mode='json_object'`
2. `dynamic.decision_executor` использует тот же mapping.
3. Public template schema менять не нужно для MVP.
4. Опциональные future additions к template schema могут включать: `top_p`, `seed`, `timeout_seconds`, `tool_choice`, `fallback_policy`, `session_mode`.
5. `prompt_ref` остаётся текущим `SchemaRef`-like opaque ref ради совместимости, даже если семантически это prompt artifact.
6. Новые vendor/runtime adapters подключаются как отдельные пакеты:

   * `pov_lab_llm.adapters.openrouter`
   * `pov_lab_llm.adapters.claude_code`
   * `pov_lab_llm.adapters.<vendor>`

### 4.14. Out of scope

* Context retrieval / RAG.
* Prompt storage/versioning semantics.
* Domain-specific artifact acceptance.
* Full policy engine for tool sandboxing.
* Multi-tenant/provider quota allocator.
* Human interaction UX around LLM traces.

---

## 5. Cross-module integration

### 5.1. Интеграция с Template Registry

#### Для `Spec A`

`TPSM` получает из Template Registry projection `TaskProgressionPolicy`:

* `type` → allowed lifecycle;
* `output_contract` → guards для `complete` и bubble-up;
* `escalation` → guards для retry/lease-expiry/escalation;
* `composite.bubble_up_outputs` → mapping parent outputs;
* `dynamic.max_depth/max_children` → guards для `spawn_children`.

`CompositeExpansionPlan` строится вне FSM на основе:

* `composite.children[*].depends_on`;
* `input_bindings`;
* `enabled_if`.

FSM не знает про YAML-graph напрямую; он принимает уже materialized plan.

#### Для `Spec B`

Mapping полей шаблона в gateway request:

| Template field                  | Gateway field                               |
| ------------------------------- | ------------------------------------------- |
| `executable.llm.model`          | `selector.explicit_model`                   |
| `executable.llm.prompt_ref`     | `prompt_ref`                                |
| `executable.llm.tool_allowlist` | `ToolSpec[]` после разрешения tool registry |
| `executable.llm.token_budget`   | `budget`                                    |
| `executable.llm.temperature`    | `generation.temperature`                    |
| `executable.llm.json_mode`      | `response_format.mode='json_object'`        |
| `dynamic.decision_executor.*`   | те же поля для decision call                |

Итог: Template Registry остаётся декларативным source of truth, но не тянет в себя runtime-specific transport contracts.

### 5.2. Интеграция с Task Store

#### `Spec A` внутри Task Store

Нормативный apply path:

```text
TX begin
  load task snapshot + deps + children + template policy
  build TransitionContext
  plan = progression.decide(snapshot, command, context)
  apply plan.state_patch
  insert plan.events into task_events
  insert status transitions
  persist task_commands dedup row
TX commit
execute plan.follow_ups outside or at tail of same orchestration boundary
```

Task Store остаётся:

* source of truth для task state;
* owner `task_events` / `task_status_transitions`;
* owner SQL locking / replay / `SKIP LOCKED`.

FSM не дублирует Task Store persistence.

#### `Spec B` рядом с Task Store

* `llm_exchanges.task_id` — канонический runtime counter для `template.escalation.max_llm_calls`.
* `GenerateResult.trace_ref` должен пробрасываться в `FailureReason.llm_trace_ref`.
* Пока task schema хранит UUID, Task Store сохраняет `trace_ref.artifact_id`.
* При `mark_failed` рекомендуется писать в `reason.details`:

  * `llm_invocation_id`
  * `provider_id`
  * `model_id`
  * `llm_error_code`
  * `retry_count`
* Business module перед каждым новым LLM call должен учитывать `COUNT(*) FROM llm_exchanges WHERE task_id=:task_id` против `max_llm_calls`.

### 5.3. Интеграция с Task Router / Context Engine / Stage-Gate / Interruption Gateway / Workspace

| Компонент                          | Интеграция                                                                                                                                                                                |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Task Router`                      | Dispatchит только `queued` задачи. `composite` он никогда не видит как ready-to-run. После completion children у `dynamic` parent он просто увидит task снова в `queued` для finalization |
| `Context Engine`                   | Резолвит `input_requirements` и пишет `task_inputs`; не вызывается Gateway’ем напрямую                                                                                                    |
| `Stage-Gate Manager`               | Gate openness приходит как внешний guard для `take`; cross-stage child creation в MVP запрещено                                                                                           |
| `Interruption Gateway`             | Подписан на `escalation_required` из Task Store; может инициировать `retry` / `invalidate` с `command_id`                                                                                 |
| `Workspace Manager / Tool Runtime` | Резолвит `tool_allowlist` в `ToolSpec` и исполняет gateway-managed tools                                                                                                                  |
| `Observer API`                     | Read-only поверх `task_events`, `task_status_transitions`, `task_commands`, `llm_exchanges`, `llm_sessions`, trace artifacts                                                              |

### 5.4. Новые shared types / schemas / tables / events

**Нужно добавить**

* shared enums:

  * `TaskCommandType`
  * `TransitionReasonCode`
  * `CapabilityFlag`
  * `SideEffectClass`
  * `FinishReason`
  * `LlmErrorCode`
* schemas:

  * `task_command.schema.json`
  * `task_transition_plan.schema.json`
  * `llm_request.schema.json`
  * `llm_response.schema.json`
  * `llm_trace.schema.json`
  * `llm_session.schema.json`
* tables:

  * `task_commands`
  * `llm_exchanges`
  * `llm_sessions`
* columns:

  * `task_events.command_id`
  * `task_events.reason_code`

**Не нужно добавлять**

* новые `TaskStatus`;
* новые `TemplateType`;
* отдельную vendor-specific таблицу на каждый provider/runtime;
* новую очередь поверх `tasks`;
* новый global event bus для LLM в MVP.

**События**

* существующий набор task events можно сохранить;
* достаточно добавить `command_id` и `reason_code` в event payload/columns;
* отдельные `llm_*` события в Task Store не нужны: observability идёт через `llm_exchanges`, tracing и artifacts.

---

## 6. Open questions / deferred decisions

1. Как именно унифицировать `FailureReason.llm_trace_ref`: полноценный `ArtifactRef` в task schema или UUID-only projection оставить навсегда.
2. Нужен ли distributed circuit breaker / distributed cancellation state для multi-instance `LLM Gateway`; MVP допускает process-local breaker + DB-backed final state.
3. Расширять ли public template schema полями `top_p`, `seed`, `timeout_seconds`, `fallback_policy`, `session_mode`, или держать их runtime-side до появления реального спроса.
4. Нужен ли стандартный repair-loop для `SCHEMA_VALIDATION_FAILED` глубже одного retry, или это должно оставаться policy hook business module.
5. Достаточна ли текущая taxonomy side effects (`read_only/workspace_write/external_write/command_execution`) для agent runtimes уровня code execution.
6. Понадобятся ли cross-stage child tasks / dependencies; текущая спека намеренно запрещает их в MVP.
7. Какая retention/redaction policy нужна для raw provider payload внутри `llm_trace`, когда безопасность перестанет быть low-priority.

