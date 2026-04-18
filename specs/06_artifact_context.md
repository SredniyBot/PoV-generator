# Artifact Store и Context Engine — спецификация

> **Статус:** v1.0 · Draft · 2026-04-18
> **Зависимости:** [00_overview.md](00_overview.md), [03_template_semantics.md](03_template_semantics.md), [04_problem_state.md](04_problem_state.md), [02_task_store.md](02_task_store.md)
> **Область:** immutable artifacts, derived representations, semantic retrieval и task-local context assembly.

---

## 1. Назначение и зона ответственности

### 1.1. Что делает
- Хранит blob-артефакты проекта и их metadata.
- Хранит derived artifacts: summaries, structured extracts, chunks, embeddings.
- Собирает минимальный `ContextManifest` под конкретную задачу.
- Поддерживает contract-driven retrieval по `input_requirements` и `context_policy`.

### 1.2. Чего НЕ делает
- Не решает, какой шаблон запускать.
- Не изменяет `ProblemState` самостоятельно.
- Не хранит lifecycle задач.
- Не исполняет tools и LLM calls.

### 1.3. Главный принцип

**Никакая задача не получает весь проектный контекст напрямую.**  
Execution всегда работает только с `ContextManifest`.

---

## 2. Модель артефактов

### 2.1. Базовые сущности

```python
class ArtifactRecord(BaseModel):
    artifact_ref: ArtifactRef
    project_id: ProjectId
    artifact_role: NamespacedId | None = None
    schema_ref: SchemaRef | None = None
    title: str | None = None
    description: str | None = None
    tags: list[str] = []
    created_by_task_id: TaskId | None = None
    provenance: Provenance
```

```python
class DerivedArtifact(BaseModel):
    artifact_ref: ArtifactRef
    parent_artifact_id: UUID
    derived_kind: Literal["structured_extract", "short_summary", "task_summary", "chunk_set", "embedding_set"]
    schema_ref: SchemaRef | None = None
    provenance: Provenance
```

```python
class ArtifactChunk(BaseModel):
    chunk_id: UUID
    artifact_id: UUID
    ordinal: int
    content_text: str
    token_count: int
    embedding_vector_id: UUID | None = None
    metadata: dict[str, Any] = {}
```

### 2.2. Summary levels

`ContextPolicy.summary_levels_allowed` опирается на фиксированные уровни:

- `raw` — оригинальный артефакт или canonical JSON render.
- `structured` — schema-guided extraction.
- `short` — краткое summary без domain-specific фокуса.
- `task_specific` — summary, построенное под конкретный `template_ref`.

`task_specific` summary всегда считается отдельным derived artifact и не переиспользуется другими templates без явного allow.

---

## 3. Context manifest

### 3.1. Модель

```python
class ContextItem(BaseModel):
    item_id: UUID
    item_type: Literal[
        "problem_field",
        "artifact_raw",
        "artifact_summary",
        "artifact_chunk",
        "task_output",
        "instruction",
    ]
    source_ref: str
    title: str
    token_estimate: int
    required: bool
    priority: int

class ContextBudget(BaseModel):
    max_input_tokens: int
    reserved_for_system: int
    reserved_for_output: int
    used_tokens: int

class ContextManifest(BaseModel):
    manifest_id: ContextManifestId
    project_id: ProjectId
    task_id: TaskId
    template_ref: TemplateRef
    problem_state_ref: ProblemStateRef
    budget: ContextBudget
    items: list[ContextItem]
    excluded_items: list[str]
    retrieval_queries: list[str]
    input_fingerprint: str
    created_at: datetime
```

### 3.2. Нормативные правила

- Каждый execution run обязан ссылаться на `ContextManifest`.
- В `items` включаются только данные, разрешённые шаблоном.
- `excluded_items` хранит отброшенные кандидаты при overflow/dedup.
- `input_fingerprint` = SHA-256 от ids/version всех фактических источников контекста.

---

## 4. Context assembly algorithm

### 4.1. Вход

```python
class ContextAssemblyRequest(BaseModel):
    project_id: ProjectId
    task_id: TaskId
    template_ref: TemplateRef
    problem_state_ref: ProblemStateRef
    input_requirements: list[InputRequirement]
    context_policy: ContextPolicy
```

### 4.2. Шаги сборки

1. **Resolve explicit inputs**
   - обработать `hard`, `soft`, `semantic` requirements;
   - direct selectors имеют приоритет над retrieval.
2. **Resolve problem fields**
   - загрузить `required_problem_fields`;
   - затем `optional_problem_fields`.
3. **Normalize**
   - выбрать raw artifact, если помещается и `allow_summary=False`;
   - иначе перейти к summary/extract согласно policy.
4. **Budgeting**
   - отсортировать по `required DESC, priority DESC, token_estimate ASC`;
   - применить overflow strategy.
5. **Manifest**
   - записать manifest + links на фактические items.

### 4.3. Overflow strategies

