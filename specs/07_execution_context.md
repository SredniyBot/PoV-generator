# Контекст и исполнение

> **Статус:** v2.1 · черновик · 2026-05-09

Документ объединяет артефакты, манифесты контекста и исполнение. Исполнитель всегда работает только с явно собранным `ContextManifest`. Поверх каждой leaf-задачи накладывается wrapper активного `methodology_pack`, который формирует структуру рассуждения вокруг основной инструкции.

---

## Артефакт

```python
class ArtifactRecord(BaseModel):
    artifact_id: UUID
    project_id: UUID
    contract_ref: str
    title: str
    kind: Literal["primary", "reasoning", "trace", "derived"]
    format: Literal["json", "markdown", "text", "binary"]
    created_by_task_id: UUID
    parent_artifact_id: UUID | None
    storage_uri: str
    metadata: dict
```

Виды артефактов:

- `primary` — основной результат задачи (по `produces.artifact` шаблона);
- `reasoning` — `reasoning_artifact`, схема которого собирается из активного `methodology_pack`;
- `trace` — `methodology_trace` (последовательность стадий, сработавшие правила, ссылки на эмиттированные `ClarificationCandidate`);
- `derived` — производные представления (markdown-render, summary).

Правила:

- артефакт неизменяем;
- исправление создает новый артефакт;
- основной артефакт валидируется по `artifact_contract`;
- markdown/render/summary являются производными представлениями или производными артефактами;
- артефакт связан с задачей-производителем.

---

## Манифест контекста

```python
class ContextManifest(BaseModel):
    manifest_id: UUID
    project_id: UUID
    task_id: UUID
    task_template_ref: str
    problem_state_version: int
    items: list[ContextItem]
    budget: ContextBudget
```

Элемент контекста:

```python
class ContextItem(BaseModel):
    kind: Literal["state", "artifact", "summary", "task", "instruction"]
    source_ref: str
    title: str
    content: str
    required: bool
    token_estimate: int
```

---

## Сборка контекста

Алгоритм:

1. Загрузить task node.
2. Загрузить шаблон задачи.
3. Проверить `requires`.
4. Найти обязательные артефакты.
5. Добавить разрешенный необязательный контекст.
6. Добавить разрешенные поля состояния.
7. Применить политику переполнения контекста.
8. Сохранить манифест.

Стратегии переполнения:

- `fail`;
- `trim_optional`;
- `summarize`;
- `decompose`.

`decompose` возвращает планировщику необходимость создать отдельную подготовительную задачу.

---

## Methodology wrapper

Wrapper активного `methodology_pack` оборачивает исполнение каждой leaf-задачи. Шаблон задачи не знает про методологию — wrapper подставляется автоматически.

Алгоритм:

1. Прочитать активный `methodology_pack` из `ProblemState.active_methodology_packs`.
2. С учётом `complexity` шаблона задачи отобрать обязательные стадии (`required_stages` + `optional_stages`, минус `skip_stages` из `complexity_overrides`).
3. Сформировать комбинированный prompt:
   - системная часть от методологии: описание стадий и формата `reasoning_artifact`,
   - пользовательская часть от задачи: `instruction` + элементы `ContextManifest`.
4. В режиме `stage_execution_mode: single_call` — один LLM-вызов со structured output по объединённой схеме `reasoning_artifact + main_artifact`. В режиме `per_stage_cot` (зарезервировано после MVP) — отдельный вызов на стадию с растущим контекстом.
5. Прогнать правила стадий по выходу. Сработавшие правила эмиттят `ClarificationCandidate` с `source_type: methodology`.
6. Сформировать `methodology_trace` (последовательность пройденных стадий, проверенные/сработавшие правила, refs на кандидатов и LLM run).

Wrapper не появляется в графе задач как отдельный узел.

---

## Запрос исполнения

```python
class ExecutionRequest(BaseModel):
    execution_run_id: UUID
    project_id: UUID
    task_id: UUID
    task_template_ref: str
    context_manifest_id: UUID
    executor: Literal["llm", "script", "tool", "human", "hybrid"]
    model: str | None
    complexity: Literal["trivial", "standard", "complex"]
    methodology_pack_ref: str | None      # активный пакет на момент запуска
```

Среда исполнения запускает только листовые задачи. `model` выбирается из политики проекта по `complexity` (с возможным override от admission control).

---

## Результат исполнения

```python
class ExecutionResult(BaseModel):
    execution_run_id: UUID
    status: Literal["succeeded", "failed"]
    primary_artifact_id: UUID | None        # по produces.artifact шаблона
    reasoning_artifact_id: UUID | None      # по схеме активной методологии
    methodology_trace_id: UUID | None       # трасса стадий wrapper'a
    proposed_problem_patches: list[dict]
    clarification_candidates: list[dict]
    trace_ids: list[UUID]                   # технические трассы LLM/tool runs
    failure_code: str | None
    failure_message: str | None
```

Успешное исполнение leaf-задачи производит три артефакта: `primary`, `reasoning_artifact`, `methodology_trace`. Все три валидируются: `primary` — по `artifact_contract` из `produces.artifact`, `reasoning_artifact` — по схеме активной методологии, `methodology_trace` — по фиксированной системной схеме.

`proposed_problem_patches` не применяются средой исполнения напрямую. Их применяет слой управления процессом после валидации.

`clarification_candidates` не показываются пользователю напрямую. Их обрабатывает координатор уточнений. Кандидаты могут приходить из задачи (`source_type: task`) или из правил методологии (`source_type: methodology`).

---

## Исполнение через LLM

LLM получает:

- системную инструкцию;
- markdown-инструкцию задачи;
- контракт и схему артефакта;
- элементы манифеста контекста;
- политику языка ответа.

LLM не получает:

- произвольный доступ к проекту;
- право создавать задачи;
- право активировать доменные пакеты;
- право менять `ProblemState` напрямую.
- право задавать вопрос пользователю напрямую.

---

## Исполнение человеком

Обычный вопрос пользователю оформляется как `ClarificationRequest`, а не как листовая задача.

Задача для человека допустима только тогда, когда требуется осмысленная работа человека, а не простой ответ на уточнение.

Задача для человека должна содержать:

- конкретный вопрос;
- причину блокировки;
- затронутые задачи и проверки качества;
- ожидаемый формат ответа.

---

## Коды ошибок

```text
llm_schema_invalid
llm_context_too_large
llm_provider_error
script_failed
tool_failed
human_timeout
contract_missing
unsupported_executor
```

---

## Инварианты

| ID | Правило |
|---|---|
| EC1 | Исполнение без `ContextManifest` запрещено. |
| EC2 | Среда исполнения запускает только листовые задачи. |
| EC3 | Среда исполнения не меняет граф задач и `ProblemState` напрямую. |
| EC4 | Артефакт неизменяем. |
| EC5 | Исполнитель не получает контекст вне манифеста. |
| EC6 | Выход LLM должен соответствовать контракту артефакта. |
| EC7 | Кандидаты уточнений проходят через координатор уточнений. |
| EC8 | Успешное исполнение leaf-задачи производит `primary`, `reasoning_artifact` и `methodology_trace`. |
| EC9 | `reasoning_artifact` валидируется по схеме активного `methodology_pack`. |
| EC10 | Wrapper методологии не создаёт узлов в графе задач. |
