# Problem State Store — спецификация

> **Статус:** v1.1 · Draft · 2026-04-19
> **Зависимости:** [00_overview.md](00_overview.md), [03_template_semantics.md](03_template_semantics.md), [02_task_store.md](02_task_store.md)
> **Область:** структурированное состояние проблемы проекта, gaps, decisions, risks и event-sourced semantic memory.

---

## 1. Назначение и зона ответственности

### 1.1. Что делает
- Хранит **структурированное понимание проекта**, а не сырой набор документов.
- Ведёт append-only историю изменений `ProblemState`.
- Поддерживает активные gaps, decisions, constraints, assumptions, risks, domain signals, enabled domain packs и readiness model.
- Принимает patches от задач, пользователя, разработчика и validation layer.
- Даёт Planning Coordinator'у компактное и детерминированное состояние для admission/selection следующего шага.

### 1.2. Чего НЕ делает
- Не хранит blob-содержимое артефактов; для этого есть Artifact Store.
- Не принимает решения о выборе шаблона; это делает Planning Coordinator.
- Не исполняет задачи и не валидирует output contracts.
- Не заменяет журналы исполнения и tool traces.

### 1.3. Главный принцип

`ProblemState` — это **не всё знание проекта**, а его каноническая структурированная проекция, пригодная для планирования и explainability.

Дополнение: `ProblemState` хранит не только “что известно”, но и “насколько задача зрелая для следующего класса шагов”. Это выражается через readiness dimensions.

---

## 2. Модель данных

### 2.1. Ключевые сущности

```python
class GoalRecord(BaseModel):
    summary: str
    business_need: str
    actor: str | None = None
    target_outcome: str | None = None

class SuccessCriterion(BaseModel):
    criterion_id: UUID
    description: str
    metric_name: str | None = None
    target_value: str | None = None
    blocking: bool = True

class ConstraintRecord(BaseModel):
    constraint_id: UUID
    constraint_type: NamespacedId
    description: str
    blocking: bool = True
    source: Literal["user", "system", "task", "developer"]

class AssumptionRecord(BaseModel):
    assumption_id: UUID
    description: str
    confidence: float
    owner: str
    status: Literal["active", "validated", "rejected"]

class RiskRecord(BaseModel):
    risk_id: UUID
    risk_type: NamespacedId
    description: str
    severity: Literal["low", "medium", "high", "critical"]
    status: Literal["open", "mitigated", "accepted", "closed"]
    linked_gap_ids: list[UUID] = []
```

```python
class GapRecord(BaseModel):
    gap_id: UUID
    gap_type: NamespacedId
    title: str
    description: str
    severity: Literal["low", "medium", "high", "critical"]
    blocking: bool = True
    status: Literal["open", "accepted_risk", "closed", "obsolete"] = "open"
    confidence: float = 1.0
    owner_task_id: UUID | None = None
    fingerprint: str
    evidence_artifacts: list[ArtifactRef] = []
    opened_in_version: int
    closed_in_version: int | None = None
```

```python
class DecisionRecord(BaseModel):
    decision_id: UUID
    decision_type: NamespacedId
    title: str
    value: str
    rationale: str | None = None
    status: Literal["proposed", "confirmed", "superseded", "rejected"] = "proposed"
    proposed_by_task_id: UUID | None = None
    supersedes_decision_id: UUID | None = None
```

```python
class DomainSignal(BaseModel):
    signal_id: UUID
    signal_type: NamespacedId
    value: str
    confidence: float

class EnabledDomainPack(BaseModel):
    pack_id: NamespacedId
    version: str
    activation_reason: str
    activated_in_version: int

class RecipeCompositionRecord(BaseModel):
    base_recipe_id: NamespacedId
    base_recipe_version: str
    composed_recipe_id: NamespacedId
    composed_recipe_version: str
    fragment_refs: list[str] = []
    updated_in_version: int

class ReadinessDimension(BaseModel):
    readiness_type: NamespacedId
    title: str
    description: str
    status: Literal["unknown", "not_ready", "partial", "ready", "waived"]
    blocking: bool = True
    confidence: float = 1.0
    evidence_artifact_ids: list[UUID] = []
    updated_in_version: int
```

```python
class ProblemStateSnapshot(BaseModel):
    project_id: ProjectId
    version: int
    stage_gate: StageGate
    goal: GoalRecord | None
    success_criteria: list[SuccessCriterion]
    known_facts: dict[str, Any]
    active_gaps: list[GapRecord]
    decisions: list[DecisionRecord]
    constraints: list[ConstraintRecord]
    assumptions: list[AssumptionRecord]
    risks: list[RiskRecord]
    domain_signals: list[DomainSignal]
    enabled_domain_packs: list[EnabledDomainPack]
    recipe_composition: RecipeCompositionRecord | None
    readiness: list[ReadinessDimension]
    extensions: dict[str, Any] = {}
    created_at: datetime
    created_by: str
```