- `summarize` — заменить large raw item на allowed summary.
- `decompose` — context builder возвращает `ContextOverflowError`, а Planner создаёт decomposition/summary task.
- `fail` — задача получает `Failed` до execution.
- `escalate` — создаётся escalation ticket.

### 4.4. Semantic retrieval

`semantic` inputs разрешены только если:

- requirement `kind=semantic`, либо
- template explicitly allows semantic chunks in `input_priorities`.

`semantic` retrieval не может подменять missing `hard` artifact.

---

## 5. Хранение и DDL

```sql
CREATE TABLE artifacts (
    artifact_id           UUID         PRIMARY KEY,
    project_id            UUID         NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    artifact_type         TEXT         NOT NULL,
    artifact_role         TEXT,
    mime_type             TEXT         NOT NULL,
    s3_uri                TEXT         NOT NULL,
    sha256                CHAR(64)     NOT NULL,
    size_bytes            BIGINT       NOT NULL,
    schema_ref            TEXT,
    title                 TEXT,
    description           TEXT,
    tags                  JSONB        NOT NULL DEFAULT '[]'::jsonb,
    created_by_task_id    UUID         REFERENCES tasks(task_id) ON DELETE SET NULL,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    provenance            JSONB        NOT NULL
);

CREATE TABLE artifact_derivatives (
    derivative_id         UUID         PRIMARY KEY,
    artifact_id           UUID         NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
    derived_artifact_id   UUID         NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
    derived_kind          TEXT         NOT NULL,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (artifact_id, derived_artifact_id)
);

CREATE TABLE artifact_chunks (
    chunk_id              UUID         PRIMARY KEY,
    artifact_id           UUID         NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
    ordinal               INT          NOT NULL,
    content_text          TEXT         NOT NULL,
    token_count           INT          NOT NULL,
    embedding             VECTOR(1536),
    metadata              JSONB        NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (artifact_id, ordinal)
);

CREATE TABLE context_manifests (
    manifest_id           UUID         PRIMARY KEY,
    project_id            UUID         NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    task_id               UUID         NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    template_id           TEXT         NOT NULL,
    template_version      TEXT         NOT NULL,
    problem_state_version INT          NOT NULL,
    budget                JSONB        NOT NULL,
    excluded_items        JSONB        NOT NULL DEFAULT '[]'::jsonb,
    retrieval_queries     JSONB        NOT NULL DEFAULT '[]'::jsonb,
    input_fingerprint     CHAR(64)     NOT NULL,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE context_manifest_items (
    manifest_item_id      UUID         PRIMARY KEY,
    manifest_id           UUID         NOT NULL REFERENCES context_manifests(manifest_id) ON DELETE CASCADE,
    item_type             TEXT         NOT NULL,
    source_ref            TEXT         NOT NULL,
    title                 TEXT         NOT NULL,
    token_estimate        INT          NOT NULL,
    required              BOOLEAN      NOT NULL,
    priority              INT          NOT NULL
);
```

---

## 6. API

```python
class ArtifactStore(Protocol):
    async def put_artifact(self, *, record: ArtifactRecord, content: bytes) -> ArtifactRef: ...
    async def create_derived(self, *, parent_artifact_id: UUID, record: ArtifactRecord, content: bytes) -> ArtifactRef: ...
    async def get_artifact(self, *, artifact_id: UUID) -> ArtifactRecord: ...
    async def list_by_role(self, *, project_id: ProjectId, artifact_role: NamespacedId) -> list[ArtifactRecord]: ...

class ContextEngine(Protocol):
    async def assemble(self, *, request: ContextAssemblyRequest) -> ContextManifest: ...
    async def render_prompt_bundle(self, *, manifest_id: ContextManifestId) -> dict[str, Any]: ...
```

---

## 7. Инварианты

| Код | Инвариант |
|---|---|
| A1 | `ArtifactRef` всегда ссылается на immutable blob |
| A2 | Summary/extract/chunk-set — это отдельный artifact, а не field внутри исходного артефакта |
| A3 | Execution без `ContextManifest` запрещён |
| A4 | `hard` input не может быть silently заменён semantic retrieval |
| A5 | `task_specific` summary не переиспользуется другим template без явного allow |
| A6 | `input_fingerprint` полностью определяет фактический состав контекста |

---

## 8. Интеграция

### 8.1. С Task Store

`task_inputs` и `task_outputs` хранят только refs. Artifact Store хранит сам артефакт и derived representations.

### 8.2. С Problem State

Context Engine может включать только `required_problem_fields` и `optional_problem_fields`.  
Он не имеет права сериализовать весь snapshot целиком по умолчанию.

### 8.3. С Execution Runtime

Runtime получает `ContextManifestRef` и prompt bundle, но не запрашивает произвольные artifacts вне manifest.

---

## 9. Что вне области этой спеки

- Выбор template кандидатов — [05_planning_coordinator.md](05_planning_coordinator.md).
- Runtime adapters и tools — [07_execution_runtime.md](07_execution_runtime.md).
