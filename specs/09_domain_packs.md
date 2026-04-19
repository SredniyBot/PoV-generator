# Domain Packs — спецификация

> **Статус:** v0.1 · Draft · 2026-04-19
> **Зависимости:** [00_overview.md](00_overview.md), [01_template_registry.md](01_template_registry.md), [03_template_semantics.md](03_template_semantics.md), [04_problem_state.md](04_problem_state.md), [05_planning_coordinator.md](05_planning_coordinator.md)
> **Область:** модель доменного пакета знаний, composition rules и влияние домена на общие сценарии вроде построения ТЗ.

---

## 1. Назначение и зона ответственности

### 1.1. Что делает

- Формализует доменную область как подключаемую единицу знаний.
- Группирует vocabulary, templates, recipe fragments и domain-specific readiness/validation expectations.
- Позволяет новому домену влиять на уже существующие общие сценарии без переписывания ядра.
- Даёт Planner'у и Recipe Composer'у декларативный источник для включения доменных обязательных шагов.

### 1.2. Чего НЕ делает

- Не заменяет отдельные templates, recipes или ProblemState.
- Не содержит runtime-логику исполнения.
- Не определяет scoring planner'а напрямую.
- Не является одним гигантским “супер-шаблоном”.

### 1.3. Главный принцип

Новый домен должен подключаться **как пакет знаний**, а не как набор hard-coded `if/else` в planner'е и не как неструктурированная россыпь шаблонов.

---

## 2. Что входит в Domain Pack

Каждый `Domain Pack` обязан содержать или ссылаться на:

1. **Vocabulary**
   - доменные gap types
   - readiness dimensions
   - artifact roles
   - decision/risk types

2. **Templates**
   - доменные `meta_analysis`
   - при необходимости `core_task`
   - `review` / `repair` / `escalation`

3. **Recipe Fragments**
   - расширения base recipes
   - insertion points
   - новые обязательные шаги

4. **Readiness expectations**
   - какие доменные readiness dimensions должны появиться
   - какие из них blocking

5. **Validation expectations**
   - какие доменные разделы или артефакты обязаны присутствовать

---

## 3. Почему Domain Pack нужен отдельно

Без `Domain Pack` система быстро деградирует в одну из двух плохих моделей:

1. Planner начинает содержать hard-coded доменную логику.
2. Доменные знания размазываются по отдельным шаблонам без общей композиции.

Обе модели плохие:

- первую трудно расширять и тестировать;
- вторую трудно объяснять и поддерживать.

`Domain Pack` вводится именно для того, чтобы доменное расширение было:

- явным;
- подключаемым;
- проверяемым;
- совместимым с базовыми сценариями.

---

## 4. Каноническая модель

```python
class DomainPack(BaseModel):
    pack_id: NamespacedId
    version: str
    status: Literal["draft", "active", "deprecated"]
    domain: str
    description: str

    vocabulary_refs: list[str]
    template_refs: list[TemplateRef]
    recipe_fragment_refs: list[str]

    readiness_types: list[NamespacedId] = []
    artifact_roles: list[NamespacedId] = []
    decision_types: list[NamespacedId] = []

    entry_signals: list[NamespacedId] = []
    entry_artifact_roles: list[NamespacedId] = []
    entry_decision_types: list[NamespacedId] = []

    affects_stage_gates: list[StageGate] = []
    provenance: Provenance
```

Смысл:

- `entry_signals` — по каким сигналам домен обычно активируется;
- `entry_artifact_roles` — какие уже существующие артефакты могут подтолкнуть активацию домена;
- `entry_decision_types` — какие решения в ProblemState могут включать pack;
- `recipe_fragment_refs` — как pack меняет базовые сценарии.

---

## 5. Recipe Fragment и композиция

Новый домен должен влиять на систему не только через свои templates, но и через `recipe fragments`.

Именно fragment отвечает на вопрос:

> В какой момент базового сценария должны появиться доменные обязательные шаги?

### 5.1. Пример логики

Есть base recipe:

- уточнить цель;
- собрать user story;
- рассмотреть альтернативы;
- подготовить черновик ТЗ;
- провести ревью.

Есть `frontend` domain pack.

Тогда его fragment может вставить перед генерацией ТЗ:

- анализ пользовательских ролей и потоков;
- инвентаризацию экранов;
- анализ UX / accessibility constraints.

