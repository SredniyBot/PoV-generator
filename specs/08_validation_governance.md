# Validation и Governance — спецификация

> **Статус:** v1.1 · Draft · 2026-04-19
> **Зависимости:** [00_overview.md](00_overview.md), [02_task_store.md](02_task_store.md), [04_problem_state.md](04_problem_state.md), [07_execution_runtime.md](07_execution_runtime.md), [09_domain_packs.md](09_domain_packs.md)
> **Область:** contract validation, critique loops, stage-gate governance и human escalation.

---

## 1. Назначение и зона ответственности

### 1.1. Что делает
- Проверяет outputs задачи против `output_contract`.
- Выполняет дополнительные checks, critique loops и integration validation.
- Проверяет readiness claims и recipe completion conditions перед success.
- Учитывает expectations активных `Domain Pack` при проверке completeness и quality.
- Оценивает exit criteria текущего `StageGate`.
- Создаёт escalation tickets при невозможности надёжно продолжать автоматически.

### 1.2. Чего НЕ делает
- Не выбирает следующий template.
- Не исполняет бизнес-задачу вместо Runtime.
- Не меняет raw artifacts.

---

## 2. Валидационный pipeline

### 2.1. Порядок шагов

После `ExecutionResult(status="succeeded")` система обязана пройти:

1. `output_contract` validation
2. artifact schema validation
3. optional critique / review loop
4. integration checks
5. readiness / recipe completion validation
6. domain-pack validation expectations
7. `ProblemStatePatch` validation
8. commit outputs + patch

Только после этих шагов задача может перейти в `Completed`.

### 2.2. `ValidationRun`

```python
class ValidationRun(BaseModel):
    validation_run_id: UUID
    project_id: ProjectId
    task_id: TaskId
    execution_run_id: ExecutionRunId
    status: Literal["running", "passed", "failed", "escalated"]
    findings: list["ValidationFinding"] = []
```

```python
class ValidationFinding(BaseModel):
    finding_id: UUID
    finding_type: Literal["contract_error", "schema_error", "quality_risk", "integration_failure", "readiness_failure", "recipe_failure"]
    severity: Literal["info", "warning", "error", "critical"]
    blocking: bool
    message: str
    related_artifact_ids: list[UUID] = []
```

### 2.3. Domain Pack expectations

Каждый активный `Domain Pack` может добавлять:

- обязательные artifact roles;
- доменные review expectations;
- доменные completeness checks;
- readiness expectations;
- quality findings specific to the domain.

Нормативное правило:

Если `Domain Pack` активирован в `ProblemState`, validator обязан учитывать его expectations даже тогда, когда основной `Recipe` является общим.

Пример:

Если активирован `frontend` pack, то validation черновика ТЗ должна уметь проверить, что в артефакте появились или были подготовлены:

- роли пользователей;
- пользовательские потоки;
- ожидания по экранам;
- ключевые UX/UI ограничения.

---

## 3. Critique loops

### 3.1. Источник правил

Critique loop запускается только если:

- `validation_policy.critique_template_refs` не пуст;
- есть findings, которые допускают correction;
- `max_correction_loops > 0`.

### 3.2. Правила

- Каждая correction iteration создаёт **отдельную задачу** или отдельный execution attempt; скрытых циклов внутри validator нет.
- `max_correction_loops` жёсткий.
- После исчерпания loops система либо fail'ит, либо escalates по policy.

---

## 4. Stage-Gate governance

### 4.1. Роль stage gate

`StageGate` — это governance-механизм, а не planner.  
Он:

- ограничивает допуск новых задач в фазу;
- проверяет exit criteria;
- допускает controlled backflow.

### 4.2. `GatePolicy`

```python
class GatePolicy(BaseModel):
    stage_gate: StageGate
    required_gap_types_closed: list[NamespacedId] = []
    required_readiness_types: list[NamespacedId] = []
    required_artifact_roles: list[NamespacedId] = []
    required_check_ids: list[str] = []
    allows_backflow_to: list[StageGate] = []
```

### 4.3. Exit criteria

Gate может быть закрыт, только если:

