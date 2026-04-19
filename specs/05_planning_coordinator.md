# Planning Coordinator — спецификация

> **Статус:** v1.1 · Draft · 2026-04-19
> **Зависимости:** [00_overview.md](00_overview.md), [03_template_semantics.md](03_template_semantics.md), [04_problem_state.md](04_problem_state.md), [02_task_store.md](02_task_store.md), [09_domain_packs.md](09_domain_packs.md)
> **Область:** детерминированный policy loop, который допускает, композирует и материализует следующий шаг.

---

## 1. Назначение и зона ответственности

### 1.1. Что делает
- Определяет активные domain packs проекта и композирует итоговый recipe.
- Собирает planning items из `ProblemState`, validation debt, user intents и системных follow-up'ов.
- Строит recipe obligations и readiness deficits.
- Находит кандидатов среди активных шаблонов.
- Выполняет admission checks и только потом score/selection.
- Создаёт новые задачи через Task Store.
- Логирует, **почему** был выбран конкретный шаблон.

### 1.2. Чего НЕ делает
- Не использует LLM как финальный механизм admission/selection.
- Не хранит доменную методологию вне шаблонов.
- Не содержит hard-coded knowledge о конкретных доменах; всё доменное поведение приходит из domain packs.
- Не исполняет задачи и не собирает контекст.
- Не закрывает gaps напрямую.

### 1.3. Главный принцип

Planning Coordinator — **тонкий policy engine**, а не второй интеллект системы.  
Он опирается на explicit semantics шаблонов, readiness model и recipes, и никогда не пытается “догадаться” вместо них.

LLM может косвенно влиять на planning через результаты уже выполненных `meta_analysis`/`review` шаблонов, но не через прямой “planner prompt”.

Дополнение:

- Planner работает не только с base recipe, но и с composed recipe проекта;
- composed recipe собирается из base recipe и активированных domain packs;
- именно composed recipe определяет, какие доменные шаги обязательны до перехода к core-task.

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
        "readiness_deficit",
        "recipe_obligation",
    ]
    stage_gate: StageGate
    gap_type: NamespacedId | None = None
    decision_type: NamespacedId | None = None
    readiness_type: NamespacedId | None = None
    recipe_id: NamespacedId | None = None
    recipe_step_id: str | None = None
    severity: Literal["low", "medium", "high", "critical"] | None = None
    blocking: bool = False
    payload: dict[str, Any] = {}
```

### 2.2. Источники planning items

- Активные gaps из `ProblemState`.
- Активные readiness deficits из `ProblemState`.
- Pending validation findings.
- Gate entry actions при открытии нового `StageGate`.
- Explicit human/developer requests.
- System-generated follow-up intents после completion/retry/invalidate.
- Recipe obligations: обязательные meta-passes/reviews, ещё не выполненные для текущего класса задачи.
- Domain-pack obligations: обязательные доменные расширения, встроенные в composed recipe.

### 2.2.1. Recipe composition

Перед построением obligations planner обязан определить актуальный composed recipe проекта.

Минимальная canonical view:

```python
class ComposedRecipe(BaseModel):
    composed_recipe_id: NamespacedId
    base_recipe_ref: str
    fragment_refs: list[str]
    steps: list[RecipeStep]
```

Правила:

1. На project bootstrap выбирается base recipe.
2. По `domain_signals`, `enabled_domain_packs`, human input и confirmed decisions planner определяет, какие domain packs активны.
3. Recipe Composer встраивает recipe fragments этих packs в base recipe.
4. Итоговый composed recipe сохраняется в `ProblemState`.

Нормативное правило:

- planner не должен сам “знать”, какие frontend- или rag-шаги нужно добавить к ТЗ;
- это должно определяться через domain packs и recipe fragments.

### 2.3. Recipe model

Planner работает не с “плоским списком шаблонов”, а с recipe-driven orchestration.

```python
class RecipeObligation(BaseModel):
    recipe_id: NamespacedId
    step_id: str
    template_role: Literal["core_task", "meta_analysis", "review", "repair", "escalation"]
    required: bool
    satisfied: bool
    source_item_id: UUID | None = None