### 2.2. Domain extensions

`extensions` допускает domain-specific JSON, но только по схеме `SchemaRef`. Extension не может дублировать core fields (`goal`, `active_gaps`, `decisions`, `constraints`, `risks`).

Нормативное правило:

- факт подключения домена должен быть отражён не только в `extensions`, а в explicit fields `enabled_domain_packs` и `recipe_composition`;
- это нужно для explainability: система должна уметь ответить, почему в проекте появились дополнительные обязательные шаги для ТЗ или архитектуры.

---

## 3. Patch-модель

Любое изменение `ProblemState` оформляется как patch и записывается в event log.

### 3.1. Patch-операции

```python
class GapUpsert(BaseModel):
    op: Literal["gap_upsert"] = "gap_upsert"
    gap_type: NamespacedId
    title: str
    description: str
    severity: Literal["low", "medium", "high", "critical"]
    blocking: bool = True
    confidence: float = 1.0
    owner_task_id: UUID | None = None
    evidence_artifact_ids: list[UUID] = []

class GapClose(BaseModel):
    op: Literal["gap_close"] = "gap_close"
    gap_type: NamespacedId
    reason: str
    evidence_artifact_ids: list[UUID] = []

class DecisionUpsert(BaseModel):
    op: Literal["decision_upsert"] = "decision_upsert"
    decision_type: NamespacedId
    title: str
    value: str
    rationale: str | None = None
    status: Literal["proposed", "confirmed"] = "proposed"

class DecisionSupersede(BaseModel):
    op: Literal["decision_supersede"] = "decision_supersede"
    decision_type: NamespacedId
    new_value: str
    rationale: str

class ConstraintUpsert(BaseModel):
    op: Literal["constraint_upsert"] = "constraint_upsert"
    constraint_type: NamespacedId
    description: str
    blocking: bool = True

class AssumptionUpsert(BaseModel):
    op: Literal["assumption_upsert"] = "assumption_upsert"
    description: str
    confidence: float
    owner: str

class RiskUpsert(BaseModel):
    op: Literal["risk_upsert"] = "risk_upsert"
    risk_type: NamespacedId
    description: str
    severity: Literal["low", "medium", "high", "critical"]
    linked_gap_types: list[NamespacedId] = []

class ReadinessUpsert(BaseModel):
    op: Literal["readiness_upsert"] = "readiness_upsert"
    readiness_type: NamespacedId
    title: str
    description: str
    status: Literal["unknown", "not_ready", "partial", "ready", "waived"]
    blocking: bool = True
    confidence: float = 1.0
    evidence_artifact_ids: list[UUID] = []

class FieldSet(BaseModel):
    op: Literal["field_set"] = "field_set"
    field_path: ProblemFieldPath
    value: Any
    mode: Literal["replace", "append", "merge", "set_if_empty"] = "replace"

class DomainPackEnable(BaseModel):
    op: Literal["domain_pack_enable"] = "domain_pack_enable"
    pack_id: NamespacedId
    version: str
    activation_reason: str

class RecipeCompositionSet(BaseModel):
    op: Literal["recipe_composition_set"] = "recipe_composition_set"
    base_recipe_id: NamespacedId
    base_recipe_version: str
    composed_recipe_id: NamespacedId
    composed_recipe_version: str
    fragment_refs: list[str] = []
```

```python
ProblemPatchOp = (
    GapUpsert
    | GapClose
    | DecisionUpsert
    | DecisionSupersede
    | ConstraintUpsert
    | AssumptionUpsert
    | RiskUpsert
    | ReadinessUpsert
    | FieldSet
    | DomainPackEnable
    | RecipeCompositionSet
)

class ProblemStatePatch(BaseModel):
    patch_id: UUID
    project_id: ProjectId
    expected_version: int
    stage_gate: StageGate | None = None
    operations: list[ProblemPatchOp]
    actor: str
    reason: str | None = None
    correlation_id: UUID | None = None
    causation_task_id: UUID | None = None
```

### 3.2. Правила применения patch

1. Patch применяется только к конкретной `expected_version`.
2. Если version изменилась, caller обязан перечитать snapshot и пересобрать patch.
3. Повторная запись с тем же `patch_id` и тем же hash — идемпотентный replay.
4. `gap_close` закрывает только активный gap matching по `gap_type`.
5. `decision_supersede` всегда создаёт новую запись решения и помечает старую как `superseded`.
6. `field_set` не может менять `active_gaps`, `decisions`, `constraints`, `assumptions`, `risks` напрямую; для этого нужны соответствующие операции.
7. Подключение domain pack и изменение composed recipe допускается только explicit operations `domain_pack_enable` и `recipe_composition_set`.

