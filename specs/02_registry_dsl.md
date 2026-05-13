# DSL реестра

> **Статус:** v2.1 · черновик · 2026-05-09

Реестр хранит декларативный DSL системы. YAML должен быть понятным для человека, коротким, валидируемым и расширяемым.

---

## Объекты

Реестр поддерживает семь `kind`:

```text
objective
task_template
artifact_contract
domain_pack
methodology_pack
quality_gate
vocabulary
```

Реестр не хранит линейные сценарии выполнения. Порядок работы возникает в графе задач во время выполнения. Методология определяет форму рассуждения внутри каждой задачи, но не порядок задач.

---

## Структура файлов

```text
templates/
  objectives/
  tasks/
    common/
      identify_data_sources/      # task pack: одна директория = одна задача
        task.yaml                 # task_template
        artifact.yaml             # artifact_contract (или inline в task.yaml)
        prompt.md                 # инструкция исполнителя
        examples/                 # фикстуры для тестов
    ml/
    security/
    integration/
    frontend/
  artifacts/                      # артефакты, переиспользуемые между задачами
  domains/
  methodologies/
  gates/
  vocabularies/
```

Правила:

- одна задача — одна директория со всеми её файлами (task pack);
- methodology pack — один файл в `methodologies/<id>.yaml` (или директория, если нужны отдельные prompts);
- путь должен соответствовать `kind` и namespace;
- опубликованные ссылки всегда указывают конкретную версию;
- большие LLM-инструкции хранятся в `prompts/**/*.md` (общая директория или `prompts/` рядом с task pack);
- YAML описывает контракт, а не длинный prompt.

---

## Общий заголовок

```yaml
kind: task_template
id: common.identify_data_sources
version: 1.0.0
title: Выявить источники данных
status: active
```

`status`: `draft`, `active`, `deprecated`.

---

## Цель

Цель задает верхний уровень работы и критерии завершения.

```yaml
kind: objective
id: common.requirements_specification
version: 1.0.0
title: Подготовить техническое задание из бизнес-запроса

root: common.prepare_requirements_spec@1.0.0

done_when:
  artifacts:
    - common.requirements_spec@1.0.0
    - common.review_report@1.0.0
  gates:
    - common.requirements_spec_review_passed@1.0.0
```

Цель не содержит `steps`, `order`, `before`, `after`.

---

## Шаблон задачи

Шаблон задачи бывает `composite` или `leaf`. Шаблон описывает специфику задачи и не знает про методологию рассуждения — её накладывает wrapper во время выполнения.

Composite:

```yaml
kind: task_template
id: common.analyze_data
version: 1.0.0
title: Разобрать данные
type: composite
status: active

children:
  - id: sources
    task: common.identify_data_sources@1.0.0
  - id: quality
    task: common.assess_data_quality@1.0.0

slots:
  - id: data.sources
    title: Источники данных
  - id: data.quality
    title: Качество данных
  - id: data.privacy
    title: Персональные и чувствительные данные

completion:
  when:
    children: all_required_completed
    gates: all_required_passed
```

Leaf:

```yaml
kind: task_template
id: common.identify_data_sources
version: 1.0.0
title: Выявить источники данных
type: leaf
status: active

complexity: standard          # trivial | standard | complex — для выбора модели
executor: llm

requires:
  state:
    - business_request
  artifacts:
    optional:
      - common.request_facts@1.0.0
    # Этап 7.3: декларативный auto-collect. Когда задаче нужно «всё, что
    # вышло из активных доменных паков», вместо hand-coded списка
    # optional-ролей используется флаг ниже. Финальная merge-задача
    # (`requirements_spec_generation`) использует именно этот путь.
    collect_optional:
      from_active_domain_packs: true

produces:
  artifact: common.data_sources@1.0.0   # только основной артефакт

context:
  include:
    - state.business_request
    - artifact.common.request_facts
  overflow: trim_optional

instruction: prompts/common/identify_data_sources.md   # только специфика задачи

validation:
  schema: strict
  min_confidence: 0.45

clarification_policy:                 # опционально, только специфика задачи
  may_generate_candidates: true
  ask_when:
    - missing_critical_input
  default_blocking_scope: task

# Этап 5: опционально для leaf-задач, объединяющих N артефактов.
# Подробно — в 04_task_template_semantics.md § «Merge как класс leaf-задач».
merge:
  strategy: structural   # structural | synthetic | hybrid
  conflict_policy: union # union | first_wins | last_wins | fail_on_conflict
```

**Что не объявляется в шаблоне:**

- структура рассуждения (цель / JTBD / варианты / решение) — задаётся активным `methodology_pack`;
- форма `reasoning_artifact` и `methodology_trace` — генерируются wrapper'ом на runtime;
- общая методологическая часть `clarification_policy` — переезжает в `methodology_pack`. В шаблоне остаются только правила, специфичные данной задаче.