```

Нормативное правило:

- `core_task` нельзя materialize, если для того же recipe есть невыполненные обязательные `meta_analysis` или `review` steps.

---

## 3. Candidate admission and selection

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

После этого planner обязан получить:

- `base recipe`;
- `active domain packs`;
- `recipe fragments`, совместимые с этим recipe;
- `composed recipe`, который и станет источником obligations.

Одновременно Planner загружает recipe definitions и актуальное состояние readiness.

### 3.2. Admission checks

Шаблон исключается, если:

- `status != active`;
- `planner_visibility=internal_only` и trigger не системный;
- activation predicates false;
- есть blocking readiness deficit, несовместимый с template role;
- recipe требует сначала другой обязательный step;
- существует active task той же `dedup_key`;
- gate закрыт для новых задач этого типа;
- у задачи нет шанса собрать hard inputs по доступным artifact roles / problem fields.

Отдельное нормативное правило:

- `core_task` не проходит admission, если есть обязательный `meta_analysis`/`review` pass того же recipe в статусе `pending`;
- `review` не проходит admission, если отсутствует review target;
- `repair` не проходит admission, если нет активных findings соответствующего scope.

---

## 4. Scoring model

Score должен быть детерминированным и explainable.

### 4.1. Базовая формула

```python
score = (
    gap_score
    + recipe_score
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
| `recipe_score` | `+80`, если candidate закрывает обязательный pending recipe step; `0` иначе |
| `stage_score` | `+20`, если `current_stage_gate` входит в `preferred_stage_gates`; иначе `0` |
| `template_priority_score` | `semantics.priority_hint` |
| `readiness_score` | `+30`, если все hard inputs уже доступны; `+10`, если нужны только summaries; `-50`, если missing hard inputs; `-1000`, если blocking readiness deficit делает `core_task` недопустимым |
| `human_override_score` | `+1000`, если developer явно форсировал template |
| `duplicate_penalty` | `1000`, если dedup conflict; иначе `0` |
| `cooldown_penalty` | `20`, если последний task той же family завершился менее `cooldown_seconds` назад |

### 4.3. Tie-breakers

При равном score:

1. Более узкий `closes_gaps` wins.
2. `meta_analysis` / `review` win против `core_task`, если они удовлетворяют обязательный pending recipe step.
3. Более высокий `max_input_tokens` loses.
4. Меньший `template_id` wins lexicographically.

Это делает выбор полностью детерминированным.

---

## 5. Материализация задач

### 5.1. `TaskMaterializationSpec`

```python
class TaskMaterializationSpec(BaseModel):
    project_id: ProjectId
    template_ref: TemplateRef
    recipe_id: NamespacedId | None = None
    recipe_step_id: str | None = None
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
3. Planner обязан сначала удовлетворять recipe obligations текущего task class, а уже затем materialize `core_task`.
4. Если execution path невозможен, Planner не создаёт task; вместо этого он:
   - открывает новый gap, либо
   - создаёт readiness deficit, либо
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

Отдельно planner может инициировать only-append update в `ProblemState`:

- `domain_pack_enable`
- `recipe_composition_set`

Но не должен менять произвольные problem fields.

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
    recipe_id: NamespacedId | None = None
    recipe_step_id: str | None = None
    score: int
    dedup_key: str
    status: Literal["materialized", "skipped_duplicate", "skipped_guard", "escalated"]
    reasons: list[str]
    created_task_id: TaskId | None = None
    admission_checks: list[str] = []
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
    recipe_id             TEXT,
    recipe_step_id        TEXT,
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
2. Считать active tasks, validation debt, readiness и stage gate status.
3. Определить активные domain packs.
4. Собрать или перечитать composed recipe.
5. Построить recipe obligations для текущего project state.
6. Собрать `PlanningItem`'ы.
7. Для каждого item:
   - получить candidate templates;
   - выполнить admission checks;
   - вычислить score;
   - выбрать победителя;
   - materialize task или записать skip reason.
8. Сохранить `planning_runs` + `planning_decisions`.
9. Вернуть decisions.

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
| C7 | Planner не использует LLM для определения готовности входа |
| C8 | `core_task` не materialize'ится, пока не выполнены обязательные recipe meta-passes |
| C9 | Доменные расширения recipe определяются через active domain packs, а не через hard-coded planner branches |

---

## 11. Что вне области этой спеки

- Семантика самих templates и recipes — [03_template_semantics.md](03_template_semantics.md).
- Context assembly — [06_artifact_context.md](06_artifact_context.md).
- Runtime execution — [07_execution_runtime.md](07_execution_runtime.md).
