# Template Semantics — спецификация

> **Статус:** v1.1 · Draft · 2026-04-19
> **Зависимости:** [00_overview.md](00_overview.md), [01_template_registry.md](01_template_registry.md), [04_problem_state.md](04_problem_state.md)
> **Область:** канонический semantic contract шаблона задачи. Это главный носитель problem-solving логики платформы.

---

## 1. Назначение и зона ответственности

### 1.1. Что делает
- Определяет, **с каким классом проблем** работает шаблон, какие gaps он закрывает и какие новые может породить.
- Описывает, **при каких условиях** шаблон может быть допущен Planning Coordinator'ом.
- Фиксирует, **какой методологией** должен пользоваться исполнитель внутри задачи.
- Описывает контекст, инструменты, правила decomposition и ожидаемые изменения `ProblemState`.
- Даёт Planner'у и Context Engine декларативный контракт, не позволяющий им принимать произвольные доменные решения.
- Фиксирует место шаблона внутри `recipe`: является ли он предметным шагом, обязательным meta-pass, review или repair.

### 1.2. Чего НЕ делает
- Не заменяет [01_template_registry.md](01_template_registry.md): registry по-прежнему отвечает за хранение, версионирование и индекс.
- Не исполняет задачи и не строит prompt/runtime payload'ы.
- Не хранит текущее состояние проекта и не заменяет `ProblemState`.
- Не принимает решение о human handoff; он только задаёт escalation policy.

### 1.3. Архитектурный принцип

Платформа **template-centric**, но не “однослойная”:

- Доменная область оформляется через `Domain Pack`.
- Доменная методология живёт в шаблонах.
- Координатор планирования применяет explicit semantic rules шаблонов и recipe-политик.
- Ни один слой выше шаблонов не имеет права “додумывать” закрываемые gaps, скрытые stop criteria или tool policy.
- Нельзя полагаться на свободную оценку LLM о том, “достаточно ли данных”; readiness и обязательные meta-passes должны быть выражены декларативно.

---

## 2. Дизайн-принципы semantic contract

### 2.1. Разделение осей

В шаблоне должны быть независимые поля для разных аспектов:

- `type` — lifecycle behavior (`composite` / `executable` / `dynamic`).
- `semantics.template_role` — роль шаблона в оркестрации (`core_task`, `meta_analysis`, `review`, `repair`, `escalation`).
- `semantics.cognitive_role` — роль в problem-solving.
- `domain` — предметная область.
- `output_contract` — какие артефакты производятся.
- `problem_state_effects` — как outputs влияют на структуру проблемы.

Нормативное правило: **одно поле не должно одновременно кодировать runtime, domain и смысл задачи**.

### 2.2. Расширяемость

Расширение нового домена делается добавлением новых шаблонов и новых namespaced ids:

- `gap_type`: `common.unclear_success_criteria`, `rag.missing_ground_truth`, `frontend.undefined_user_flow`
- `artifact_role`: `common.spec`, `rag.eval_report`, `frontend.ui_mock`
- `decision_type`: `common.solution_direction`, `frontend.design_system_choice`

Core платформы понимает формат ids и базовые операции над ними; domain-specific смысл задаётся самими шаблонами.

Но новый домен не должен быть “россыпью отдельных шаблонов”.
Нормативное правило:

- шаблоны домена поставляются через `Domain Pack`;
- pack включает vocabulary, templates и recipe fragments;
- именно recipe fragments позволяют домену влиять на ранние этапы вроде подготовки ТЗ, а не только на позднюю реализацию.

Пример: `frontend`-домен должен расширять не только фазу implementation, но и фазу requirements, добавляя шаги анализа пользовательских потоков, экранов, UX-ограничений и требований к интерфейсу.

### 2.3. Explainability

Любой выбор шаблона должен быть объясним через:

- activation rules;
- открытые gaps / decisions / risks;
- доступные artifacts;
- stage gate;
- explicit priority hints.

Скрытая логика в prompt'е, не отражённая в semantic fields, запрещена.

### 2.4. Шаблон как атомарный локальный способ работы

Чтобы не смешивать уровни модели:

- `Domain Pack` — это доменная область целиком;
- `Recipe` — это обязательная схема прохождения шагов;
- `Template` — это **один типовой локальный шаг работы**.

