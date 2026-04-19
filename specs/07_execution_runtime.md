# Execution Runtime — спецификация

> **Статус:** v1.0 · Draft · 2026-04-18
> **Зависимости:** [00_overview.md](00_overview.md), [03_template_semantics.md](03_template_semantics.md), [06_artifact_context.md](06_artifact_context.md), [02_task_store.md](02_task_store.md)
> **Область:** единый runtime для LLM, скриптов, инструментов и сред исполнения.

---

## 1. Назначение и зона ответственности

### 1.1. Что делает
- Принимает execution request на основе `Task`, `Template` и `ContextManifest`.
- Выполняет задачу через LLM, script или hybrid executor.
- Управляет tool calls и execution environments.
- Записывает traces, usage, tool logs и run metadata.
- Возвращает structured result: outputs, traces, patch proposal, emitted gaps/findings.

### 1.2. Чего НЕ делает
- Не выбирает шаблон.
- Не собирает контекст.
- Не меняет task status напрямую.
- Не коммитит `ProblemStatePatch`.

---

## 2. Core contracts

### 2.1. Execution modes

```python
class ExecutorKind(StrEnum):
    LLM = "llm"
    SCRIPT = "script"
    HYBRID = "hybrid"
```

### 2.2. Tool model

```python
class SideEffectClass(StrEnum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    COMMAND_EXECUTION = "command_execution"
    EXTERNAL_WRITE = "external_write"

class ToolSpec(BaseModel):
    tool_id: str
    name: str
    description: str
    input_schema: dict[str, Any]
    side_effect_class: SideEffectClass
    timeout_seconds: int
    idempotent: bool = False
```

### 2.3. Request / result

```python
class ExecutionRequest(BaseModel):
    execution_run_id: ExecutionRunId
    project_id: ProjectId
    task_id: TaskId
    template_ref: TemplateRef
    executor_kind: ExecutorKind
    context_manifest_ref: ContextManifestRef
    problem_state_ref: ProblemStateRef
    tool_allowlist: list[str]
    budget: dict[str, int]
    actor: str
    correlation_id: UUID | None = None
```

```python
class ExecutionOutput(BaseModel):
    output_name: str
    artifact_ref: ArtifactRef

class ExecutionResult(BaseModel):
    execution_run_id: ExecutionRunId
    status: Literal["succeeded", "failed", "cancelled"]
    outputs: list[ExecutionOutput] = []
    proposed_problem_state_patch: ProblemStatePatch | None = None
    emitted_gaps: list[GapUpsert] = []
    trace_refs: list[ExecutionTraceRef] = []
    failure_code: str | None = None
    failure_message: str | None = None
```

---

## 3. Runtime subcomponents

### 3.1. `LLM Gateway`

Отвечает за:

- provider/model selection;
- request normalization;
- retries / fallback;
- structured output validation;
- usage accounting.

LLM Gateway получает уже готовый prompt bundle из Context Engine.  
Он **не** рендерит бизнес-логику сам.

### 3.2. `Script Runtime`

Исполняет Python entrypoints / контейнерные команды в изолированной среде.

### 3.3. `Tool Runtime`

Исполняет зарегистрированные tools по allowlist. Tool Runtime обязан:

- валидировать input schema;
- проверять side-effect policy;
- писать `tool_invocations`;
- возвращать deterministic result envelope.

### 3.4. `Environment Manager`

Управляет:

- `docker` environments;
- `conda` / `venv` script environments;
- stateful `jupyter` sessions;
- workspace mounts.

---

## 4. Хранение и DDL