В результате composed recipe становится богаче, а planner не нужно “знать про frontend” в коде.

---

## 6. Влияние Domain Pack на ТЗ

Нормативное правило:

Если домен влияет на будущий состав системы, он должен иметь возможность влиять и на более ранний этап подготовки ТЗ.

Это значит:

- domain pack не должен ограничиваться поздней фазой implementation;
- через `recipe fragments` он должен расширять требования, альтернативы, ограничения и review-проходы там, где это необходимо.

Пример для `frontend`:

При подключении pack итоговое ТЗ должно получить дополнительные доменные разделы:

- роли пользователей;
- пользовательские потоки;
- экранный состав;
- UX/UI ограничения;
- accessibility / responsive требования.

Это должно происходить не по догадке модели, а потому что composed recipe потребовал соответствующие проходы и артефакты.

---

## 7. Активация Domain Pack

Pack не обязательно должен подключаться только руками.

Допустимы три механизма:

1. **Явный выбор пользователя или разработчика**
2. **Автоматический bootstrap по domain signals**
3. **Подтверждение planner'ом через meta-analysis outputs**

Но нормативное правило такое:

- финальная активация pack должна становиться explicit фактом в `ProblemState`;
- после активации должен появляться `recipe_composition_set`.

То есть “домен подключился” — это не скрытое внутреннее состояние, а traceable решение.

---

## 8. Интеграция с ProblemState

После активации домена ProblemState обязан отражать:

- `enabled_domain_packs`
- `recipe_composition`
- новые readiness dimensions домена
- новые gaps или decisions домена, если они уже выявлены

Это нужно для explainability и для стабильной работы planner'а.

---

## 9. Интеграция с Planner

Planner использует Domain Pack только опосредованно:

- читает `enabled_domain_packs`;
- вызывает Recipe Composer;
- получает composed recipe;
- дальше работает уже с obligations composed recipe.

Planner не должен:

- вручную вставлять frontend/rag/ml steps;
- знать порядок доменных steps в коде;
- содержать отдельные ветки логики под каждый домен.

---

## 10. Шаблон vs Domain Pack

Очень важное различие:

- `Template` — один локальный тип шага;
- `Domain Pack` — доменная область целиком;
- `Recipe` / `Recipe Fragment` — способ связать шаги в обязательный сценарий.

Если коротко:

- template отвечает на вопрос “как делать этот шаг?”;
- domain pack отвечает на вопрос “какие шаги, vocabulary и obligations вообще существуют в этой доменной области?”.

---

## 11. Пример: frontend pack

Минимальный состав:

### 11.1. Vocabulary

- `frontend.ui_flow_missing`
- `frontend.screen_inventory_missing`
- `frontend.accessibility_requirements_missing`
- `frontend.user_flows_covered`
- `frontend.screen_inventory_ready`

### 11.2. Templates

- `frontend.user_flow_analysis`
- `frontend.screen_inventory_analysis`
- `frontend.ui_constraints_review`
- `frontend.spec_section_review`

### 11.3. Recipe Fragment

`frontend.requirements_extension`

Встраивается в `common.build_requirements_spec` и добавляет перед `requirements_spec_generation`:

1. `frontend.user_flow_analysis`
2. `frontend.screen_inventory_analysis`
3. `frontend.ui_constraints_review`

### 11.4. Result

После активации `frontend` pack:

- planner обязан пройти эти шаги;
- readiness по frontend становится частью admission;
- итоговый черновик ТЗ обязан учитывать frontend-часть проекта.

---

## 12. Инварианты

| Код | Инвариант |
|---|---|
| DP1 | Domain Pack не может ссылаться на отсутствующие templates / fragments / vocabularies |
| DP2 | Pack не может расширять recipe, не указав insertion points |
| DP3 | Pack не может активироваться скрыто; факт активации должен попасть в ProblemState |
| DP4 | Planner не должен содержать hard-coded ветки под конкретный pack |
| DP5 | Если pack влияет на requirements-stage, это должно быть выражено через recipe fragments, а не только через поздние implementation templates |

---

## 13. Что вне области этой спеки

- Runtime-исполнение доменных шаблонов
- UI для включения/отключения pack
- Алгоритмы domain classification через LLM

Эти вопросы описываются в execution/UI- и future-planning спеках.