Шаблон не должен кодировать весь workflow домена целиком.  
Шаблон отвечает на вопрос:

> Если система уже решила выполнить шаг этого типа, как именно этот шаг нужно выполнить, какие входы нужны и что будет считаться завершением?

Именно поэтому доменная методология распределяется так:

- domain-wide composition — через `Domain Pack`;
- sequencing и обязательные проходы — через `Recipe` / `RecipeFragment`;
- локальная профессиональная методика — через `Template`.

---

## 3. Каноническая структура шаблона

Существующая модель из [01_template_registry.md](01_template_registry.md) расширяется следующими top-level секциями:

```yaml
id: requirements_alignment
version: 1.0.0
type: executable
description: Уточнение потребности и критериев успеха
owner: analyst@pov-lab
status: active
domain: common
input_requirements: []
output_contract: []
escalation: {}
metadata: {}

semantics: {}
activation: {}
problem_state_effects: {}
context_policy: {}
tool_policy: {}
validation_policy: {}
```

Секции `semantics`, `activation`, `problem_state_effects`, `context_policy`, `tool_policy`, `validation_policy` обязательны для всех активных шаблонов. Для `draft` допускается их частичное заполнение.

Нормативное уточнение по схемам:

- текущий файл `schemas/template.schema.json` покрывает только registry/runtime shape из `01_template_registry.md`;
- реализация этой спеки обязана выпустить новую версию схемы (`template.schema.v2.json` или эквивалентное расширение текущей схемы), включающую поля из этого документа;
- до появления новой JSON Schema authoritative источником остаётся этот markdown-документ.

---

## 4. Общие типы и модели

### 4.1. Базовые aliases

```python
NamespacedId = Annotated[str, StringConstraints(
    pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"
)]

ProblemFieldPath = Annotated[str, StringConstraints(
    pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$"
)]
```

Использование:

- `GapType = NamespacedId`
- `ArtifactRole = NamespacedId`
- `DecisionType = NamespacedId`
- `RiskType = NamespacedId`
- `CapabilityTag = NamespacedId`

### 4.2. Core cognitive roles

```python
CoreCognitiveRole = Literal[
    "problem_framing",
    "requirements_discovery",
    "constraint_analysis",
    "solution_design",
    "architecture_design",
    "implementation",
    "validation",
    "critique",
    "integration",
    "delivery",
]
```

Расширение допускается только через namespaced id, например `frontend.component_design`. Unknown role разрешён registry, но Planning Coordinator не имеет права использовать его в scoring, если для него не включена policy support.

### 4.3. `TemplateSemantics`

```python
class TemplateSemantics(BaseModel):
    template_role: Literal["core_task", "meta_analysis", "review", "repair", "escalation"]
    cognitive_role: str
    closes_gaps: list[NamespacedId]
    may_emit_gaps: list[NamespacedId] = []
    detects_gaps: list[NamespacedId] = []
    consumes_decisions: list[NamespacedId] = []
    produces_decisions: list[NamespacedId] = []
    artifact_roles_produced: list[NamespacedId]
    capability_tags: list[NamespacedId] = []
    priority_hint: int = 0                 # -100..100
    planner_visibility: Literal["public", "internal_only"] = "public"
    supersedes_templates: list[TemplateRef] = []
    admissibility_tags: list[NamespacedId] = []
```

Смысл полей:

- `closes_gaps` — template претендует на закрытие указанных gaps при успешном completion.
- `may_emit_gaps` — template может породить новые gaps как легитимный результат.
- `detects_gaps` — template умеет обнаруживать gaps, даже если не закрывает их.
- `artifact_roles_produced` — semantic role outputs. Не заменяет `output_contract`, а дополняет его.
- `planner_visibility=internal_only` — шаблон может запускаться только системой как follow-up, но не как прямой пользовательский intent.
- `template_role` определяет, считается ли шаблон основным предметным шагом или обязательным meta-pass. Это влияет на recipe orchestration и completion policy.
- `admissibility_tags` позволяют policy layer проверять recipe-level требования без чтения prompt'ов.

### 4.3.1. Template roles

Нормативные правила по ролям:

