# Template Registry — спецификация

> **Статус:** v1.0 · Draft · 2026-04-15
> **Зависимости:** [00_overview.md](00_overview.md)
> **Область:** реализация компонента 2.3 Template Registry из [ТЗ Архитектура.md](../Downloads/ТЗ%20Архитектура.md)

---

## 1. Назначение и зона ответственности

### 1.1. Что делает
- Хранит **декларативные описания классов задач** (шаблоны) — что задача принимает на вход, что обязана произвести, как исполняется, как декомпозируется
- Предоставляет **read-only API** для получения шаблона по `(id, version)` и перечисления
- Валидирует шаблоны (как по JSON Schema, так и по структурным инвариантам) при загрузке и публикации
- Ведёт индекс активных и устаревших версий в PostgreSQL для быстрых выборок

### 1.2. Чего НЕ делает
- Не создаёт задачи и не управляет их статусами (это Task Store)
- Не выполняет задачи и не взаимодействует с LLM (это бизнес-модули)
- Не собирает контекст и не ищет артефакты (это Context Engine)
- Не редактирует шаблоны через API (source of truth — Git-репозиторий; правки — через коммит)

### 1.3. Границы с соседями

| Компонент | Контракт |
|---|---|
| **Task Router** | вызывает `registry.load(id, version)` перед созданием задачи; получает `Template` или `NotFoundError` |
| **Context Engine** | читает `template.input_requirements` и резолвит их в конкретные артефакты |
| **Stage-Gate Manager** | читает `template.metadata.stage_gate` для агрегатов |
| **CLI / CI** | вызывает `registry.validate_all()` в pre-commit и в CI |

---

## 2. Модель шаблона

### 2.1. Общие поля (все типы)

