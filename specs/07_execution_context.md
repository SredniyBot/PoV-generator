# Контекст и исполнение

> **Статус:** v3.0 · черновик · 2026-05-13. Артефакты — first-class
> объекты с явным графом связей; reasoning и methodology trace —
> метаинформация, а не отдельные артефакты (Этап 1 roadmap).

Документ объединяет артефакты, манифесты контекста и исполнение. Исполнитель всегда работает только с явно собранным `ContextManifest`. Поверх каждой leaf-задачи накладывается wrapper активного `methodology_pack`, который формирует структуру рассуждения вокруг основной инструкции.

---

## Артефакт

```python
class ArtifactRecord(BaseModel):
    artifact_id: UUID
    project_id: UUID
    artifact_role: str
    title: str
    description: str | None
    artifact_format: Literal["json", "markdown", "text"]
    artifact_kind: Literal["primary", "synthesized", "derived"]
    created_by_task_id: UUID | None
    storage_path: str
    created_at: datetime
    relations: ArtifactRelations
    metadata: ArtifactMetadata
    is_superseded: bool

class ArtifactRelations(BaseModel):
    """Граф связей артефакта."""
    parent_artifact_id: UUID | None        # предыдущая версия
    input_artifact_ids: tuple[UUID, ...]   # lineage по контексту
    child_artifact_ids: tuple[UUID, ...]   # для synthesized — компоненты-источники

class ArtifactMetadata(BaseModel):
    """Метаинформация артефакта.

    Содержит reasoning и methodology trace, которые в v2.* были
    отдельными артефактами, а теперь свёрнуты в метаинформацию
    primary артефакта.
    """
    template_ref: str | None
    provider: str | None
    model: str | None
    complexity: Literal["trivial", "standard", "complex"] | None
    methodology_pack_ref: str | None
    execution_run_id: UUID | None
    reasoning: dict                        # бывший reasoning_artifact
    methodology_trace: dict                # бывший methodology_trace
    overall_confidence: float | None
    field_confidence: dict[str, float]
    used_position_ids: tuple[str, ...]     # положения Layer A (Этап 1.4)
    extras: dict
```

Виды артефактов:

- `primary` — основной результат leaf-задачи (по `produces.artifact` шаблона);
- `synthesized` — синтезированный артефакт композитной задачи (объединение результатов детей; механика слияния — Этап 5 roadmap);
- `derived` — производные представления (markdown-render, summary и т.п.).

Правила:

- артефакт неизменяем;
- исправление создаёт новый артефакт со ссылкой через `relations.parent_artifact_id`;
- старая версия помечается `is_superseded = True`;
- основной артефакт валидируется по `artifact_contract`;
- markdown/render/summary являются производными представлениями или производными артефактами;
- артефакт связан с задачей-производителем (`created_by_task_id`).

### Граф артефактов

Артефакты образуют направленный граф, где `relations.input_artifact_ids`
определяет «вход → выход» (lineage). Обратные ссылки (downstream) не
хранятся явно — вычисляются обходом индекса при необходимости
(`runtime.downstream_artifacts(artifact_id)`).

Граф используется для:

- определения, какие артефакты затрагивает оспаривание положения слоя A
  (через `used_position_ids` + downstream обход);
- провенанса при объяснении «откуда этот вывод» (upstream обход);
- инвалидации артефактов при retry или версионировании.

---

## Манифест контекста

```python
class ContextManifest(BaseModel):
    manifest_id: UUID
    project_id: UUID
    task_id: UUID
    template_ref: str
    problem_state_version: int            # версия знания на момент сборки
    items: tuple[ContextItem, ...]
    budget: ContextBudget
    excluded_items: tuple[str, ...]
    input_fingerprint: str
    created_at: datetime
    used_position_ids: tuple[str, ...]    # положения Layer A, попавшие в контекст (Этап 1.4)
```

`used_position_ids` — это идентификаторы положений слоя A, реально попавшие
в собранный контекст задачи. При создании primary артефакта это значение
переносится в `ArtifactMetadata.used_position_ids` (см. PS11), что замыкает
граф «положение → артефакт» и позволяет вычислять `artifacts_using_position`
для оспаривания.

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

1. Прочитать активный `methodology_pack` из `ProcessState.active_methodology_packs`.
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
    outputs: tuple[ExecutionOutput, ...]    # один primary артефакт на leaf-задачу
    methodology_candidates: tuple[ClarificationCandidate, ...]
    trace_ids: tuple[UUID, ...]             # технические трассы LLM/tool runs
    proposed_goal: str | None
    failure_code: str | None
    failure_message: str | None
```

Успешное исполнение leaf-задачи производит **один primary артефакт**.
Reasoning и methodology trace — это поля :attr:`ArtifactMetadata.reasoning`
и :attr:`ArtifactMetadata.methodology_trace` этого артефакта, не отдельные
``ArtifactRecord`` (Этап 1.1 roadmap).

Валидация:

- `primary.content` — по `artifact_contract` из `produces.artifact`;
- `primary.metadata.reasoning` — по схеме активного `methodology_pack`;
- `primary.metadata.methodology_trace` — по фиксированной системной схеме.

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
- право менять `ProjectKnowledge` / `ProcessState` напрямую.
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
| EC3 | Среда исполнения не меняет граф задач и `ProjectKnowledge` / `ProcessState` напрямую. |
| EC4 | Артефакт неизменяем. |
| EC5 | Исполнитель не получает контекст вне манифеста. |
| EC6 | Выход LLM должен соответствовать контракту артефакта. |
| EC7 | Один primary артефакт на исполнение leaf-задачи; reasoning и methodology trace — поля его метаинформации. |
| EC8 | `relations.input_artifact_ids` отражает реальные артефакты-входы из `ContextManifest`. |
| EC9 | `metadata.used_position_ids` совпадает с `ContextManifest.used_position_ids` на момент сборки. |
| EC10 | Производные представления (markdown, summary) хранятся отдельно от primary, со ссылкой через `relations.parent_artifact_id` или `relations.input_artifact_ids`. |
| EC11 | Кандидаты уточнений проходят через координатор уточнений. |
| EC12 | `metadata.reasoning` валидируется по схеме активного `methodology_pack` (бывший инвариант про `reasoning_artifact`). |
| EC13 | Wrapper методологии не создаёт узлов в графе задач. |