- `core_task` — производит основной полезный артефакт для текущего класса задач; не должен запускаться до выполнения обязательных meta-passes recipe.
- `meta_analysis` — почти всегда меняет `ProblemState`, readiness или набор active gaps/decisions; может не производить пользовательский финальный артефакт.
- `review` — проверяет уже сформированное решение, постановку или артефакт на полноту, согласованность и релевантность.
- `repair` — адресно исправляет findings после `review`/validation.
- `escalation` — оформляет handoff человеку, формализует блокировку или запрос внешнего решения.

### 4.4. `ActivationPolicy`

```python
class ActivationPredicate(BaseModel):
    kind: Literal[
        "gap_open",
        "gap_absent",
        "decision_missing",
        "decision_present",
        "problem_field_missing",
        "problem_field_present",
        "artifact_role_present",
        "artifact_role_missing",
        "stage_gate_in",
        "risk_above",
        "domain_signal_present",
        "readiness_at_least",
        "readiness_below",
        "recipe_step_pending",
        "recipe_step_completed",
    ]
    key: str
    value: str | int | float | bool | None = None

class ActivationPolicy(BaseModel):
    when_all: list[ActivationPredicate] = []
    when_any: list[ActivationPredicate] = []
    unless: list[ActivationPredicate] = []
    preferred_stage_gates: list[StageGate] = []
    max_active_tasks_per_project: int = 1
    cooldown_seconds: int = 0
    dedup_scope: Literal["project", "gap", "decision", "task_family"] = "gap"
    dedup_key_template: str
    requires_recipe: str | None = None
```

Правила:

- `when_all` — все предикаты обязаны быть true.
- `when_any` — хотя бы один предикат обязан быть true; пустой список = no-op.
- `unless` — если хотя бы один predicate true, шаблон не активируется.
- `dedup_key_template` — Jinja-like строка над полями project/gap/decision/template; используется Planner'ом для idempotent selection.

### 4.4.1. Admission vs selection

`ActivationPolicy` определяет **допуск**, а не только scoring.

Шаблон считается admissible только если:

- `when_all` выполнены;
- `when_any` выполнен, если он задан;
- `unless` не сработал;
- recipe-level обязательства не запрещают переход;
- readiness constraints удовлетворены.

Только после этого шаблон может участвовать в selection/scoring.

### 4.5. `FrameworkSpec`

```python
class FrameworkStep(BaseModel):
    step_id: str
    title: str
    intent: str
    required_outputs: list[str] = []      # output_contract.name
    can_emit_gaps: list[NamespacedId] = []
    can_update_fields: list[ProblemFieldPath] = []

class StopCondition(BaseModel):
    kind: Literal["outputs_ready", "confidence_below", "user_input_missing", "framework_exhausted"]
    threshold: float | int | None = None

class FrameworkSpec(BaseModel):
    mode: Literal["prompt_contract", "questionnaire", "checklist", "review_loop"]
    steps: list[FrameworkStep]
    stop_conditions: list[StopCondition]
    confidence_required: float = 0.7
    emits_problem_state_patch: bool = True
    may_request_user_input: bool = False
```

Нормативное правило: `FrameworkSpec` описывает **локальную методологию**. Глобальную очередность шаблонов он не определяет.

### 4.5.1. FrameworkSpec не заменяет meta-passes

`FrameworkSpec` описывает локальную методологию внутри одного шаблона. Он не должен использоваться как суррогат для глобальных обязательных проходов вроде:

- уточнения цели;
- анализа user story;
- анализа альтернатив;
- consistency/relevance review;
- downstream impact review.

Если такие проходы обязательны для класса задач, они должны быть выражены отдельными шаблонами и включены в recipe.

### 4.5.2. Recipe semantics

Для неплоской оркестрации вводится отдельная сущность `TemplateRecipe`. Recipe не заменяет шаблон и не хранит предметную методологию. Он описывает:

- какой `core_task` считается центральным;
- какие `meta_analysis`/`review` проходы обязательны;
- по каким readiness dimensions нельзя двигаться дальше без прохождения;
- в каком порядке допускаются роли.

Каноническая модель:

```python
class RecipeStep(BaseModel):
    step_id: str
    template_role: Literal["core_task", "meta_analysis", "review", "repair", "escalation"]
    required: bool = True
    completion_artifact_roles: list[NamespacedId] = []
    completion_gap_closures: list[NamespacedId] = []
    completion_readiness: list[NamespacedId] = []

class TemplateRecipe(BaseModel):
    recipe_id: NamespacedId
    description: str
    entry_conditions: list[ActivationPredicate] = []
    core_template_refs: list[TemplateRef]
    mandatory_steps: list[RecipeStep]
    allows_parallel_meta_passes: bool = True
```