---

## 4. Хранение и DDL

### 4.1. Event log и snapshots

```sql
CREATE TABLE problem_state_events (
    event_id             UUID         PRIMARY KEY,
    project_id           UUID         NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    version              INT          NOT NULL,
    patch_id             UUID         NOT NULL,
    event_type           TEXT         NOT NULL CHECK (event_type IN ('state_initialized', 'patch_applied')),
    patch                JSONB        NOT NULL,
    actor                TEXT         NOT NULL,
    reason               TEXT,
    correlation_id       UUID,
    causation_task_id    UUID         REFERENCES tasks(task_id) ON DELETE SET NULL,
    occurred_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (project_id, version),
    UNIQUE (patch_id)
);

CREATE TABLE problem_state_snapshots (
    snapshot_id          UUID         PRIMARY KEY,
    project_id           UUID         NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    version              INT          NOT NULL,
    stage_gate           TEXT         NOT NULL,
    goal                 JSONB,
    success_criteria     JSONB        NOT NULL DEFAULT '[]'::jsonb,
    known_facts          JSONB        NOT NULL DEFAULT '{}'::jsonb,
    decisions            JSONB        NOT NULL DEFAULT '[]'::jsonb,
    constraints          JSONB        NOT NULL DEFAULT '[]'::jsonb,
    assumptions          JSONB        NOT NULL DEFAULT '[]'::jsonb,
    risks                JSONB        NOT NULL DEFAULT '[]'::jsonb,
    domain_signals       JSONB        NOT NULL DEFAULT '[]'::jsonb,
    enabled_domain_packs JSONB        NOT NULL DEFAULT '[]'::jsonb,
    recipe_composition   JSONB,
    readiness            JSONB        NOT NULL DEFAULT '[]'::jsonb,
    extensions           JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    created_by           TEXT         NOT NULL,
    UNIQUE (project_id, version)
);
```

### 4.2. Projections для planner/UI

```sql
CREATE TABLE problem_gaps (
    gap_id               UUID         PRIMARY KEY,
    project_id           UUID         NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    gap_type             TEXT         NOT NULL,
    title                TEXT         NOT NULL,
    description          TEXT         NOT NULL,
    severity             TEXT         NOT NULL CHECK (severity IN ('low','medium','high','critical')),
    blocking             BOOLEAN      NOT NULL DEFAULT TRUE,
    status               TEXT         NOT NULL CHECK (status IN ('open','accepted_risk','closed','obsolete')),
    confidence           DOUBLE PRECISION NOT NULL,
    fingerprint          CHAR(64)     NOT NULL,
    owner_task_id        UUID         REFERENCES tasks(task_id) ON DELETE SET NULL,
    evidence_artifact_ids JSONB       NOT NULL DEFAULT '[]'::jsonb,
    opened_in_version    INT          NOT NULL,
    closed_in_version    INT,
    UNIQUE (project_id, gap_type, fingerprint)
);

CREATE INDEX idx_problem_gaps_active
    ON problem_gaps (project_id, severity)
    WHERE status = 'open';

CREATE TABLE problem_decisions (
    decision_id          UUID         PRIMARY KEY,
    project_id           UUID         NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    decision_type        TEXT         NOT NULL,
    title                TEXT         NOT NULL,
    value                TEXT         NOT NULL,
    rationale            TEXT,
    status               TEXT         NOT NULL CHECK (status IN ('proposed','confirmed','superseded','rejected')),
    proposed_by_task_id  UUID         REFERENCES tasks(task_id) ON DELETE SET NULL,
    supersedes_decision_id UUID       REFERENCES problem_decisions(decision_id) ON DELETE SET NULL,
    created_in_version   INT          NOT NULL
);
```

Дополнительные projections `problem_constraints`, `problem_assumptions`, `problem_risks` реализуются отдельными таблицами по тому же принципу; их схема следует Pydantic-моделям из §2.1.

Для readiness вводится отдельная projection:

```sql
CREATE TABLE problem_readiness (
    readiness_id         UUID         PRIMARY KEY,
    project_id           UUID         NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    readiness_type       TEXT         NOT NULL,
    title                TEXT         NOT NULL,
    description          TEXT         NOT NULL,
    status               TEXT         NOT NULL CHECK (status IN ('unknown','not_ready','partial','ready','waived')),
    blocking             BOOLEAN      NOT NULL DEFAULT TRUE,
    confidence           DOUBLE PRECISION NOT NULL,
    evidence_artifact_ids JSONB       NOT NULL DEFAULT '[]'::jsonb,
    updated_in_version   INT          NOT NULL,
    UNIQUE (project_id, readiness_type)
);

CREATE INDEX idx_problem_readiness_blocking
    ON problem_readiness (project_id, status)
    WHERE blocking = TRUE;
```