```sql
CREATE TABLE execution_runs (
    execution_run_id      UUID         PRIMARY KEY,
    project_id            UUID         NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    task_id               UUID         NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    template_id           TEXT         NOT NULL,
    template_version      TEXT         NOT NULL,
    executor_kind         TEXT         NOT NULL CHECK (executor_kind IN ('llm','script','hybrid')),
    context_manifest_id   UUID         NOT NULL REFERENCES context_manifests(manifest_id) ON DELETE RESTRICT,
    started_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    finished_at           TIMESTAMPTZ,
    status                TEXT         NOT NULL CHECK (status IN ('running','succeeded','failed','cancelled')),
    usage                 JSONB        NOT NULL DEFAULT '{}'::jsonb,
    trace_artifact_ids    JSONB        NOT NULL DEFAULT '[]'::jsonb,
    error                 JSONB,
    correlation_id        UUID
);

CREATE TABLE tool_invocations (
    tool_invocation_id    UUID         PRIMARY KEY,
    execution_run_id      UUID         NOT NULL REFERENCES execution_runs(execution_run_id) ON DELETE CASCADE,
    tool_id               TEXT         NOT NULL,
    arguments             JSONB        NOT NULL,
    result                JSONB,
    status                TEXT         NOT NULL CHECK (status IN ('running','succeeded','failed','cancelled')),
    started_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    finished_at           TIMESTAMPTZ
);
```

---

## 5. API

```python
class ExecutionRuntime(Protocol):
    async def execute(self, *, request: ExecutionRequest) -> ExecutionResult: ...
    async def cancel(self, *, execution_run_id: ExecutionRunId) -> None: ...
    async def heartbeat(self, *, execution_run_id: ExecutionRunId) -> None: ...
```

---

## 6. Runtime semantics

### 6.1. Общий execution flow

1. Получить `ExecutionRequest`.
2. Загрузить `Template`, `ContextManifest`, `ProblemStateRef`.
3. Проверить `tool_allowlist` против `tool_policy`.
4. Запустить executor:
   - `llm`: LLM Gateway
   - `script`: Script Runtime
   - `hybrid`: orchestrator внутри worker
5. Сохранить traces и tool logs.
6. Вернуть `ExecutionResult`.

### 6.2. Обязательные ограничения

- Runtime видит только `ContextManifest`; прямое чтение любых других artifacts запрещено.
- Tool доступ ограничен пересечением:
  - `template.tool_policy.allowed_tool_ids`
  - runtime-level policy
  - developer/user approvals
- `EXTERNAL_WRITE` без explicit approval запрещён.

### 6.3. LLM structured output

Если `json_mode=True` или задан schema contract:

1. LLM Gateway валидирует JSON.
2. При невалидности допускается retry только в рамках `max_llm_calls`.
3. Невалидный JSON не может быть зарегистрирован как output artifact.

---

## 7. Failure mapping

Execution Runtime обязан нормализовать ошибки в `failure_code`:

- `context_missing`
- `tool_policy_violation`
- `tool_timeout`
- `llm_schema_invalid`
- `llm_budget_exceeded`
- `script_nonzero_exit`
- `environment_unavailable`
- `external_dependency_error`

Task Store решает, переводить ли задачу в `Failed` или `Queued` для retry, но runtime обязан вернуть machine-readable code.

---

## 8. Инварианты

| Код | Инвариант |
|---|---|
| R1 | Execution run всегда привязан к `ContextManifest` |
| R2 | Output artifacts immutable и создаются только через Artifact Store |
| R3 | Runtime не коммитит task status и ProblemState напрямую |
| R4 | Любой tool invocation записывается в `tool_invocations` |
| R5 | Запуск tool вне allowlist запрещён |
| R6 | `EXTERNAL_WRITE` без approval запрещён |

---

## 9. Интеграция

### 9.1. С Task Store

Task Router переводит задачу в `in_progress`, затем вызывает runtime. По `ExecutionResult` Task Store делает `mark_completed` / `mark_failed`.

### 9.2. С Problem State

Runtime может предложить patch, но store коммитит его только после validation.

### 9.3. С Validation Layer

Validation получает outputs и trace refs из `ExecutionResult`.

---

## 10. Что вне области этой спеки

- Semantic contract шаблонов — [03_template_semantics.md](03_template_semantics.md).
- Context selection — [06_artifact_context.md](06_artifact_context.md).
- Stage gates и human escalation — [08_validation_governance.md](08_validation_governance.md).