Нормативное правило: recipe-driven orchestration должна использоваться для классов задач, где “сразу перейти к core task” опасно из-за систематического оптимизма LLM.

До появления отдельного Recipe Registry definitions recipes считаются registry-managed YAML-объектами того же semantic layer и версионируются рядом с шаблонами.

### 4.5.3. Recipe fragments и влияние домена на ТЗ

Для сценариев, где домен должен влиять на уже существующий класс задач, вводится `TemplateRecipeFragment`.

Смысл:

- base recipe задаёт общий skeleton сценария;
- fragment встраивает доменные обязательные проходы в конкретные точки recipe;
- composed recipe становится итоговым источником obligations для planner'а.

Это особенно важно для построения ТЗ.

Пример:

- `common.build_requirements_spec` задаёт общий сценарий подготовки ТЗ;
- `frontend.requirements_extension` добавляет:
  - анализ пользовательских ролей;
  - анализ пользовательских потоков;
  - inventory экранов;
  - UX / accessibility constraints review.

Итог: frontend-знания влияют на ТЗ не через “умную догадку модели”, а через обязательные domain-specific meta-passes.

### 4.6. `ProblemStateEffects`

```python
class FieldWriteEffect(BaseModel):
    field_path: ProblemFieldPath
    source: Literal["literal", "output_field", "derived"]
    value: str | int | float | bool | None = None
    output_name: str | None = None
    mode: Literal["replace", "append", "merge", "set_if_empty"] = "replace"

class GapEffect(BaseModel):
    gap_type: NamespacedId
    action: Literal["close", "open", "reopen", "accept_risk"]
    reason_from_output: str | None = None

class DecisionEffect(BaseModel):
    decision_type: NamespacedId
    action: Literal["propose", "confirm", "supersede", "reject"]
    value_from_output: str | None = None

class ProblemStateEffects(BaseModel):
    field_writes: list[FieldWriteEffect] = []
    gap_effects: list[GapEffect] = []
    decision_effects: list[DecisionEffect] = []
```

Если шаблон закрывает gap, это должно быть выражено либо через `gap_effects`, либо через output schema, из которой Problem State Store может однозначно вывести такое закрытие.

Если доменный шаблон влияет на состав будущего ТЗ, это должно быть выражено не только через produced artifact, но и через:

- readiness effects;
- domain-specific gap closures;
- decision proposals;
- artifact roles, которые затем станут входом для core-task генерации ТЗ.

### 4.7. `ContextPolicy`

```python
class ContextInputPriority(BaseModel):
    input_name: str
    priority: int
    allow_summary: bool = True
    allow_semantic_chunks: bool = False

class ContextPolicy(BaseModel):
    required_problem_fields: list[ProblemFieldPath]
    optional_problem_fields: list[ProblemFieldPath] = []
    input_priorities: list[ContextInputPriority] = []
    summary_levels_allowed: list[Literal["raw", "structured", "short", "task_specific"]]
    max_input_tokens: int
    max_retrieval_chunks: int = 0
    overflow_strategy: Literal["summarize", "decompose", "fail", "escalate"]
    manifest_required: bool = True
    readiness_evidence_required: list[NamespacedId] = []
```

### 4.8. `ToolPolicy`

```python
class ToolPolicy(BaseModel):
    allow_tools: bool = False
    allowed_tool_ids: list[str] = []
    allowed_tool_classes: list[
        Literal["read_only", "workspace_write", "command_execution", "external_write"]
    ] = []
    max_tool_rounds: int = 0
    approval_mode: Literal["none", "on_external_write", "on_any_mutation"] = "none"
```

### 4.9. `ValidationPolicy`

```python
class ValidationCheckRef(BaseModel):
    check_id: str
    severity: Literal["info", "warning", "error"]
    blocking: bool = True

class ValidationPolicy(BaseModel):
    contract_checks: list[ValidationCheckRef] = []
    critique_template_refs: list[TemplateRef] = []
    max_correction_loops: int = 0
    allow_partial_success: bool = False
    required_readiness_before_success: list[NamespacedId] = []
```