---

## 5. API

```python
class ProblemStateStore(Protocol):
    async def initialize(
        self,
        *,
        project_id: ProjectId,
        stage_gate: StageGate,
        initial_patch: ProblemStatePatch,
    ) -> ProblemStateSnapshot: ...

    async def get_snapshot(
        self,
        *,
        project_id: ProjectId,
        version: int | None = None,
    ) -> ProblemStateSnapshot: ...

    async def apply_patch(
        self,
        *,
        patch: ProblemStatePatch,
    ) -> ProblemStateSnapshot: ...

    async def list_active_gaps(
        self,
        *,
        project_id: ProjectId,
    ) -> list[GapRecord]: ...

    async def list_decisions(
        self,
        *,
        project_id: ProjectId,
        active_only: bool = True,
    ) -> list[DecisionRecord]: ...

    async def replay(
        self,
        *,
        project_id: ProjectId,
    ) -> ProblemStateSnapshot: ...
```

---

## 6. Алгоритмы

### 6.1. `initialize`

1. Проверить, что по project ещё нет snapshot.
2. Создать synthetic `ProblemState v1` через `initial_patch`.
3. Записать `state_initialized` в `problem_state_events`.
4. Материализовать snapshot и projections.

### 6.2. `apply_patch`

1. Загрузить snapshot по `expected_version`.
2. Проверить идемпотентность по `patch_id`.
3. Нормализовать операции:
   - gap fingerprint = SHA-256(`gap_type + title + description`);
   - `decision_type` dedupe по active decision of same type;
   - `readiness_type` upsert по unique key `(project_id, readiness_type)`;
   - `field_set.merge` только для dict/list.
4. Построить новый snapshot `version+1`.
5. Записать event и projections в одной транзакции.

### 6.3. Обработка противоречий

Если patch пытается:

- закрыть отсутствующий gap;
- заменить confirmed decision без `decision_supersede`;
- удалить blocking constraint;
- молча выставить blocking readiness в `ready`, если evidence отсутствует, хотя policy этого требует;

операция отклоняется `ProblemStateConflictError`.

Если patch предлагает значение, противоречащее текущему confirmed decision, store обязан:

1. отклонить patch целиком, либо
2. принять его только при явной `decision_supersede`.

Автоматическое молчаливое перетирание запрещено.

---

## 7. Инварианты

| Код | Инвариант |
|---|---|
| P1 | `ProblemStateSnapshot.version` монотонно возрастает на 1 |
| P2 | У проекта не более одного active gap с одинаковым `(gap_type, fingerprint)` |
| P3 | `DecisionRecord.status='confirmed'` допустим не более одного на `decision_type` |
| P4 | `GapRecord.status='closed'` ⇒ `closed_in_version IS NOT NULL` |
| P5 | `accepted_risk` не считается закрытием gap для Planner; Planner видит его отдельно |
| P6 | Любой change имеет `actor` и `correlation_id` либо `NULL` по явному правилу |
| P7 | `extensions` не содержит ключей core fields |
| P8 | Patch без `expected_version` запрещён |
| P9 | У проекта не более одной readiness dimension на `readiness_type` |
| P10 | `blocking` readiness в статусе `not_ready` или `unknown` должно учитываться planner'ом как admission constraint |
| P11 | Подключённый domain pack должен существовать в registry и иметь `status=active`, если проект находится в active lifecycle |
| P12 | `recipe_composition` обязана ссылаться на существующий base recipe и допустимые fragment refs |

---

## 8. Интеграция с другими компонентами

### 8.1. С шаблонами

`problem_state_effects` из [03_template_semantics.md](03_template_semantics.md) являются декларативной моделью того, какой patch task **может** произвести. Runtime возвращает фактический patch, а store проверяет его на соответствие шаблону.

### 8.2. С Planning Coordinator

Planner использует только `ProblemStateSnapshot` и projections, не event log.

Отдельно Planner обязан читать:

- `problem_readiness`;
- активные gaps;
- confirmed/proposed decisions;
- `enabled_domain_packs`;
- `recipe_composition`;
- recipe-related readiness deficits.

### 8.3. С Validation Layer

Validation может:

- открывать новые gaps;
- закрывать gaps после successful checks;
- создавать risks;
- подтверждать или отклонять decisions.
- обновлять readiness dimensions по результатам reviews/checks.

Validation **не может** менять goal и raw user constraints без explicit human action.

---

## 9. Что вне области этой спеки

- Хранение blob-артефактов и summaries — [06_artifact_context.md](06_artifact_context.md).
- Task lifecycle и DAG — [02_task_store.md](02_task_store.md).
- Алгоритм выбора template кандидатов — [05_planning_coordinator.md](05_planning_coordinator.md).
