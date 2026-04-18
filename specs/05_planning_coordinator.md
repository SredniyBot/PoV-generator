# Planning Coordinator — спецификация

> **Статус:** v1.0 · Draft · 2026-04-18
> **Зависимости:** [00_overview.md](00_overview.md), [03_template_semantics.md](03_template_semantics.md), [04_problem_state.md](04_problem_state.md), [02_task_store.md](02_task_store.md)
> **Область:** детерминированный selection loop, который выбирает следующий шаблон и материализует задачи.

---

## 1. Назначение и зона ответственности

### 1.1. Что делает
- Собирает planning items из `ProblemState`, validation debt, user intents и системных follow-up'ов.
- Находит кандидатов среди активных шаблонов.
- Вычисляет activation predicates и score.
- Создаёт новые задачи через Task Store.
- Логирует, **почему** был выбран конкретный шаблон.

### 1.2. Чего НЕ делает
- Не вызывает LLM.
- Не хранит доменную методологию вне шаблонов.
- Не исполняет задачи и не собирает контекст.
- Не закрывает gaps напрямую.

### 1.3. Главный принцип

Planning Coordinator — **тонкий диспетчер**, а не второй интеллект системы.  
Он опирается на explicit semantics шаблонов и никогда не пытается “догадаться” вместо них.

---

## 2. Входы Planner'а

### 2.1. `PlanningItem`

```python
class PlanningItem(BaseModel):
    item_id: UUID
    project_id: ProjectId
    item_type: Literal[
        "gap",
        "decision_followup",
        "validation_debt",
        "gate_entry",
        "human_request",
        "system_followup",
    ]
    stage_gate: StageGate
    gap_type: NamespacedId | None = None
    decision_type: NamespacedId | None = None
    severity: Literal["low", "medium", "high", "critical"] | None = None
    blocking: bool = False
    payload: dict[str, Any] = {}
```

### 2.2. Источники planning items

- Активные gaps из `ProblemState`.
- Pending validation findings.
- Gate entry actions при открытии нового `StageGate`.
- Explicit human/developer requests.
- System-generated follow-up intents после completion/retry/invalidate.

---

## 3. Candidate selection

### 3.1. Получение кандидатов

Planner вызывает Registry:

```python
registry.list(
    active_only=True,
    stage_gate=current_stage_gate,
    domains=domain_candidates,
)
```

`domain_candidates` вычисляются из:

- `ProblemState.domain_signals`;
- `human_request`;
- artifact roles уже присутствующих outputs;
- confirmed decisions.

### 3.2. Фильтрация кандидатов

Шаблон исключается, если:

- `status != active`;
- `planner_visibility=internal_only` и trigger не системный;
- activation predicates false;
- существует active task той же `dedup_key`;
- gate закрыт для новых задач этого типа;
- у задачи нет шанса собрать hard inputs по доступным artifact roles / problem fields.

---

## 4. Scoring model

Score должен быть детерминированным и explainable.

### 4.1. Базовая формула

```python
score = (
    gap_score
    + stage_score
    + template_priority_score
    + readiness_score
    + human_override_score
    - duplicate_penalty
    - cooldown_penalty
)
```

### 4.2. Нормативные веса по умолчанию

| Компонент | Формула |
|---|---|
| `gap_score` | `120` для blocking critical, `90` для blocking high, `50` для medium, `20` для low |
| `stage_score` | `+20`, если `current_stage_gate` входит в `preferred_stage_gates`; иначе `0` |
| `template_priority_score` | `semantics.priority_hint` |
| `readiness_score` | `+30`, если все hard inputs уже доступны; `+10`, если нужны только summaries; `-50`, если missing hard inputs |
| `human_override_score` | `+1000`, если developer явно форсировал template |
| `duplicate_penalty` | `1000`, если dedup conflict; иначе `0` |
| `cooldown_penalty` | `20`, если последний task той же family завершился менее `cooldown_seconds` назад |

### 4.3. Tie-breakers

При равном score:

1. Более узкий `closes_gaps` wins.
2. Более высокий `max_input_tokens` loses.
3. Меньший `template_id` wins lexicographically.

Это делает выбор полностью детерминированным.

---

## 5. Материализация задач

### 5.1. `TaskMaterializationSpec`

```python
class TaskMaterializationSpec(BaseModel):
    project_id: ProjectId
    template_ref: TemplateRef
    stage_gate: StageGate
    dedup_key: str
    source_item_id: UUID
    payload: dict[str, Any]
    summary: str
    correlation_id: UUID | None = None
```

### 5.2. Правила materialization