---

## 5. Полный Pydantic view шаблона

```python
class SemanticTemplate(BaseModel):
    id: TemplateId
    version: TemplateVersion
    type: TemplateType
    description: str
    owner: str
    status: Literal["draft", "active", "deprecated"]
    domain: str
    input_requirements: list[InputRequirement]
    output_contract: list[OutputArtifactSpec]
    escalation: EscalationPolicy
    metadata: TemplateMetadata

    semantics: TemplateSemantics
    activation: ActivationPolicy
    framework: FrameworkSpec
    problem_state_effects: ProblemStateEffects
    context_policy: ContextPolicy
    tool_policy: ToolPolicy
    validation_policy: ValidationPolicy
    recipe_membership: list[NamespacedId] = []

    composite: CompositeSpec | None = None
    executable: ExecutableSpec | None = None
    dynamic: DynamicSpec | None = None
```

---

## 6. Семантика выбора и исполнения

### 6.1. Как Planner использует шаблон

Planner имеет право использовать только следующие поля:

- `status`
- `domain`
- `metadata.stage_gate`
- `semantics.*`
- `activation.*`
- `context_policy.max_input_tokens`
- `validation_policy.max_correction_loops`
- `validation_policy.required_readiness_before_success`
- `recipe_membership`

Planner **не имеет права** читать prompt text, hidden instructions или runtime-only transport поля для принятия решения о выборе шаблона.

### 6.2. Как Context Engine использует шаблон

Context Engine читает:

- `input_requirements`
- `context_policy`
- `problem_state_effects` только для traceability, но не для selection

Контекст не должен включать данные, которых нет в `input_requirements` или `required_problem_fields`/`optional_problem_fields`, кроме служебных execution instructions.

### 6.3. Как Runtime использует шаблон

Runtime читает:

- `type`
- `framework`
- `tool_policy`
- `validation_policy`
- `executable` / `dynamic` / `composite`

Runtime не имеет права самостоятельно изменять `ProblemState`; он возвращает structured outputs и `problem_state_patch`, а commit выполняет отдельный store.

---

## 7. Структурные инварианты

| Код | Инвариант |
|---|---|
| S1 | `semantics.closes_gaps` не пуст для любого `public` шаблона, кроме `review` и `escalation` ролей |
| S2 | Любой `gap_type` / `artifact_role` / `decision_type` соответствует regex `NamespacedId` |
| S3 | `activation.dedup_key_template` обязателен для `status=active` |
| S4 | `context_policy.max_input_tokens >= 512` для `executable` и `dynamic` |
| S5 | `tool_policy.max_tool_rounds = 0` если `allow_tools=False` |
| S6 | `type=composite` ⇒ `framework.mode != "review_loop"` и `tool_policy.allow_tools=False` |
| S7 | `problem_state_effects.gap_effects.action="close"` допустим только для gap'ов из `semantics.closes_gaps` |
| S8 | `validation_policy.max_correction_loops = 0` если `critique_template_refs` пуст |
| S9 | Любой `FrameworkStep.required_outputs` ссылается на существующий `output_contract.name` |
| S10 | `planner_visibility=internal_only` запрещает direct manual creation через user API |

---

## 8. Алгоритм template admission and matching

1. Planner получает `ProblemStateSnapshot`, readiness projections, recipe obligations и открытые `PlanningItem`'ы.
2. Для каждого candidate template из registry:
   - проверяет `status=active`;
   - проверяет stage compatibility;
   - вычисляет predicates из `activation`;
   - проверяет recipe membership / pending mandatory steps;
   - проверяет readiness constraints;
   - строит `dedup_key`;
   - исключает template при conflict с уже активной task family.
3. Из admissible шаблонов строится детерминированный score:
   - `priority_hint`;
   - blocking severity соответствующего gap;
   - наличие готовых hard inputs;
   - penalty за незакрытые readiness deficits у `core_task`;
   - stage preference;
   - penalty за cooldown / active duplicates.
4. Planner materializes task только если может объяснить решение полями из spec и сохранить `planning_reason`.

---

## 9. YAML-пример