- нет blocking gaps, относящихся к текущей фазе;
- обязательные readiness dimensions не ниже `ready`/`waived`;
- есть обязательные artifact roles;
- выполнены обязательные domain-pack expectations для этой фазы;
- все required checks passed;
- нет active critical escalations по фазе.

### 4.4. Backflow

Если в более поздней фазе открыт новый blocking gap ранней фазы:

1. governance layer не переписывает историю;
2. открывает gate backflow;
3. Planner снова может создавать задачи соответствующего stage.

---

## 5. Human escalation

### 5.1. `EscalationTicket`

```python
class EscalationTicket(BaseModel):
    escalation_ticket_id: UUID
    project_id: ProjectId
    task_id: TaskId | None = None
    stage_gate: StageGate
    reason_code: str
    severity: Literal["warning", "error", "critical"]
    blocking: bool
    summary: str
    details: dict[str, Any]
    created_at: datetime
    resolved_at: datetime | None = None
    resolution: str | None = None
```

### 5.2. Когда создаётся escalation

- missing blocking user input;
- repeated correction exhaustion;
- conflicting confirmed decisions;
- impossible context assembly for required task;
- critical validation failure;
- external side-effect approval required.

### 5.3. Типы разрешения

- `user_answered`
- `developer_override`
- `accepted_risk`
- `aborted_project`
- `replanned`

---

## 6. Хранение и DDL

```sql
CREATE TABLE validation_runs (
    validation_run_id     UUID         PRIMARY KEY,
    project_id            UUID         NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    task_id               UUID         NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    execution_run_id      UUID         NOT NULL REFERENCES execution_runs(execution_run_id) ON DELETE CASCADE,
    status                TEXT         NOT NULL CHECK (status IN ('running','passed','failed','escalated')),
    started_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    finished_at           TIMESTAMPTZ
);

CREATE TABLE validation_findings (
    finding_id            UUID         PRIMARY KEY,
    validation_run_id     UUID         NOT NULL REFERENCES validation_runs(validation_run_id) ON DELETE CASCADE,
    finding_type          TEXT         NOT NULL,
    severity              TEXT         NOT NULL CHECK (severity IN ('info','warning','error','critical')),
    blocking              BOOLEAN      NOT NULL,
    message               TEXT         NOT NULL,
    details               JSONB        NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE escalation_tickets (
    escalation_ticket_id  UUID         PRIMARY KEY,
    project_id            UUID         NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    task_id               UUID         REFERENCES tasks(task_id) ON DELETE SET NULL,
    stage_gate            TEXT         NOT NULL,
    reason_code           TEXT         NOT NULL,
    severity              TEXT         NOT NULL CHECK (severity IN ('warning','error','critical')),
    blocking              BOOLEAN      NOT NULL,
    summary               TEXT         NOT NULL,
    details               JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    resolved_at           TIMESTAMPTZ,
    resolution            TEXT
);
```

---

## 7. API

```python
class ValidationService(Protocol):
    async def validate_execution(
        self,
        *,
        task_id: TaskId,
        execution_result: ExecutionResult,
    ) -> ValidationRun: ...

class GovernanceService(Protocol):
    async def evaluate_stage_gate(
        self,
        *,
        project_id: ProjectId,
        stage_gate: StageGate,
    ) -> dict[str, Any]: ...

    async def create_escalation(
        self,
        *,
        ticket: EscalationTicket,
    ) -> None: ...
```

---

## 8. Инварианты

| Код | Инвариант |
|---|---|
| G1 | Task не может стать `Completed` без успешного `ValidationRun` |
| G2 | `StageGate` не используется Planner'ом как источник доменной логики |
| G3 | Blocking escalation ticket блокирует закрытие gate |
| G4 | Critique loop не может выполняться скрыто; каждая итерация traceable |
| G5 | Validation не может silently менять confirmed decisions |
| G6 | `core_task` не может считаться успешным, если recipe-required meta-passes не подтверждены |
| G7 | Активный `Domain Pack` обязан влиять на validation expectations соответствующей фазы |

---

## 9. Что вне области этой спеки

- Planner scoring — [05_planning_coordinator.md](05_planning_coordinator.md).
- Execution adapters — [07_execution_runtime.md](07_execution_runtime.md).