1. Один `PlanningDecision` создаёт не более одной корневой задачи.
2. Planner создаёт task только если есть **минимальный execution path**:
   - шаблон активен;
   - stage gate допускает создание;
   - Task Store FSM допускает initial status;
   - Context Engine потенциально способен собрать hard inputs.
3. Если execution path невозможен, Planner не создаёт task; вместо этого он:
   - открывает новый gap, либо
   - создаёт escalation item.

---

## 6. Replanning и invalidation

### 6.1. Когда разрешён replanning

Replanning разрешён только при событиях:

- confirmed decision superseded;
- blocking gap reopened;
- produced artifact marked invalid;
- stage gate rollback;
- human override.

### 6.2. Примитивы replanning

Planner не мутирует DAG напрямую. Он использует только следующие команды Task Store:

- `invalidate(task_id, reason=...)`
- `retry(task_id, reason=...)`
- `create_task(spec)`

### 6.3. Ограничения

- Planner не имеет права удалять события или задачи.
- Planner не имеет права редактировать завершённые outputs.
- Planner не создаёт cross-stage child tasks; переход между stage gates делает governance layer.

---

## 7. API

```python
class PlanningCoordinator(Protocol):
    async def plan_once(
        self,
        *,
        project_id: ProjectId,
        stage_gate: StageGate | None = None,
        trigger: str,
    ) -> list["PlanningDecision"]: ...

    async def explain_last_run(
        self,
        *,
        project_id: ProjectId,
    ) -> list["PlanningDecision"]: ...

    async def enqueue_human_request(
        self,
        *,
        project_id: ProjectId,
        request_payload: dict[str, Any],
    ) -> None: ...
```

```python
class PlanningDecision(BaseModel):
    planning_run_id: PlanningRunId
    project_id: ProjectId
    source_item_id: UUID
    template_ref: TemplateRef
    score: int
    dedup_key: str
    status: Literal["materialized", "skipped_duplicate", "skipped_guard", "escalated"]
    reasons: list[str]
    created_task_id: TaskId | None = None
```

---

## 8. Хранение и DDL

```sql
CREATE TABLE planning_runs (
    planning_run_id       UUID         PRIMARY KEY,
    project_id            UUID         NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    stage_gate            TEXT         NOT NULL,
    trigger               TEXT         NOT NULL,
    started_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    finished_at           TIMESTAMPTZ,
    actor                 TEXT         NOT NULL,
    status                TEXT         NOT NULL CHECK (status IN ('running','completed','failed'))
);

CREATE TABLE planning_decisions (
    planning_decision_id  UUID         PRIMARY KEY,
    planning_run_id       UUID         NOT NULL REFERENCES planning_runs(planning_run_id) ON DELETE CASCADE,
    project_id            UUID         NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    source_item_id        UUID         NOT NULL,
    template_id           TEXT         NOT NULL,
    template_version      TEXT         NOT NULL,
    dedup_key             TEXT         NOT NULL,
    score                 INT          NOT NULL,
    decision_status       TEXT         NOT NULL CHECK (decision_status IN
                                    ('materialized','skipped_duplicate','skipped_guard','escalated')),
    reasons               JSONB        NOT NULL DEFAULT '[]'::jsonb,
    created_task_id       UUID         REFERENCES tasks(task_id) ON DELETE SET NULL,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX idx_planning_runs_project_started
    ON planning_runs (project_id, started_at DESC);
```

---

## 9. Алгоритм `plan_once`

1. Прочитать актуальный `ProblemStateSnapshot`.
2. Считать active tasks, validation debt и stage gate status.
3. Собрать `PlanningItem`'ы.
4. Для каждого item:
   - получить candidate templates;
   - отфильтровать по activation;
   - вычислить score;
   - выбрать победителя;
   - materialize task или записать skip reason.
5. Сохранить `planning_runs` + `planning_decisions`.
6. Вернуть decisions.

Planner может запускаться:

- по schedule;
- после completion/failure задачи;
- после patch `ProblemState`;
- после human input;
- после gate transition.

---

## 10. Инварианты

| Код | Инвариант |
|---|---|
| C1 | Planner не использует LLM |
| C2 | Любой `PlanningDecision` имеет `reasons[]` с human-readable объяснением |
| C3 | Для одного `(project_id, dedup_key)` не более одной active materialized task family |
| C4 | Planner не создаёт task без `template_version` |
| C5 | Planner читает только snapshot/projections, а не сырые artifacts напрямую |
| C6 | Planner не открывает и не закрывает gaps напрямую |

---

## 11. Что вне области этой спеки

- Семантика самих templates — [03_template_semantics.md](03_template_semantics.md).
- Context assembly — [06_artifact_context.md](06_artifact_context.md).
- Runtime execution — [07_execution_runtime.md](07_execution_runtime.md).