```yaml
id: requirements_alignment
version: 1.0.0
type: executable
description: Уточнение бизнес-потребности и критериев успеха
owner: analyst@pov-lab
status: active
domain: common
input_requirements:
  - name: intake_request
    kind: hard
    artifact_type: business_request
    selector:
      kind: by_type
      artifact_type: business_request
    description: Исходный запрос пользователя
output_contract:
  - name: requirements_spec
    artifact_type: requirements_spec
    mime_type: application/json
    description: Нормализованные требования
  - name: requirements_patch
    artifact_type: problem_state_patch
    mime_type: application/json
    description: Patch для ProblemState
escalation:
  max_attempts: 2
  max_llm_calls: 4
  wall_clock_seconds: 900
metadata:
  stage_gate: requirements
  labels: {}
  tags: ["core", "intake"]
semantics:
  template_role: meta_analysis
  cognitive_role: requirements_discovery
  closes_gaps:
    - common.unclear_business_need
    - common.unclear_success_criteria
  may_emit_gaps:
    - common.missing_input_data
    - common.conflicting_constraints
  detects_gaps:
    - common.conflicting_constraints
  consumes_decisions: []
  produces_decisions:
    - common.scope_statement
  artifact_roles_produced:
    - common.spec
  capability_tags:
    - common.user_dialog
  priority_hint: 50
  planner_visibility: public
activation:
  when_any:
    - kind: gap_open
      key: common.unclear_business_need
    - kind: gap_open
      key: common.unclear_success_criteria
  when_all:
    - kind: recipe_step_pending
      key: common.build_requirements_spec.goal_alignment
  unless:
    - kind: stage_gate_in
      key: delivery
  preferred_stage_gates: [requirements]
  max_active_tasks_per_project: 1
  cooldown_seconds: 0
  dedup_scope: gap
  dedup_key_template: "requirements_alignment:{{ gap_type }}"
framework:
  mode: questionnaire
  steps:
    - step_id: identify_goal
      title: Определить реальную потребность
      intent: Превратить запрос в верифицируемую цель
      can_update_fields: ["goal.summary", "goal.business_need"]
    - step_id: define_success
      title: Зафиксировать критерии успеха
      intent: Вынести измеримые критерии
      required_outputs: ["requirements_spec", "requirements_patch"]
      can_update_fields: ["success_criteria"]
  stop_conditions:
    - kind: outputs_ready
  confidence_required: 0.75
  emits_problem_state_patch: true
  may_request_user_input: true
problem_state_effects:
  gap_effects:
    - gap_type: common.unclear_business_need
      action: close
    - gap_type: common.unclear_success_criteria
      action: close
  decision_effects:
    - decision_type: common.scope_statement
      action: propose
      value_from_output: requirements_spec
context_policy:
  required_problem_fields: ["goal.summary", "constraints", "active_gaps"]
  optional_problem_fields: ["known_facts"]
  input_priorities:
    - input_name: intake_request
      priority: 100
      allow_summary: false
  summary_levels_allowed: ["raw", "structured", "short"]
  max_input_tokens: 8000
  max_retrieval_chunks: 0
  overflow_strategy: summarize
  manifest_required: true
  readiness_evidence_required:
    - common.goal_clarity
tool_policy:
  allow_tools: false
  allowed_tool_ids: []
  allowed_tool_classes: []
  max_tool_rounds: 0
  approval_mode: none
validation_policy:
  contract_checks:
    - check_id: output_contract
      severity: error
      blocking: true
  critique_template_refs: []
  max_correction_loops: 0
  allow_partial_success: false
  required_readiness_before_success:
    - common.goal_clarity
recipe_membership:
  - common.build_requirements_spec
executable:
  executor: llm
  llm:
    model: anthropic:claude-sonnet-4-6
    prompt_ref: pov_lab:prompts.requirements_alignment@1.0.0
    tool_allowlist: []
    token_budget:
      input_tokens_max: 8000
      output_tokens_max: 2000
      total_tokens_max: 10000
    temperature: 0.0
    json_mode: true
```

---

## 10. Что вне области этой спеки

- Хранение YAML, алиасов и индекса версий — `01_template_registry.md`.
- Lifecycle конкретных задач — `02_task_store.md`.
- Структура `ProblemState` и patch-apply semantics — `04_problem_state.md`.
- Алгоритм admission/scoring planner runs и recipe orchestration — `05_planning_coordinator.md`.
- Runtime adapter contracts — `07_execution_runtime.md`.