`reasoning_artifact` создаётся автоматически на каждое исполнение leaf-задачи. Его схема собирается из `produces` всех активных стадий методологии.

---

## Контракт артефакта

Контракт артефакта описывает структуру результата.

> **Этап 7.5 (честные контракты).** Если YAML-схема контракта пустая
> или сводится к `additionalProperties: true` без обязательных полей,
> контракт обязан явно указать `unstructured: true`. Псевдо-контракт
> хуже отсутствующего: он создаёт ложное ощущение защищённости и
> молча пропускает невалидные артефакты. Декларация `unstructured: true`
> делает истину явной: «схема здесь декларативна, реальную форму
> результата валидирует исполнительный слой».

```yaml
kind: artifact_contract
id: common.data_sources
version: 1.0.0
title: Источники данных

# unstructured: true   # ← поднять, если схема пустая / additionalProperties: true.

schema:
  type: object
  required: [sources, confidence]
  properties:
    sources:
      type: array
      items:
        type: object
    confidence:
      type: number
```

---

## Доменный пакет

Доменный пакет добавляет доменные задачи и проверки в слоты.

```yaml
kind: domain_pack
id: ml.predictive_analytics
version: 1.0.0
title: Предиктивная аналитика и ML

detect:
  signals:
    - прогноз
    - машинное обучение
    - скоринг

contributes:
  - to: data.entities
    add:
      - id: prediction_target
        task: ml.define_prediction_target@1.0.0

  - to: review.domain_gates
    add:
      - id: ml_requirements_review
        gate: ml.requirements_complete@1.0.0

clarification_policy:
  topics:
    - id: ml.prediction_target
      title: Целевое событие прогноза
      ask_when:
        - missing_owner_decision
        - conflicting_business_goal
```

Доменное расширение должно быть идемпотентным: повторное применение не создает дубль.

`domain_pack` отвечает за **«над чем думаем»** и не зависит от `methodology_pack`. Эти две оси расширения накладываются независимо: методология задаёт структуру рассуждения для всех задач, домен — состав задач и проверок для конкретной предметной области.

---

## Методологический пакет

Методологический пакет описывает **«как мы думаем»** — обязательные стадии рассуждения, правила, политики эскалации развилок. Один пакет применяется ко всем leaf-задачам проекта; смена пакета не требует правки шаблонов задач.

```yaml
kind: methodology_pack
id: process.lean_jtbd
version: 1.0.0
title: JTBD-driven decision making
status: active

# Режим исполнения wrapper'a.
# MVP: только single_call. Зарезервировано: per_stage_cot.
stage_execution_mode: single_call

# Стадии — линейная последовательность.
# Каждая объявляет поля, попадающие в reasoning_artifact.
stages:
  - id: goal_framing
    title: Сформулировать цель задачи
    produces:
      - field: declared_goal
        type: string
        required: true
    rules:
      - id: empty_goal
        if: "declared_goal == null"
        emit_candidate:
          severity: high
          need: "Цель задачи не сформулирована"
          blocking_scope: task

  - id: jtbd_anchor
    title: Зафиксировать JTBD
    produces:
      - field: jtbd_focus
        type: object
        schema:
          when: string
          want: string
          so_that: string

  - id: option_generation
    title: Сгенерировать варианты решения
    produces:
      - field: options
        type: array
        item_schema:
          label: string
          rationale: string
          tradeoffs: string
          confidence: number
    constraints:
      min: 2
      max: 5

  - id: decision
    title: Выбрать вариант или эскалировать
    produces:
      - field: chosen_option_id
        type: string
        nullable: true
      - field: escalation_candidate_id
        type: string
        nullable: true
    rules:
      - id: ambiguous_choice
        if: "max(options.confidence) - second(options.confidence) < 0.15"
        emit_candidate:
          severity: high
          need: "Варианты сопоставимы по уверенности — нужно решение"
          options_from: stage.option_generation.options
          blocking_scope: task

# Контракт reasoning_artifact, формируемый из стадий.
# Регистрируется автоматически реестровым loader'ом.
reasoning_artifact:
  required_stages: [goal_framing, decision]
  optional_stages: [jtbd_anchor, option_generation]

# Режимы для разных уровней сложности задач.
# Для trivial задач часть стадий может пропускаться.
complexity_overrides:
  trivial:
    skip_stages: [option_generation]
    relax_rules: [ambiguous_choice]

# Общая политика эскалаций развилок — для всех задач проекта.
clarification_policy:
  default_blocking_scope: task
  ask_when:
    - owner_decision_required
    - low_confidence_high_impact

provenance:
  emit_source_refs: true   # каждое поле reasoning_artifact получает _source
```

Правила:

- активный пакет один на проект (MVP); множественные методологии — расширение позже;
- стадии — линейная последовательность; DAG зарезервирован на будущее;
- `produces` всех активных стадий вместе формируют схему `reasoning_artifact`;
- правила стадий могут только эмиттить `ClarificationCandidate` (`source_type: methodology`); собственного механизма прерывания методология не имеет;
- методология не знает про конкретные шаблоны задач и не зависит от `domain_pack`.

---

## Политика уточнений

`clarification_policy` описывает, когда объект реестра может порождать кандидаты уточнений и какие вопросы считаются обязательными.

Политика может объявляться в:

- `task_template` — специфика конкретной задачи;
- `methodology_pack` — общие правила «как реагировать на развилки» для всех задач проекта;
- `domain_pack` — доменные темы, требующие обязательного вопроса;
- `quality_gate` — что считать причиной непрохождения.

Политика не должна содержать длинный prompt. Она задает короткие правила, темы и пороги. Конкретные формулировки вопросов создаются задачами или валидаторами и проходят через координатор уточнений.

Типовые поля:

```yaml
clarification_policy:
  may_generate_candidates: true
  ask_when:
    - owner_decision_required
    - low_confidence_high_impact
    - security_or_legal_risk
  assume_when:
    - high_confidence_low_impact
  default_blocking_scope: task
```

Координатор уточнений сводит политики со всех уровней (project mode → methodology → task → domain → gate) и принимает решение.

---

## Проверка качества

Quality gate — точка явного согласования с заказчиком или ответственной ролью между фазами проекта. Это не методологическая проверка задачи (это делают валидация артефакта и правила методологии).

```yaml
kind: quality_gate
id: client.requirements_signoff
version: 1.0.0
title: Согласование ТЗ с заказчиком
status: active

trigger:
  on_artifact_ready: common.requirements_spec@1.0.0

check:
  type: human_approval         # human_approval | external_signoff | automated_review

  # Поля для human_approval / external_signoff:
  approver_role: client        # client | methodologist | architect | dpo | ...
  decision_modes:
    - approved
    - approved_with_comments
    - rejected
  blocking: true
  timeout_hours: 120

  # Поля для automated_review:
  # validator_ref: common.requirements_review@1.0.0
  # instruction: prompts/common/review_requirements_spec.md

on_pass: complete_objective
on_fail: create_repair_task
on_comments: rerun_via_clarifications
```

**Принцип:** gate — это точка остановки. Решение по умолчанию принимает человек (`human_approval`). `automated_review` остаётся как технический подвид (например, schema compliance), но не главный путь.

Тип `external_signoff` отличается от `human_approval` тем, что предполагает внешнюю систему-источник решения (DocuSign, тикет в Jira, подпись DPO в отдельной системе) — Gateway фиксирует событие, а не собирает ответ через UI.

---

## Проверка реестра

Проверка реестра контролирует:

- все refs существуют;
- refs указывают конкретную версию;
- корневая задача цели ссылается на композитную задачу;
- листовая задача имеет исполнителя, входы, контекст и выход;
- доменное расширение ссылается на существующий слот;
- схемы артефактов валидны;
- проверки качества ссылаются на существующие артефакты и инструкции;
- `methodology_pack.stages[].produces[].field` уникальны в пределах пакета;
- `methodology_pack` и `domain_pack` не пересекаются по полям, попадающим в `reasoning_artifact` (конфликт = ошибка валидации реестра);
- `quality_gate.check.type` ∈ {`human_approval`, `external_signoff`, `automated_review`};
- если `check.type == human_approval` или `external_signoff` — обязательны `approver_role` и `decision_modes`;
- task pack: одна директория `tasks/<area>/<task_name>/` содержит ровно один `task_template` и не более одного `artifact_contract`;
- в опубликованном реестре нет устаревших объектов линейного сценария.

---

## Инварианты

| ID | Правило |
|---|---|
| R1 | YAML описывает контракт, а не состояние времени выполнения. |
| R2 | `objective` не задает порядок выполнения. |
| R3 | `task_template` не должен быть крупнее одного понятного класса работы. |
| R4 | `domain_pack` расширяет только слоты. |
| R5 | Опубликованные ссылки не используют `latest`. |
| R6 | Большие инструкции живут в markdown prompts. |
| R7 | Политика уточнений задает правила, но не создает вопрос пользователю напрямую. |
| R8 | `task_template` не объявляет структуру рассуждения — она задаётся активным `methodology_pack`. |
| R9 | `methodology_pack` не зависит от конкретных шаблонов задач. |
| R10 | `methodology_pack` и `domain_pack` — независимые оси расширения; конфликт по полю `reasoning_artifact` запрещён. |
| R11 | `quality_gate` — точка внешнего согласования или формальной проверки, не источник методологии. |
| R12 | `artifact_contract` с пустой/permissive схемой обязан декларировать `unstructured: true` (Этап 7.5). |
| R13 | Финальная merge-задача не перечисляет руками optional доменных артефактов: вместо этого использует `collect_optional.from_active_domain_packs: true` (Этап 7.3). |