| Поле | Тип | Обязательность | Комментарий |
|---|---|---|---|
| `id` | `TemplateId` (slug) | required | См. [00_overview §2.5](00_overview.md#25-идентификаторы) |
| `version` | `TemplateVersion` (SemVer) | required | Иммутабельна после публикации |
| `type` | `TemplateType` | required | `composite` / `executable` / `dynamic` |
| `description` | `string` | required | ≤ 500 символов, человекочитаемый абзац |
| `owner` | `string` | required | ответственный за шаблон (email/handle) |
| `status` | `TemplateStatus` | required | `draft` / `active` / `deprecated` |
| `domain` | `string` | required | `ds`, `rag`, `ml`, `common`, ... (см. §2.7) |
| `input_requirements` | `list[InputRequirement]` | required | может быть пустым |
| `output_contract` | `list[OutputArtifactSpec]` | required | ≥ 1 элемент, см. §2.3 |
| `escalation` | `EscalationPolicy` | required | лимиты ретраев/токенов/времени |
| `metadata` | `TemplateMetadata` | required | labels, stage_gate, tags |
| `provenance` | `Provenance` | required при публикации | кто/когда создал версию |

### 2.2. `InputRequirement`

```python
class InputRequirement(BaseModel):
    name: str                            # slug, уникален в пределах шаблона
    kind: RequirementKind                # hard / soft / semantic
    artifact_type: str                   # "schema", "code", "report", ...
    schema_ref: SchemaRef | None = None  # валидация содержимого артефакта
    selector: Selector                   # правило выбора артефакта
    description: str                     # человекочитаемое пояснение
```

`Selector` — discriminated union:

```python
class SelectorByArtifactId(BaseModel):
    kind: Literal["by_artifact_id"] = "by_artifact_id"
    artifact_id: UUID                    # точная ссылка (для тестов/fixture)

class SelectorByProducer(BaseModel):
    kind: Literal["by_producer"] = "by_producer"
    producer_template_id: TemplateId     # артефакт сгенерирован задачей из этого шаблона
    output_name: str                     # имя из output_contract производителя

class SelectorByType(BaseModel):
    kind: Literal["by_type"] = "by_type"
    artifact_type: str                   # последний артефакт такого типа в проекте

class SelectorSemantic(BaseModel):
    kind: Literal["semantic"] = "semantic"
    query: str                           # промпт-запрос для Context Engine
    k: int = 5                           # top-k
    filter: dict[str, str] = Field(default_factory=dict)
```

Инвариант: если `kind == semantic`, то `Selector.kind == "semantic"`. Иначе — любой из `by_*`.

### 2.3. `OutputArtifactSpec`

```python
class OutputArtifactSpec(BaseModel):
    name: str                            # slug, уникален в пределах шаблона
    artifact_type: str
    mime_type: str
    schema_ref: SchemaRef | None = None  # обязательная JSON-валидация
    description: str
    required: bool = True                # False → артефакт опционален
    multiplicity: Literal["one", "many"] = "one"
```

Сохранение артефакта в `task_outputs` обязано содержать минимум все `required=True` элементы. Task Store валидирует до `Completed`.

### 2.4. Специализация по типу шаблона

#### `composite` — контейнер
```python
class CompositeChildRef(BaseModel):
    alias: str                           # уникальное имя шага внутри composite
    template_id: TemplateId
    template_version: TemplateVersion | Literal["latest"] = "latest"
    depends_on: list[str] = []           # aliases других child'ов этого же composite
    input_bindings: dict[str, InputBinding]   # mapping child.input_name → источник
    enabled_if: Expression | None = None # см. §2.8

class CompositeSpec(BaseModel):
    children: list[CompositeChildRef]    # ≥ 1
    bubble_up_outputs: dict[str, OutputBinding]  # child.output → self.output
```

Инварианты:
- Все `alias` уникальны.
- Все `depends_on` ссылаются на существующие `alias`.
- Граф `depends_on` ацикличен.
- Каждый `input_bindings[x]` покрывает `x ∈ child.template.input_requirements` с `kind = hard`.
- Каждый элемент `output_contract` composite'а либо имеет `bubble_up_outputs` mapping, либо помечен `multiplicity="many"` и собирается из выходов children.

#### `executable` — лист
```python
class ExecutableSpec(BaseModel):
    executor: ExecutorKind               # "llm" | "script" | "hybrid"
    llm: LlmExecutorConfig | None = None
    script: ScriptExecutorConfig | None = None

class LlmExecutorConfig(BaseModel):
    model: str                           # "anthropic:claude-sonnet-4-6", ...
    prompt_ref: SchemaRef                # ссылка на файл промпта в репо
    tool_allowlist: list[str] = []       # ids инструментов
    token_budget: TokenBudget            # input/output/total
    temperature: float = 0.0
    json_mode: bool = False

class ScriptExecutorConfig(BaseModel):
    entrypoint: str                      # "pov_lab.scripts.foo:main"
    timeout_seconds: int                 # 1..3600
    environment: EnvironmentRef          # docker image ref / conda env
    resources: ResourceLimits            # cpu, memory

class TokenBudget(BaseModel):
    input_tokens_max: int                # ≥ 512
    output_tokens_max: int               # ≥ 128
    total_tokens_max: int                # ≥ input+output
```

Инварианты:
- `executor == "llm"` ⇒ `llm` обязателен, `script` — null.
- `executor == "script"` ⇒ наоборот.
- `executor == "hybrid"` ⇒ оба обязательны; в этом случае исполнитель сам решает, когда вызывать LLM.

#### `dynamic` — динамическая декомпозиция
```python
class DynamicSpec(BaseModel):
    decision_executor: LlmExecutorConfig # промпт для принятия решения
    max_depth: int                       # 1..5
    max_children: int                    # 1..20
    decomposition_rules: list[DecompositionRule]
    fallback_template: TemplateRef       # что делать, если декомпозиция не удалась

class DecompositionRule(BaseModel):
    condition: Expression                # python-подобное выражение над payload
    action: Literal["execute", "decompose"]
    child_templates: list[TemplateRef] = []  # требуется при action=decompose
```

Инварианты:
- `decomposition_rules` не пустое.
- У каждого правила `action=decompose` непустое `child_templates`.
- `fallback_template.id` существует в реестре.

### 2.5. `EscalationPolicy`

```python
class EscalationPolicy(BaseModel):
    max_attempts: int = 3                # полные перезапуски задачи
    max_llm_calls: int = 10              # суммарно по всем attempts
    wall_clock_seconds: int = 1800       # общий таймаут
    on_escalation: TemplateRef | None = None  # куда эскалировать (или пустое → Interruption Gateway)
```

### 2.6. `TemplateMetadata`

```python
class TemplateMetadata(BaseModel):
    stage_gate: StageGate                # для Stage-Gate Manager
    labels: dict[str, str] = {}          # произвольные key:value
    tags: list[str] = []                 # свободный набор
    docs_url: str | None = None
    since_version: TemplateVersion | None = None  # минимальная версия платформы
```

### 2.7. Домены (namespace)

Домен определяет логическую группу и соответствующую поддиректорию в `templates/`. Разрешённые значения перечислены в отдельном файле `templates/domains.yaml`:

```yaml
domains:
  - id: common
    description: Кросс-доменные шаблоны (intake, validation, report)
  - id: ds
    description: Data Science общие задачи
  - id: rag
    description: RAG-специфичные задачи (index, retrieval, eval)
  - id: ml
    description: Классические ML-задачи (train, tune, evaluate)
```

Шаблон с `domain: "rag"` должен лежать в `templates/rag/<id>.yaml`. Валидатор проверяет соответствие.

### 2.8. `Expression` — простой boolean DSL

Для `enabled_if` / `condition` используется безопасное выражение над `payload`:

```
EXPR   := EXPR "and" EXPR | EXPR "or" EXPR | "not" EXPR | "(" EXPR ")" | PRED
PRED   := PATH OP VALUE
PATH   := "payload." IDENT ("." IDENT | "[" INT "]")*
OP     := "==" | "!=" | ">" | ">=" | "<" | "<=" | "in" | "contains"
VALUE  := STRING | NUMBER | BOOL | "null" | "[" VALUE ("," VALUE)* "]"
```

Парсинг и вычисление — `pov_lab_templates.expression`. Любое другое выражение запрещено (без `import`, без `eval`).

---

## 3. Хранилище

### 3.1. Файловая раскладка (source of truth)

```
templates/
├── domains.yaml                        # см. §2.7
├── prompts/                            # промпты для LlmExecutorConfig.prompt_ref
│   └── <prompt_name>@<version>.md
├── schemas/                            # JSON Schema для SchemaRef.scope == "project"
│   └── <scope>.<name>@<version>.json
├── common/
│   ├── requirements_intake@1.0.0.yaml
│   └── report_render@1.0.0.yaml
├── ds/
│   └── data_profiling@1.0.0.yaml
├── rag/
│   ├── index_build@1.0.0.yaml
│   └── retrieval_eval@1.0.0.yaml
└── ml/
    └── baseline_train@1.0.0.yaml
```

Правила имени файла: `<template_id>@<version>.yaml`. Каждая версия — отдельный файл. Удаление файла при наличии `tasks.template_version` ссылок запрещено (проверяется в CI, см. §6).

### 3.2. PostgreSQL индекс

Реестр строит материализованный индекс при старте и при `reload()`. Это **проекция**, пересоздаваемая из файлов.

```sql
CREATE TABLE template_index (
    template_id        TEXT        NOT NULL,
    version            TEXT        NOT NULL,        -- SemVer
    type               TEXT        NOT NULL,        -- composite/executable/dynamic
    status             TEXT        NOT NULL,        -- draft/active/deprecated
    domain             TEXT        NOT NULL,
    stage_gate         TEXT        NOT NULL,
    owner              TEXT        NOT NULL,
    file_sha256        CHAR(64)    NOT NULL,        -- для детекции изменений
    file_path          TEXT        NOT NULL,
    loaded_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (template_id, version)
);

CREATE INDEX idx_template_index_domain_status ON template_index (domain, status);
CREATE INDEX idx_template_index_stage_gate    ON template_index (stage_gate);
CREATE INDEX idx_template_index_metadata_gin  ON template_index USING GIN (metadata);

CREATE TABLE template_alias (
    template_id        TEXT        NOT NULL,
    alias              TEXT        NOT NULL,        -- "latest", "stable"
    version            TEXT        NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (template_id, alias),
    FOREIGN KEY (template_id, version) REFERENCES template_index (template_id, version) ON DELETE RESTRICT
);
```

`template_alias` фиксирует `latest` (вычисляется как max(version) с `status='active'`) и любые ручные алиасы.

### 3.3. Иммутабельность и версионирование
- Опубликованная (`status=active`) версия **иммутабельна**: SHA-256 файла фиксируется в `template_index.file_sha256`. Попытка `reload()` при расхождении → `TemplateIntegrityError`.
- `draft` версии могут изменяться; при переходе `draft → active` SHA-256 фиксируется.
- `deprecated` — версия не предлагается новым задачам, но остаётся доступной для уже созданных и для воспроизведения.
- Правило перехода версии (SemVer):
  - **MAJOR** — несовместимое изменение `input_requirements` или `output_contract`.
  - **MINOR** — обратно совместимые дополнения.
  - **PATCH** — исправления описаний, промптов без изменения контрактов.

---

## 4. Валидация

### 4.1. Два уровня
1. **Schema-level** — JSON Schema (`specs/schemas/template.schema.json`), проверяет типы, enums, обязательность.
2. **Structural-level** — код `pov_lab_templates.validator`, проверяет инварианты из §2 (циклы, резолвинг references, уникальность).

### 4.2. Структурные правила
| № | Правило | Ошибка |
|---|---|---|
| V1 | `id` уникален в рамках `(template_id, version)` | `DuplicateTemplateError` |
| V2 | `file_path` соответствует `domain/id@version.yaml` | `MisplacedTemplateError` |
| V3 | Все `SchemaRef` резолвятся в существующие файлы | `UnresolvedSchemaRefError` |
| V4 | Все `TemplateRef` резолвятся (кроме self-reference) | `UnresolvedTemplateRefError` |
| V5 | `composite.children[*].depends_on` — ацикличный граф | `CircularDependencyError` |
| V6 | Для `composite` — каждое `output_contract` имеет bubble-up путь | `MissingBubbleUpError` |
| V7 | Для `executable` — executor-config соответствует `executor` kind | `InvalidExecutorConfigError` |
| V8 | Для `dynamic` — `fallback_template` существует | `UnresolvedTemplateRefError` |
| V9 | `token_budget.total >= input + output` | `InvalidBudgetError` |
| V10 | Транзитивно composite не ссылается на себя | `CircularTemplateError` |
| V11 | `escalation.on_escalation` (если задан) существует | `UnresolvedTemplateRefError` |
| V12 | Expression парсится | `InvalidExpressionError` |
| V13 | `metadata.stage_gate` — один из `StageGate` | `InvalidStageGateError` |

Все ошибки наследуют `TemplateValidationError` (§7).

### 4.3. `ValidationReport`

```python
class ValidationReport(BaseModel):
    timestamp: datetime
    total_templates: int
    ok: list[TemplateRef]
    errors: list[TemplateValidationIssue]
    warnings: list[TemplateValidationIssue]
    duration_ms: int

class TemplateValidationIssue(BaseModel):
    template_ref: TemplateRef
    code: str                            # "V5_CIRCULAR_DEPENDENCY", ...
    message: str
    path: str | None                     # JSONPath внутри YAML
    severity: Literal["error", "warning"]
```

---

## 5. Python API

### 5.1. Протоколы

```python
# src/pov_lab_templates/ports.py
class TemplateRegistry(Protocol):
    def load(self, template_id: TemplateId, version: TemplateVersion | Literal["latest"] = "latest") -> Template: ...
    def resolve(self, ref: TemplateRef) -> Template: ...
    def list(self,
             *,
             domain: str | None = None,
             status: TemplateStatus | None = None,
             stage_gate: StageGate | None = None,
             label: tuple[str, str] | None = None) -> list[TemplateSummary]: ...
    def validate_all(self) -> ValidationReport: ...
    def reload(self) -> ReloadReport: ...
    def exists(self, template_id: TemplateId, version: TemplateVersion | None = None) -> bool: ...
```

### 5.2. Основная реализация

```python
# src/pov_lab_templates/registry.py
class FileSystemTemplateRegistry(TemplateRegistry):
    def __init__(
        self,
        root: Path,                           # путь к templates/
        pg_index: TemplateIndexRepository,    # работает с template_index/alias
        validator: TemplateValidator,
        cache_size: int = 256,
    ): ...
```

- Вся логика синхронна (файлы + кеш). Внутри может быть вызов блокирующего драйвера к БД; предоставляется thin async-обёртка для интеграции с async-вызывающими.
- Кеш — `functools.lru_cache(maxsize=cache_size)` на `load()`. Инвалидируется в `reload()`.

### 5.3. Семантика методов

| Метод | Описание | Возможные ошибки |
|---|---|---|
| `load(id, version="latest")` | Возвращает полный `Template`. `"latest"` резолвится через `template_alias`. Читает из кеша, на miss — из файла. | `TemplateNotFoundError`, `TemplateValidationError` |
| `resolve(ref)` | Эквивалент `load(ref.id, ref.version)`. | те же |
| `list(...)` | Выбор из `template_index` по фильтрам. Возвращает `TemplateSummary` (без полного содержимого). | — |
| `validate_all()` | Пробегает по всем файлам, валидирует. Не мутирует индекс. | — (ошибки попадают в отчёт) |
| `reload()` | Пересканирует директорию, валидирует, обновляет `template_index` и `template_alias` в одной транзакции. | `TemplateValidationError` (прерывает reload; старый индекс остаётся) |
| `exists(...)` | Быстрая проверка по индексу. | — |

### 5.4. `Template` (pydantic model)

```python
class Template(BaseModel):
    # Общие поля
    id: TemplateId
    version: TemplateVersion
    type: TemplateType
    description: str
    owner: str
    status: TemplateStatus
    domain: str
    input_requirements: list[InputRequirement]
    output_contract: list[OutputArtifactSpec]
    escalation: EscalationPolicy
    metadata: TemplateMetadata
    provenance: Provenance

    # Специализация (только одно из полей заполнено)
    composite: CompositeSpec | None = None
    executable: ExecutableSpec | None = None
    dynamic: DynamicSpec | None = None

    model_config = ConfigDict(frozen=True, extra="forbid")
```

pydantic validator проверяет, что ровно одно из `composite/executable/dynamic` соответствует `type`.

### 5.5. `TemplateSummary`

```python
class TemplateSummary(BaseModel):
    id: TemplateId
    version: TemplateVersion
    type: TemplateType
    status: TemplateStatus
    domain: str
    stage_gate: StageGate
    owner: str
    description: str
```

---

## 6. Операционные процессы

### 6.1. Pre-commit hook
```
pov-lab-templates validate --fail-on warning
```
Проверяет затронутые файлы + их транзитивные зависимости. Конфиг — `.pre-commit-config.yaml`.

### 6.2. CI проверки (обязательные)
1. `pov-lab-templates validate --all` — полная валидация.
2. `pov-lab-templates check-integrity` — проверка, что иммутабельные версии не изменились.
3. `pov-lab-templates list-deleted --against main` — запрещает удаление файла, на который ссылаются существующие задачи в staging/prod (проверка делается в deploy pipeline, не в PR CI).

### 6.3. Публикация новой версии
1. Разработчик создаёт `templates/<domain>/<id>@<version>.yaml` со `status: draft`.
2. Пишет тесты (golden-fixture) в `tests/templates/<id>@<version>/`.
3. CI валидирует.
4. На approve PR → `status` меняется на `active`.
5. После merge — деплой service вызывает `registry.reload()`.

### 6.4. Hot-reload в dev

В dev-режиме активируется `watchfiles`-подписка. Изменение файла → `reload()` только затронутой версии (partial reload). В production `reload()` вызывается только явно (deploy, admin endpoint).

---

## 7. Ошибки

```python
# src/pov_lab_templates/errors.py
class TemplateError(PovLabError): ...
class TemplateNotFoundError(TemplateError, NotFoundError): ...
class TemplateValidationError(TemplateError, ValidationError):
    def __init__(self, issues: list[TemplateValidationIssue]): ...
class TemplateIntegrityError(TemplateError, IntegrityError): ...
class TemplateRefError(TemplateError): ...    # базовый для V3/V4/V8/V10/V11
class TemplateReloadError(TemplateError): ... # оборачивает проблемы reload()
```

Все ошибки содержат `template_ref: TemplateRef` и `correlation_id` (если известен).

---

## 8. Observability

### 8.1. Метрики (Prometheus)
| Метрика | Тип | Labels | Комментарий |
|---|---|---|---|
| `pov_registry_load_total` | counter | `template_id`, `result=hit/miss/error` | счётчик загрузок |
| `pov_registry_load_latency_seconds` | histogram | `template_id` | latency load() |
| `pov_registry_validate_errors_total` | counter | `code` | ошибки валидации |
| `pov_registry_templates_total` | gauge | `domain`, `status` | число шаблонов |
| `pov_registry_reload_duration_seconds` | histogram | — | длительность reload() |
| `pov_registry_reload_failures_total` | counter | `reason` | неуспешные reload() |

### 8.2. Логи (structlog)
Пример записи:
```json
{
  "timestamp": "2026-04-15T12:34:56Z",
  "level": "info",
  "component": "template_registry",
  "action": "load",
  "template_id": "rag_index_build",
  "template_version": "1.2.0",
  "result": "hit",
  "correlation_id": "018f3b5a-..."
}
```

### 8.3. Tracing (OpenTelemetry)
- Span name: `template_registry.<method>`
- Атрибуты: `template.id`, `template.version`, `template.type`, `registry.cache_hit`.
- Ошибка → `span.set_status(ERROR)` + запись исключения.

---

## 9. Тестовая стратегия

### 9.1. Unit-тесты
- Парсинг каждого типа шаблона из YAML → `Template`.
- Валидатор: по одному тесту на каждое правило V1–V13 (позитив + негатив).
- Expression parser: golden + property-based.

### 9.2. Golden-тесты
- Для каждого шаблона в `templates/` — снимок результата `Template.model_dump()`.
- Дифф в PR-checks при любом изменении.

### 9.3. Integration-тесты
- Testcontainers Postgres: построение индекса из фикстурной директории, проверка `list()`, `reload()`, конкурентных чтений во время reload().

### 9.4. Property-based (Hypothesis)
- Генератор composite-деревьев глубины ≤ 5 → проверка ацикличности и полноты bubble-up.
- Генератор SemVer переходов → проверка monotonic `latest`.

---

## 10. Производительность и ограничения

| Метрика | Цель |
|---|---|
| `load()` (cache hit) | ≤ 1 ms p99 |
| `load()` (cache miss) | ≤ 10 ms p99 |
| `validate_all()` на 500 шаблонов | ≤ 3 s |
| `reload()` полный | ≤ 500 ms для 500 шаблонов |
| Потребление памяти | ≤ 50 MB на 500 шаблонов (включая кеш) |

### 10.1. Ограничения
- Не более **10 000** версий шаблонов в одном репозитории (иначе indexing > 10 s).
- Длина `id` ≤ 64 символов.
- Глубина `composite`-дерева ≤ 5 (проверяется V10).
- Один файл YAML ≤ 1 MiB.

---

## 11. CLI

```
pov-lab-templates list [--domain <d>] [--status <s>]
pov-lab-templates show <id>[@<version>]
pov-lab-templates validate [--all | --changed | <path>...]
pov-lab-templates check-integrity
pov-lab-templates graph <id>[@<version>] --format dot   # composite dep-graph в dot
pov-lab-templates diff <id> <old-version> <new-version>
```

CLI — тонкая обёртка над Python API. Реализация: `click` или `typer` (выбрать `typer` — типизация лучше интегрируется с pydantic).

---

## 12. Миграции и совместимость

- Схема `template.schema.json` версионируется полем `$id` и имеет `$schema: draft 2020-12`.
- При breaking-change схемы — создаётся новая версия JSON Schema (`template.schema.v2.json`), старая остаётся для старых YAML (читаются через поле `schema_version`).
- Alembic-миграции `template_index` / `template_alias` — часть `migrations/registry/`.

---

## 13. Что вне области MVP этой спеки

- Distributed reload (несколько экземпляров сервиса с координацией)
- Подписи шаблонов (cosign)
- UI для просмотра шаблонов
- A/B тестирование версий шаблонов

Эти вопросы адресуются отдельными RFC
