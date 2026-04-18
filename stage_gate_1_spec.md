# Спецификация Stage Gate 1 «Преобразование бизнес-запроса в ТЗ» (v2, модульная)

**Область применения:** первый макро-гейт платформы E2E-генерации PoV.
**Поддомены первой очереди:** RAG-системы, простые ML-модели (согласно `PoV.md`).
**Изменения относительно v1 (по комментарию заказчика):**

- Задачи сделаны переиспользуемыми — отвязаны от конкретного гейта, без префикса `BIZ_TO_TZ_`.
- Доменные знания вынесены в плагины (**Domain Pack**) — добавление поддомена не требует редактирования задач.
- Граф задач перенесён на отдельный слой (**Workflow**) — структура пайплайна отделена от спецификации самих задач.

---

## Часть 0. Архитектурная модель

Спецификация организована в три слоя с явным разделением ответственности.

```
┌─────────────────────────────────────────────────────────────────┐
│  Слой 1: Task Templates (универсальные задачи)                  │
│  — Чистые I/O-контракты                                         │
│  — Не знают, в каком гейте используются                         │
│  — Принимают Domain Pack как входной параметр                   │
│  — Пример: extract_typed_mentions, generate_document_section    │
└─────────────────────────────────────────────────────────────────┘
                          ▲                    ▲
                          │                    │
                          │ use (инстанцирует) │ parameterizes
                          │                    │
┌──────────────────────────┴───┐   ┌──────────┴──────────────────┐
│  Слой 2: Workflow Templates  │   │  Слой 3: Domain Packs       │
│  — Граф вызовов Task Templ.  │   │  — Плагины поддоменов       │
│  — Привязка данных           │   │  — Чеклисты, паттерны,      │
│  — Специфика гейта           │   │    шаблоны требований       │
│  — Политика эскалаций        │   │  — Пример: rag_v1, ml_v1    │
└──────────────────────────────┘   └─────────────────────────────┘
```

### 0.1. Task Templates

Универсальные задачи-шаблоны. Каждая имеет:

- **`template_id`** — глобально уникальный, без привязки к гейту.
- **Контракт входов и выходов** в терминах типов, а не конкретных артефактов.
- **`domain_pack` как необязательный вход** — через него задача получает доменные подсказки (примеры, чеклисты, паттерны).
- **`escalation_signals`** — чистые сигналы вида `low_confidence`, `ambiguous_input`, `limit_exhausted`. Что делать при сигнале — решает Workflow, не задача.
- **Три типа:** Executable, Composite, Dynamic (как в `ТЗ_Архитектура.md`).

**Принцип:** задача знает, ЧТО делает. Не знает, КОГДА и ЗАЧЕМ её вызвали и ЧТО БУДЕТ после её завершения.

### 0.2. Domain Packs

Плагины поддоменов. Каждый пак — структурированный артефакт в `Template Registry`, соответствующий единой схеме (раздел Части 2). Содержит:

- Сигналы классификации (как опознать поддомен)
- Типовые упоминания для каждой категории (данные, метрики, ...) — используется как контекст для `extract_typed_mentions`
- Чеклист обязательных полей для полноты запроса
- Шаблоны функциональных и нефункциональных требований
- Таксономию классов задач внутри поддомена
- Каталог архитектурных паттернов с правилами применимости
- Эвристики достаточности данных
- Политику синтетических данных
- Определения baseline-решений

**Добавление поддомена = создание нового Domain Pack + регистрация в реестре.** Код задач не меняется.

### 0.3. Workflow Templates

Графы вызовов задач для конкретных гейтов. Workflow описывает:

- Последовательность вызовов (фазы + узлы)
- Привязку данных между узлами (выход одного узла = вход другого)
- Политику реакции на escalation_signals от задач
- Условия перехода между фазами
- Выбор активного Domain Pack (обычно однократно в начале)

**Принцип разделения «механики от политики»:** задачи — механика (как сделать), Workflow — политика (когда, зачем, что при сбое). Это позволяет переиспользовать задачи в других гейтах с другой политикой.

### 0.4. Как это соотносится с архитектурой платформы

- **Template Registry** хранит всё три слоя: Task Templates, Domain Packs, Workflow Templates — разграничены по namespace.
- **Stage-Gate Manager** работает с Workflow Templates (выбирает и запускает подходящий для гейта).
- **Task Router** работает с экземплярами Task Templates (создаваемыми Workflow-движком).
- **Context Engine** при сборке контекста задачи подгружает активный Domain Pack и передаёт его как входной артефакт.

---

## Часть 1. Каталог Task Templates

Задачи сгруппированы по функциональным категориям. Одна и та же задача может вызываться в Workflow многократно с разными параметрами (пример: `extract_typed_mentions` вызывается 5 раз с разными значениями `mention_type`).

### 1.1. Категория: Обработка входного текста

```yaml
template_id: parse_free_text_to_structured
name: "Парсинг свободного текста в структуру"
type: Executable
description: "Преобразует произвольный текст в JSON по заданной схеме. Не интерпретирует смысл — только структурирует явно присутствующую информацию."
input_contract:
  - name: source_text
    type: string
    required: true
  - name: schema
    type: object
    required: true
    description: "JSON Schema, задающая структуру выхода."
  - name: domain_pack
    type: DomainPack
    required: false
    description: "Если передан — используется для подсказок, какие поля ожидать."
output_contract:
  - name: parsed_object
    type: object
    schema: "определяется параметром schema"
execution:
  kind: LLM
  constraints:
    - "Запрещено добавлять поля, не входящие в schema"
    - "Запрещено интерпретировать смысл — только структурирование"
    - "Отсутствующие поля заполняются null, не домысливаются"
escalation_signals:
  - parsing_failed: "Текст не поддаётся парсингу (бинарные данные, пустой вход)"
  - schema_violation: "LLM вернул ответ, не соответствующий schema, после 1 попытки самокоррекции"
```

```yaml
template_id: check_minimum_completeness
name: "Проверка минимальной полноты"
type: Executable
description: "Бинарное решение: содержит ли вход набор обязательных элементов по заданному чеклисту. Детерминированная логика."
input_contract:
  - name: parsed_input
    type: object
    required: true
  - name: checklist
    type: MinimumChecklist
    required: true
    description: "Список обязательных полей/элементов и правил их присутствия."
output_contract:
  - name: completeness_verdict
    type: object
    schema: "{is_complete: bool, missing: [str], reasoning: str}"
execution:
  kind: Tool
  constraints:
    - "Без LLM — формальная проверка"
escalation_signals:
  - incomplete: "is_complete=false (не ошибка задачи, но сигнал для Workflow)"
```

```yaml
template_id: classify_against_registry
name: "Классификация объекта по реестру"
type: Executable
description: "Относит объект (текст, запрос) к одной из категорий реестра. Используется в том числе для определения активного Domain Pack."
input_contract:
  - name: object_to_classify
    type: any
    required: true
  - name: registry
    type: ClassificationRegistry
    required: true
    description: "Реестр с описаниями категорий, сигналами и порогами уверенности."
output_contract:
  - name: classification_result
    type: object
    schema: "{category_id: str, confidence: float, reasoning: str, alternatives: [{category_id, confidence}]}"
execution:
  kind: LLM
  constraints:
    - "Категория строго из реестра — не изобретать новые"
    - "При confidence < порога обязательно заполнять alternatives"
escalation_signals:
  - unsupported_category: "Классификация = категория с флагом unsupported=true"
  - low_confidence: "confidence ниже порога даже после самокоррекции"
```

```yaml
template_id: check_registry_membership
name: "Проверка членства в реестре"
type: Executable
description: "Формальная проверка: входит ли объект в подмножество реестра, помеченное активным."
input_contract:
  - name: item
    type: any
    required: true
  - name: registry
    type: any
    required: true
  - name: filter
    type: object
    required: false
    description: "Дополнительные условия фильтрации (например, {status: active})."
output_contract:
  - name: membership_result
    type: object
    schema: "{is_member: bool, matched_record: object|null}"
execution:
  kind: Tool
escalation_signals: []
```

```yaml
template_id: detect_patterns_from_catalog
name: "Обнаружение паттернов в тексте по каталогу"
type: Executable
description: "Сканирует текст на присутствие паттернов из заданного каталога. Универсально — каталог задаёт, что ищем (нерешаемость, риски, индикаторы категории)."
input_contract:
  - name: source_text
    type: string
    required: true
  - name: pattern_catalog
    type: PatternCatalog
    required: true
    description: "Каталог с ID паттернов, описаниями, примерами, уровнями severity."
output_contract:
  - name: detected_patterns
    type: array
    schema: "[{pattern_id, evidence_quote, severity}]"
execution:
  kind: LLM
  constraints:
    - "Каждое обнаружение обязано содержать прямую цитату-доказательство"
    - "Запрещено обнаруживать паттерны вне каталога"
escalation_signals:
  - blocker_detected: "Найден паттерн с severity=blocker"
  - ambiguous_match: "LLM не может однозначно определить совпадение после самокоррекции"
```

```yaml
template_id: synthesize_verdict
name: "Синтез решения из входных сигналов по правилам"
type: Executable
description: "Принимает набор сигналов (булевых или структурированных) и правила их комбинирования, возвращает итоговое решение с обоснованием."
input_contract:
  - name: signals
    type: array
    required: true
  - name: decision_rules
    type: DecisionRules
    required: true
    description: "Правила комбинирования сигналов в итоговое решение."
output_contract:
  - name: verdict
    type: object
    schema: "{decision: str, rationale: str, applied_rules: [rule_id]}"
execution:
  kind: Tool
  constraints:
    - "Без LLM — детерминированное применение правил"
escalation_signals: []
```

### 1.2. Категория: Анализ смысла текста

```yaml
template_id: extract_declared_goal
name: "Извлечение декларируемой цели из текста"
type: Executable
description: "Формулирует в одном предложении то, что ЯВНО просят сделать. Без интерпретации причин."
input_contract:
  - name: source_text
    type: string
    required: true
  - name: domain_pack
    type: DomainPack
    required: false
    description: "Даёт few-shot примеры формулировок целей для поддомена."
output_contract:
  - name: declared_goal
    type: object
    schema: "{goal_statement: str, direct_quotes: [str]}"
execution:
  kind: LLM
  constraints:
    - "goal_statement — одно предложение"
    - "Запрещены формулировки 'чтобы X' без явного подтверждения в direct_quotes"
    - "Минимум одна цитата из source_text"
escalation_signals:
  - no_goal_found: "В тексте нет явной цели — предположительно вход неполный"
```

```yaml
template_id: generate_hypotheses
name: "Генерация гипотез по теме"
type: Executable
description: "Универсальный генератор гипотез: о первопричине, о baseline-процессе, о любой другой теме, задаваемой параметром hypothesis_topic."
input_contract:
  - name: source_text
    type: string
    required: true
  - name: context_artifacts
    type: array
    required: false
    description: "Дополнительный контекст (например, declared_goal для генерации гипотез о первопричине)."
  - name: hypothesis_topic
    type: string
    required: true
    description: "Тема гипотез: 'root_cause', 'baseline_process', ... Определяет промпт-вариацию."
  - name: domain_pack
    type: DomainPack
    required: false
    description: "Для baseline-гипотез — типовые baseline для поддомена."
  - name: max_hypotheses
    type: integer
    required: false
    default: 4
output_contract:
  - name: hypotheses
    type: array
    schema: "[{hypothesis, supporting_signals: [str], plausibility: float, verification_question: str}]"
execution:
  kind: LLM
  constraints:
    - "Каждая гипотеза — поле verification_question обязательно"
    - "Минимум 2 гипотезы, максимум — max_hypotheses"
    - "Гипотезы по возможности взаимоисключающие"
escalation_signals:
  - all_low_plausibility: "Все гипотезы получили plausibility < 0.3 — входная информация слишком абстрактна"
```

```yaml
template_id: identify_stakeholders
name: "Выявление стейкхолдеров и их интересов"
type: Executable
description: "Определяет роли, затронутые задачей, и предполагаемые интересы каждой роли."
input_contract:
  - name: source_text
    type: string
    required: true
  - name: context_artifacts
    type: array
    required: false
  - name: domain_pack
    type: DomainPack
    required: false
    description: "Задаёт обязательные стейкхолдеры для поддомена (например, для RAG: asker, corpus_owner)."
output_contract:
  - name: stakeholders_map
    type: array
    schema: "[{role, interest, explicit_in_source: bool}]"
execution:
  kind: LLM
  constraints:
    - "Минимум 2 роли"
    - "Если domain_pack задаёт обязательных стейкхолдеров — они должны присутствовать"
    - "Если роль не упомянута явно — explicit_in_source=false (сигнал для последующего gap-анализа)"
escalation_signals: []
```

```yaml
template_id: consolidate_analysis
name: "Консолидация набора аналитических артефактов"
type: Executable
description: "Сводит результаты нескольких анализов в единую модель, выявляет противоречия и формирует открытые вопросы."
input_contract:
  - name: artifacts_to_consolidate
    type: array
    required: true
    description: "Набор связанных артефактов (например, declared_goal + hypotheses + stakeholders_map)."
  - name: consolidation_schema
    type: object
    required: true
    description: "Схема итоговой модели."
output_contract:
  - name: consolidated_model
    type: object
    schema: "определяется consolidation_schema + стандартные поля: internal_contradictions, open_questions"
execution:
  kind: LLM
  constraints:
    - "open_questions содержит все найденные internal_contradictions как явные вопросы"
    - "Ограничение когнитивной нагрузки на заказчика: не более 7 open_questions"
escalation_signals:
  - contradictions_unresolvable: "Противоречия настолько нечёткие, что не формулируются как вопрос"
```

### 1.3. Категория: Извлечение упоминаний

Одна универсальная задача вместо пяти специализированных (из v1 это 401–405).

```yaml
template_id: extract_typed_mentions
name: "Извлечение типизированных упоминаний из текста"
type: Executable
description: "Находит упоминания заданного типа (данные / метрики / ограничения / критерии приёмки / интеграции / любой другой тип, описанный в Domain Pack). НЕ классифицирует и НЕ оценивает — только вычленяет."
input_contract:
  - name: source_text
    type: string
    required: true
  - name: mention_type
    type: string
    required: true
    description: "Тип упоминаний: 'data' | 'metrics' | 'constraints' | 'acceptance' | 'integrations' | любой тип из Domain Pack."
  - name: domain_pack
    type: DomainPack
    required: true
    description: "Из domain_pack.extraction_hints[mention_type] берутся: typical_types, domain_examples, extraction_guidelines."
output_contract:
  - name: typed_mentions
    type: array
    schema: "[{mention, quote, extracted_attributes: object}]"
execution:
  kind: LLM
  constraints:
    - "Каждое упоминание обязано иметь quote (прямую цитату)"
    - "Атрибуты заполняются только если явно упомянуты"
    - "Пустой массив — валидный результат"
escalation_signals: []
```

### 1.4. Категория: Gap-анализ

```yaml
template_id: compare_against_checklist
name: "Сверка набора элементов с чеклистом"
type: Executable
description: "Детерминированная сверка: для каждого элемента чеклиста проверяет наличие соответствия в наборе данных. Универсально — чеклист задаётся параметром."
input_contract:
  - name: items
    type: any
    required: true
  - name: checklist
    type: Checklist
    required: true
  - name: additional_inputs
    type: array
    required: false
    description: "Дополнительные источники (например, open_questions из модели потребности)."
output_contract:
  - name: gap_report
    type: array
    schema: "[{field, category, required: bool, source, hint}]"
execution:
  kind: Tool
escalation_signals: []
```

```yaml
template_id: prioritize_items
name: "Приоритезация набора элементов"
type: Executable
description: "Присваивает каждому элементу приоритет по правилам, заданным параметром."
input_contract:
  - name: items
    type: array
    required: true
  - name: priority_rules
    type: PriorityRules
    required: true
    description: "Правила присвоения приоритетов (например, {blocking: ..., important: ..., nice_to_have: ...})."
  - name: context
    type: object
    required: false
output_contract:
  - name: prioritized_items
    type: array
    schema: "[{item, priority, reasoning}]"
execution:
  kind: LLM
  constraints:
    - "reasoning обязательно для каждого элемента"
    - "Правила из priority_rules должны быть соблюдены (элементы с required=true не могут быть ниже important)"
escalation_signals:
  - extreme_distribution: "Доля blocking элементов > 70% (сигнал слишком неполного входа)"
```

```yaml
template_id: group_and_structure_items
name: "Группировка и структурирование набора элементов"
type: Executable
description: "Группирует элементы по темам и добавляет сводную статистику. Универсально — темы и схема сводки задаются параметром."
input_contract:
  - name: items
    type: array
    required: true
  - name: grouping_schema
    type: object
    required: true
    description: "Темы для группировки + схема сводной статистики."
output_contract:
  - name: structured_items
    type: object
    schema: "определяется grouping_schema"
execution:
  kind: LLM
escalation_signals: []
```

### 1.5. Категория: Взаимодействие с пользователем

```yaml
template_id: generate_batched_questionnaire
name: "Формирование пакетного опросника"
type: Executable
description: "Из набора структурированных вопросов формирует человекочитаемый опросник с группировкой и обоснованиями. Подходит для любого случая массового сбора информации."
input_contract:
  - name: question_specs
    type: array
    required: true
    description: "Исходные вопросы с метаданными (тема, приоритет, ожидаемый формат ответа)."
  - name: domain_pack
    type: DomainPack
    required: false
    description: "Библиотека типовых формулировок для поддомена."
  - name: max_questions
    type: integer
    required: false
    default: 10
output_contract:
  - name: questionnaire
    type: object
    schema: "{human_readable: markdown, response_schema: JSONSchema}"
execution:
  kind: LLM
  constraints:
    - "Не более max_questions вопросов"
    - "Группировка по темам"
    - "Каждый вопрос сопровождается кратким обоснованием, ЗАЧЕМ спрашиваем"
    - "Приоритизация: сначала blocking, потом important"
escalation_signals:
  - too_many_questions: "Не удалось уложиться в max_questions даже после приоритезации"
```

```yaml
template_id: generate_point_question
name: "Формирование точечного вопроса"
type: Executable
description: "Один открытый вопрос по конкретной теме. Используется для быстрых уточнений."
input_contract:
  - name: question_spec
    type: object
    required: true
    description: "Спецификация одного вопроса (тема, контекст, ожидаемый формат)."
  - name: domain_pack
    type: DomainPack
    required: false
output_contract:
  - name: point_question
    type: object
    schema: "{question, context, expected_format}"
execution:
  kind: LLM
  constraints:
    - "Ровно один вопрос, не более 2 предложений"
    - "Язык формулировки — без технического жаргона без необходимости"
escalation_signals: []
```

```yaml
template_id: request_user_input_via_gateway
name: "Запрос ввода пользователя через Interruption Gateway"
type: Executable
description: "Передаёт запрос в систему общения с пользователем через Interruption Gateway, ожидает внешнее событие 'response_received'."
input_contract:
  - name: request_payload
    type: any
    required: true
    description: "Опросник, точечный вопрос или любой запрос для отправки."
  - name: timeout_hours
    type: integer
    required: false
    default: 48
  - name: purpose
    type: string
    required: true
    description: "Семантическая метка цели запроса (для UI, телеметрии, классификации ответа)."
output_contract:
  - name: user_response_raw
    type: any
execution:
  kind: Human
  constraints:
    - "Задача не вызывает LLM"
    - "Ожидание не блокирует остальной граф"
escalation_signals:
  - timeout_exceeded: "Превышен timeout_hours"
  - cannot_answer: "Получен ответ вида 'не знаю' / 'затрудняюсь'"
```

```yaml
template_id: parse_user_response
name: "Парсинг ответа пользователя"
type: Executable
description: "Превращает свободный ответ пользователя в структурированные данные по схеме исходного запроса."
input_contract:
  - name: response_raw
    type: any
    required: true
  - name: response_schema
    type: JSONSchema
    required: true
  - name: original_request
    type: any
    required: false
    description: "Если был опросник — связывает ответы с вопросами."
output_contract:
  - name: parsed_response
    type: object
    schema: "определяется response_schema + поля not_answered: [str]"
execution:
  kind: LLM
  constraints:
    - "Запрещено додумывать ответы на неотвеченные вопросы"
    - "not_answered должен явно содержать все пропущенные вопросы"
escalation_signals: []
```

```yaml
template_id: validate_response
name: "Валидация содержательности ответа"
type: Executable
description: "Проверяет, что ответы осмысленны, отвечают на заданные вопросы и не противоречат ранее известной информации."
input_contract:
  - name: parsed_response
    type: object
    required: true
  - name: original_questions
    type: array
    required: true
  - name: prior_knowledge
    type: object
    required: false
    description: "Ранее установленные факты для проверки противоречий."
output_contract:
  - name: validation_result
    type: object
    schema: "{valid_answers, contradictions, unanswered_critical}"
execution:
  kind: LLM
  constraints:
    - "Противоречия формулируются как пары (известное, новое)"
escalation_signals:
  - contradictions_detected: "Противоречия между ответом и prior_knowledge"
  - critical_unanswered: "Остались без ответа вопросы с priority=blocking"
```

### 1.6. Категория: Инвентаризация и характеризация объектов

```yaml
template_id: inventory_items_from_mentions
name: "Инвентаризация объектов из набора упоминаний"
type: Executable
description: "Из разрозненных упоминаний формирует нормализованный реестр объектов с ID, известными и неизвестными атрибутами. Универсально — применимо к источникам данных, внешним системам, сущностям."
input_contract:
  - name: mentions
    type: array
    required: true
  - name: additional_info
    type: array
    required: false
    description: "Ответы пользователя или другой контекст."
  - name: object_type_schema
    type: object
    required: true
    description: "Схема объекта: какие атрибуты может иметь, как их нормализовать."
  - name: domain_pack
    type: DomainPack
    required: false
output_contract:
  - name: objects_inventory
    type: array
    schema: "[{id, name, type, known_attributes, unknown_attributes: [str]}]"
execution:
  kind: LLM
  constraints:
    - "Дедупликация: одно и то же название = один объект"
    - "Каждый объект получает уникальный ID"
    - "unknown_attributes содержит атрибуты, известные схеме, но не заполненные — сигнал для последующего уточнения"
escalation_signals:
  - inventory_empty: "Упоминаний нет, хотя объекты требуются (сигнал для порождения уточняющего запроса)"
```

```yaml
template_id: generate_clarification_questions
name: "Генерация уточняющих вопросов по объекту"
type: Executable
description: "Для конкретного объекта формирует вопросы об атрибутах, попадающих в указанную категорию. Вопросы сгруппированы так, чтобы уточнение шло пакетами по смысловым измерениям."
input_contract:
  - name: target_object
    type: object
    required: true
  - name: attribute_category
    type: string
    required: true
    description: "Например: 'format_volume' | 'quality_labeling' | 'legal_access' | любая категория из Domain Pack."
  - name: domain_pack
    type: DomainPack
    required: true
    description: "Шаблоны вопросов для категории берутся из domain_pack.clarification_templates[attribute_category]."
output_contract:
  - name: clarification_questions
    type: array
    schema: "[{question, expected_format, target_attribute}]"
execution:
  kind: LLM
escalation_signals: []
```

```yaml
template_id: evaluate_sufficiency_by_heuristics
name: "Оценка достаточности по эвристикам"
type: Executable
description: "Применяет эвристики из Domain Pack к набору объектов и возвращает вердикт достаточности с оценкой риска."
input_contract:
  - name: objects
    type: array
    required: true
  - name: domain_pack
    type: DomainPack
    required: true
    description: "Эвристики берутся из domain_pack.sufficiency_heuristics для применимого класса задачи."
  - name: task_class_id
    type: string
    required: true
    description: "Класс задачи в рамках поддомена (определяет применимые эвристики)."
output_contract:
  - name: sufficiency_verdict
    type: object
    schema: "{sufficient: bool, gaps: [str], risk_level: enum[low, medium, high]}"
execution:
  kind: LLM
  constraints:
    - "sufficient=true допустимо только при risk_level ∈ {low, medium}"
    - "gaps содержит конкретные недостающие параметры со ссылками на эвристики"
escalation_signals:
  - insufficient_no_fallback: "sufficient=false и Domain Pack не разрешает синтетику для этого класса задачи"
```

```yaml
template_id: decide_synthetic_fallback
name: "Решение об использовании синтетических данных"
type: Executable
description: "При недостаточности реальных данных — решает, применим ли синтетический fallback согласно политике Domain Pack. Формирует обоснование и явные ограничения."
input_contract:
  - name: sufficiency_verdict
    type: object
    required: true
  - name: domain_pack
    type: DomainPack
    required: true
    description: "domain_pack.synthetic_data_policy определяет: для чего разрешено, для чего запрещено."
  - name: task_class_id
    type: string
    required: true
output_contract:
  - name: synthetic_decision
    type: object
    schema: "{use_synthetic: bool, rationale, synthetic_for: [str], limitations: [str]}"
execution:
  kind: LLM
  constraints:
    - "Если use_synthetic=true, limitations не может быть пустым"
    - "synthetic_for — только категории, явно разрешённые политикой"
escalation_signals:
  - needs_client_confirmation: "Решение об использовании синтетики требует явного подтверждения пользователя"
```

### 1.7. Категория: Формализация требований

```yaml
template_id: generate_requirements_from_templates
name: "Генерация требований из доменных шаблонов"
type: Executable
description: "Создаёт список формализованных требований (ФТ или НФТ — задаётся параметром) на основе шаблонов из Domain Pack и контекста задачи."
input_contract:
  - name: requirement_kind
    type: string
    required: true
    description: "'functional' | 'non_functional'."
  - name: context_artifacts
    type: array
    required: true
    description: "Артефакты, влияющие на формулировки (need_model, mentions, clarifications)."
  - name: domain_pack
    type: DomainPack
    required: true
    description: "Шаблоны требований — из domain_pack.requirements_templates[requirement_kind]."
  - name: id_prefix
    type: string
    required: false
    default: "FR"
output_contract:
  - name: requirements
    type: array
    schema: "[{id, statement, priority, category, target_value, source}]"
execution:
  kind: LLM
  constraints:
    - "Каждое требование имеет уникальный ID с префиксом id_prefix"
    - "Формулировка в форме 'Система должна ...' (для ФТ) или привязанной к категории (для НФТ)"
    - "Обязательна трассировка (source) к конкретному входному артефакту"
    - "Базовый набор требований из шаблонов Domain Pack должен присутствовать (если применим)"
escalation_signals:
  - base_set_unachievable: "Не удаётся сформулировать даже базовый набор из шаблонов Domain Pack (признак недоопределённого входа)"
```

```yaml
template_id: generate_constraints_from_mentions
name: "Формализация ограничений"
type: Executable
description: "Превращает упоминания ограничений и следствия из других артефактов в формальные Constraint-записи."
input_contract:
  - name: constraint_mentions
    type: array
    required: true
  - name: derived_constraints_sources
    type: array
    required: false
    description: "Источники, из которых ограничения выводятся (например, data_specification → constraint по защите ПД)."
  - name: domain_pack
    type: DomainPack
    required: false
output_contract:
  - name: constraints
    type: array
    schema: "[{id, type, statement, is_hard: bool, source}]"
execution:
  kind: LLM
escalation_signals: []
```

```yaml
template_id: generate_acceptance_criteria
name: "Формирование критериев приёмки"
type: Executable
description: "Для каждого must-требования и ключевых НФТ генерирует измеримый критерий приёмки с методом проверки."
input_contract:
  - name: requirements
    type: array
    required: true
    description: "Объединённый список ФТ и НФТ."
  - name: metric_mentions
    type: array
    required: false
  - name: acceptance_mentions
    type: array
    required: false
  - name: domain_pack
    type: DomainPack
    required: false
    description: "Для примеров методов проверки в поддомене."
output_contract:
  - name: acceptance_criteria
    type: array
    schema: "[{id, statement, measurable: bool, target, verification_method, linked_requirements}]"
execution:
  kind: LLM
  constraints:
    - "Каждое must-требование покрыто хотя бы одним критерием"
    - "verification_method конкретный (не 'проверяется вручную' без деталей)"
    - "measurable=false допустимо только при явном обосновании"
escalation_signals:
  - unmeasurable_must_requirement: "Must-требование без возможности сформулировать измеримый критерий"
```

```yaml
template_id: detect_contradictions
name: "Поиск противоречий в наборе элементов"
type: Executable
description: "Универсальный поиск противоречий между элементами (требования vs ограничения, ответы заказчика vs ранее известное, архитектурный выбор vs требования)."
input_contract:
  - name: items
    type: array
    required: true
  - name: contradiction_rules
    type: ContradictionRules
    required: true
    description: "Правила попарной или групповой проверки на противоречия."
output_contract:
  - name: contradictions_report
    type: object
    schema: "{is_consistent: bool, contradictions: [{description, involved_items, severity}]}"
execution:
  kind: LLM
  constraints:
    - "Каждое противоречие описано с упоминанием конкретных ID элементов"
escalation_signals:
  - critical_contradictions: "Есть противоречия с severity=blocker"
```

### 1.8. Категория: Архитектурный анализ

```yaml
template_id: classify_within_taxonomy
name: "Классификация задачи по таксономии"
type: Executable
description: "Относит задачу к классу из доменной таксономии. Универсально — таксономия приходит из Domain Pack."
input_contract:
  - name: context_artifacts
    type: array
    required: true
  - name: domain_pack
    type: DomainPack
    required: true
    description: "Таксономия — domain_pack.task_class_taxonomy."
output_contract:
  - name: task_class
    type: object
    schema: "{class_id, class_name, confidence, reasoning, alternatives: []}"
execution:
  kind: LLM
  constraints:
    - "class_id строго из таксономии"
    - "При confidence < 0.7 — обязательны alternatives"
escalation_signals:
  - no_matching_class: "Задача не укладывается ни в один класс таксономии (сигнал к пересмотру выбора Domain Pack)"
```

```yaml
template_id: select_pattern_from_catalog
name: "Выбор паттерна из доменного каталога"
type: Executable
description: "Выбирает архитектурный паттерн из каталога Domain Pack на основе класса задачи и характеристик входа."
input_contract:
  - name: task_class_id
    type: string
    required: true
  - name: input_characteristics
    type: object
    required: true
    description: "Данные для применения правил выбора (объём данных, требования к качеству, ...)."
  - name: domain_pack
    type: DomainPack
    required: true
    description: "Каталог паттернов — domain_pack.architecture_patterns."
output_contract:
  - name: pattern_choice
    type: object
    schema: "{pattern_id, components, rationale}"
execution:
  kind: LLM
  constraints:
    - "pattern_id строго из каталога"
    - "Правила применимости из каталога соблюдены"
    - "rationale ссылается на конкретные требования"
escalation_signals:
  - no_pattern_satisfies_constraints: "Ни один паттерн не удовлетворяет входным характеристикам"
```

```yaml
template_id: generate_rationale
name: "Генерация обоснования выбора"
type: Executable
description: "Формирует связный текст, обосновывающий выбор, через таблицу сопоставления 'требование → как удовлетворяется'."
input_contract:
  - name: chosen_option
    type: object
    required: true
  - name: requirements
    type: array
    required: true
  - name: alternatives_considered
    type: array
    required: false
output_contract:
  - name: rationale_document
    type: string
    format: markdown
execution:
  kind: LLM
  constraints:
    - "Таблица сопоставления покрывает все must-требования"
    - "Отдельный блок 'Риски' с мерами митигации (минимум 2 риска)"
escalation_signals: []
```

```yaml
template_id: define_baseline
name: "Определение baseline-решения"
type: Executable
description: "Формирует описание baseline — простого решения для сравнения. Берёт типовой baseline для класса задачи из Domain Pack и адаптирует под контекст."
input_contract:
  - name: task_class_id
    type: string
    required: true
  - name: acceptance_criteria
    type: array
    required: true
  - name: domain_pack
    type: DomainPack
    required: true
    description: "domain_pack.baseline_definitions."
output_contract:
  - name: baseline
    type: object
    schema: "{description, expected_limitations: [str], comparison_metrics: [str]}"
execution:
  kind: LLM
  constraints:
    - "Baseline проще основного решения"
    - "comparison_metrics ⊆ измеримых acceptance_criteria"
escalation_signals:
  - no_shared_metrics: "Нет метрик, общих между baseline и основным решением"
```

### 1.9. Категория: Сборка и валидация документов

```yaml
template_id: generate_document_section
name: "Генерация раздела документа"
type: Executable
description: "Пишет один раздел документа по заданной структуре и входным артефактам. Параметризуется типом раздела — одна задача заменяет шесть монолитных из v1."
input_contract:
  - name: section_spec
    type: object
    required: true
    description: "Спецификация раздела: тип, обязательная структура, ограничения по длине."
  - name: input_artifacts
    type: array
    required: true
    description: "Артефакты, из которых собирается раздел."
  - name: domain_pack
    type: DomainPack
    required: false
    description: "Может предоставлять шаблоны формулировок для поддомена."
output_contract:
  - name: section_content
    type: string
    format: markdown
execution:
  kind: LLM
  constraints:
    - "Опираться только на input_artifacts — не выдумывать факты"
    - "Заголовок раздела задаётся section_spec"
    - "Длина в пределах, заданных section_spec"
    - "Каждое фактическое утверждение — трассировка к конкретному input_artifact"
escalation_signals:
  - empty_mandatory_inputs: "Hard-зависимость пуста (предыдущий блок не выполнил свою задачу)"
```

```yaml
template_id: assemble_document
name: "Механическая сборка документа"
type: Executable
description: "Соединяет секции в единый документ, генерирует оглавление, проставляет сквозную нумерацию."
input_contract:
  - name: sections
    type: array
    required: true
  - name: document_metadata
    type: object
    required: true
    description: "Название, версия, статус, автор."
output_contract:
  - name: document
    type: string
    format: markdown
execution:
  kind: Tool
  constraints:
    - "Без LLM — чисто механическая сборка"
escalation_signals:
  - section_missing: "Одна из обязательных секций пуста"
```

```yaml
template_id: check_document_completeness
name: "Проверка полноты документа по чеклисту"
type: Executable
description: "Формальная проверка наличия и непустоты обязательных разделов и полей."
input_contract:
  - name: document
    type: string
    required: true
  - name: completeness_checklist
    type: Checklist
    required: true
output_contract:
  - name: completeness_report
    type: object
    schema: "{is_complete: bool, missing_items: [{item, severity}]}"
execution:
  kind: Tool
escalation_signals:
  - critical_missing: "Отсутствует пункт с severity=critical"
```

```yaml
template_id: check_internal_consistency
name: "Проверка внутренней непротиворечивости документа"
type: Executable
description: "Ищет противоречия между разделами документа."
input_contract:
  - name: document
    type: string
    required: true
  - name: consistency_rules
    type: ConsistencyRules
    required: true
    description: "Правила попарной проверки элементов документа."
output_contract:
  - name: consistency_report
    type: object
    schema: "{is_consistent: bool, contradictions: [{description, involved, severity}]}"
execution:
  kind: LLM
escalation_signals:
  - blocker_contradictions: "Найдены противоречия с severity=blocker"
```

```yaml
template_id: check_traceability
name: "Проверка трассируемости элементов к источнику"
type: Executable
description: "Для каждого ключевого элемента документа проверяет наличие обоснования в указанных источниках (исходный запрос, ответы пользователя)."
input_contract:
  - name: document
    type: string
    required: true
  - name: source_artifacts
    type: array
    required: true
  - name: traceability_spec
    type: object
    required: true
    description: "Какие элементы документа должны трассироваться + минимальный порог покрытия."
output_contract:
  - name: traceability_report
    type: object
    schema: "{traced: int, untraced: [{element_id, content}], coverage: float}"
execution:
  kind: LLM
escalation_signals:
  - coverage_below_threshold: "coverage < traceability_spec.min_coverage"
```

```yaml
template_id: request_approval_via_gateway
name: "Запрос согласования документа через Interruption Gateway"
type: Executable
description: "Отправляет документ на согласование пользователю, ждёт одного из трёх решений: approved / approved_with_comments / rejected."
input_contract:
  - name: document
    type: string
    required: true
  - name: cover_message
    type: string
    required: false
    description: "Сопроводительное сообщение — рекомендуется подсвечивать ключевые допущения."
  - name: timeout_hours
    type: integer
    required: false
    default: 120
output_contract:
  - name: approval_decision
    type: object
    schema: "{decision: enum[approved, approved_with_comments, rejected], comments_raw, timestamp}"
execution:
  kind: Human
escalation_signals:
  - timeout_exceeded: "Превышен timeout_hours"
  - deep_rework_requested: "decision=rejected — требуется глубокая переделка"
```

```yaml
template_id: classify_comments
name: "Классификация комментариев к документу"
type: Executable
description: "Разбирает комментарии пользователя: привязка к разделу/элементу, тип изменения, критичность, список задач, которые нужно перезапустить."
input_contract:
  - name: comments_raw
    type: string
    required: true
  - name: document
    type: string
    required: true
  - name: task_mapping
    type: object
    required: true
    description: "Маппинг 'раздел/элемент документа → задачи Workflow, которые его порождают'."
output_contract:
  - name: classified_comments
    type: array
    schema: "[{comment_text, target, change_type, criticality, triggered_workflow_nodes: [str]}]"
execution:
  kind: LLM
escalation_signals:
  - comment_unlocalizable: "Комментарий не удаётся привязать к конкретному элементу"
  - scope_change_required: "Комментарии требуют выхода за рамки текущего гейта"
```

```yaml
template_id: finalize_artifact
name: "Финальная фиксация артефакта"
type: Executable
description: "Помечает артефакт как approved, сохраняет финальную версию, пишет метаданные, отправляет событие завершения."
input_contract:
  - name: artifact_content
    type: any
    required: true
  - name: approval_decision
    type: object
    required: true
  - name: finalization_spec
    type: object
    required: true
    description: "Куда сохранить, какие метаданные записать, какое событие отправить."
output_contract:
  - name: finalized_artifact
    type: object
    schema: "{content, version, approved_at, approved_by, trace_id}"
execution:
  kind: Tool
escalation_signals:
  - storage_failure: "Техническая ошибка при сохранении"
```

---

## Часть 2. Спецификация Domain Pack

### 2.1. Схема Domain Pack

```yaml
# Метаданные
domain_pack_id: str          # уникальный идентификатор (например, 'rag_v1')
name: str                    # человекочитаемое название
version: str                 # SemVer
description: str
status: enum[active, deprecated, experimental]
parent_pack: str | null      # для наследования от другого пака

# ==================== Классификация поддомена ====================
classification:
  # Используется template_id=classify_against_registry при выборе активного Domain Pack
  positive_signals: [str]    # фразы/признаки, характеризующие поддомен
  negative_signals: [str]    # признаки, исключающие поддомен
  typical_goals: [str]       # примеры типовых формулировок целей (для few-shot)
  confidence_threshold: float

# ==================== Границы применимости ====================
applicability:
  # Ранняя feasibility check
  supported_task_classes: [str]   # классы задач, которые Domain Pack поддерживает
  out_of_scope_patterns: [str]    # типовые out-of-scope сценарии (дополняют глобальный каталог нерешаемости)

# ==================== Извлечение упоминаний ====================
extraction_hints:
  # Потребляется template_id=extract_typed_mentions
  # Ключ — mention_type, значение — подсказки для LLM
  data:
    typical_types: [str]     # для RAG: [document_corpus, wiki, email_archive, codebase]
    domain_examples: [str]   # конкретные примеры из типовых проектов поддомена
    attribute_schema: {...}  # какие атрибуты пытаться извлечь (format, volume, ...)
  metrics:
    typical_metrics: [str]
    domain_specific_categories: [str]
  constraints:
    typical_constraints: [str]
  acceptance:
    typical_criteria: [str]
  integrations:
    typical_systems: [str]

# ==================== Обязательные стейкхолдеры ====================
mandatory_stakeholders: [str]  # потребляется identify_stakeholders

# ==================== Чеклист полноты ====================
completeness_checklist:
  # Потребляется compare_against_checklist и prioritize_items
  - field: str
    category: enum[data, requirements, integrations, stakeholders, other]
    priority: enum[blocking, important, nice_to_have]
    question_hint: str        # как переформулировать пробел в вопрос
    verification_rule: str    # правило, как проверить присутствие в извлечённых декларациях

# ==================== Шаблоны вопросов ====================
clarification_templates:
  # Потребляется generate_clarification_questions
  # Ключ — категория атрибутов, значение — шаблоны вопросов
  format_volume: [{question_template, expected_format, applicable_when}]
  quality_labeling: [...]
  legal_access: [...]
  # ... другие категории, специфичные для поддомена

question_library:
  # Потребляется generate_batched_questionnaire и generate_point_question
  # Библиотека типовых формулировок для переиспользования
  - topic: str
    question_variants: [str]
    response_schema: JSONSchema

# ==================== Эвристики достаточности данных ====================
sufficiency_heuristics:
  # Потребляется evaluate_sufficiency_by_heuristics
  - for_task_class: str
    rules: [{metric, threshold, risk_if_below}]
    min_recommended: object

# ==================== Политика синтетики ====================
synthetic_data_policy:
  # Потребляется decide_synthetic_fallback
  allowed_for: [str]       # например, для RAG: ['user_queries'], не 'corpus'
  never_for: [str]
  default_limitations: [str]

# ==================== Таксономия классов задач ====================
task_class_taxonomy:
  # Потребляется classify_within_taxonomy
  - class_id: str
    class_name: str
    signals: [str]
    typical_inputs: [str]
    typical_outputs: [str]

# ==================== Архитектурные паттерны ====================
architecture_patterns:
  # Потребляется select_pattern_from_catalog
  - pattern_id: str
    name: str
    description: str
    applicable_to: [task_class_id]
    applicability_rules: [{condition, requirement}]
    components: {...}
    complexity_level: enum[low, medium, high]
    default_for_mvp: bool

# ==================== Шаблоны требований ====================
requirements_templates:
  # Потребляется generate_requirements_from_templates
  functional:
    - id_suffix: str
      statement_template: str
      default_priority: enum[must, should, could]
      applicable_to_task_classes: [str] | "all"
      conditional_on: str | null    # условие включения
  non_functional:
    - id_suffix: str
      category: enum[performance, reliability, maintainability, usability, security]
      statement_template: str
      default_target: str
      default_priority: enum[must, should, could]

# ==================== Baseline-решения ====================
baseline_definitions:
  # Потребляется define_baseline
  - for_task_class: str
    description_template: str
    typical_limitations: [str]
    typical_metrics: [str]
```

### 2.2. Domain Pack: RAG (фрагмент для иллюстрации)

```yaml
domain_pack_id: rag_v1
name: "RAG-системы"
version: "1.0.0"
status: active

classification:
  positive_signals:
    - "поиск по документам"
    - "ответы на вопросы по корпусу"
    - "извлечение информации из базы знаний"
    - "чат с документами"
    - "knowledge base Q&A"
  negative_signals:
    - "предсказание числового значения"
    - "классификация на фиксированное число классов"
    - "регрессия"
  typical_goals:
    - "Давать ответы сотрудникам по внутренней документации"
    - "Находить релевантные фрагменты в корпусе документов"
  confidence_threshold: 0.7

applicability:
  supported_task_classes: [chat_with_docs, faq_qa, structured_extraction, summarization_over_corpus]
  out_of_scope_patterns:
    - id: realtime_streaming_rag
      description: "RAG с требованием latency < 100ms"
      severity: warning

extraction_hints:
  data:
    typical_types: [document_corpus, wiki, confluence, sharepoint, email_archive, codebase]
    domain_examples:
      - "документация в Confluence"
      - "база вопросов-ответов"
      - "корпус PDF-документов"
    attribute_schema:
      format: enum[pdf, docx, html, txt, markdown, mixed]
      volume: {count, total_size_bytes}
      language: [str]
      update_frequency: enum[static, periodic, continuous]
  metrics:
    typical_metrics:
      - retrieval_precision
      - answer_faithfulness
      - citation_coverage
      - response_latency_p95
  integrations:
    typical_systems: [confluence, sharepoint, github, jira, ldap_sso]

mandatory_stakeholders: [asker, corpus_owner]

completeness_checklist:
  - field: corpus_description
    category: data
    priority: blocking
    question_hint: "Какой корпус документов используется? Объём, формат, язык?"
    verification_rule: "mentions.data содержит хотя бы один объект с type=document_corpus"
  - field: query_types
    category: requirements
    priority: blocking
    question_hint: "Какие вопросы задают пользователи? 3-5 типовых примеров"
  - field: response_quality_bar
    category: requirements
    priority: important
    question_hint: "Какое качество ответов считается приемлемым?"
  - field: answer_format
    category: requirements
    priority: important
    question_hint: "В каком формате ожидается ответ: короткий/развёрнутый, с цитатами/без?"
  - field: corpus_update_policy
    category: data
    priority: nice_to_have

clarification_templates:
  format_volume:
    - question_template: "В каком формате хранятся документы? (PDF, DOCX, HTML, txt)"
      expected_format: "enum"
    - question_template: "Сколько документов в корпусе и каков средний размер?"
      expected_format: "numeric + units"
  quality_labeling:
    - question_template: "Есть ли у документов метаданные (дата, автор, тип)?"
    - question_template: "Какая доля документов устарела? Есть ли дубликаты?"
  legal_access:
    - question_template: "Содержат ли документы ПД или конфиденциальные сведения?"
    - question_template: "Как технически получить доступ: API, выгрузка, дамп?"

sufficiency_heuristics:
  - for_task_class: chat_with_docs
    rules:
      - metric: corpus_document_count
        threshold: 50
        risk_if_below: high
      - metric: avg_document_length
        threshold: 200
        risk_if_below: medium
    min_recommended: {count: 200, languages_coverage: "все целевые языки"}

synthetic_data_policy:
  allowed_for: [user_queries, evaluation_questions]
  never_for: [corpus_documents]
  default_limitations:
    - "Синтетические запросы не отражают распределение реальных вопросов пользователей"
    - "Метрики на синтетических запросах — ориентир, не финальная оценка"

task_class_taxonomy:
  - class_id: chat_with_docs
    class_name: "Диалог по корпусу документов"
    signals: ["многоходовой диалог", "учёт контекста беседы"]
    typical_inputs: ["сообщение пользователя", "история диалога"]
    typical_outputs: ["ответ", "ссылки на источники"]
  - class_id: faq_qa
    class_name: "Ответы на вопросы из FAQ"
    signals: ["одноходовой Q&A", "ограниченный набор тем"]
  - class_id: structured_extraction
    class_name: "Извлечение структурированных полей"
    signals: ["заполнение формы", "извлечение сущностей"]
  - class_id: summarization_over_corpus
    class_name: "Суммаризация по корпусу"
    signals: ["обобщение", "краткое содержание"]

architecture_patterns:
  - pattern_id: naive_rag
    name: "Naive RAG"
    description: "Базовый RAG: embedder → vector store → retriever → generator"
    applicable_to: [faq_qa, chat_with_docs]
    applicability_rules:
      - condition: "corpus_document_count < 1000"
        requirement: "quality_bar != critical"
    components: {embedder, vector_store, retriever, generator}
    complexity_level: low
    default_for_mvp: true
  - pattern_id: advanced_rag_with_reranking
    name: "Advanced RAG + reranking"
    applicable_to: [chat_with_docs, faq_qa]
    applicability_rules:
      - condition: "quality_bar == high OR corpus_document_count >= 1000"
    components: {embedder, vector_store, retriever, reranker, generator}
    complexity_level: medium
  - pattern_id: hierarchical_rag
    applicable_to: [summarization_over_corpus, chat_with_docs]
    applicability_rules:
      - condition: "corpus_document_count >= 10000 OR mixed_document_types"
  - pattern_id: agentic_rag
    applicable_to: [chat_with_docs]
    applicability_rules:
      - condition: "requires_multi_step_reasoning"

requirements_templates:
  functional:
    - id_suffix: "001"
      statement_template: "Система должна принимать запрос пользователя в текстовом формате"
      default_priority: must
      applicable_to_task_classes: "all"
    - id_suffix: "002"
      statement_template: "Система должна выполнять поиск релевантных фрагментов в корпусе"
      default_priority: must
      applicable_to_task_classes: "all"
    - id_suffix: "003"
      statement_template: "Система должна формировать ответ на основе найденных фрагментов"
      default_priority: must
    - id_suffix: "004"
      statement_template: "Система должна возвращать ссылки на использованные источники"
      default_priority: must
    - id_suffix: "005"
      statement_template: "Система должна поддерживать контекст диалога"
      default_priority: must
      applicable_to_task_classes: [chat_with_docs]
  non_functional:
    - id_suffix: "001"
      category: reliability
      statement_template: "Ответы должны быть faithful к найденным источникам (без галлюцинаций)"
      default_priority: must
    - id_suffix: "002"
      category: maintainability
      statement_template: "Все LLM-вызовы логируются для анализа и воспроизведения"
      default_priority: must

baseline_definitions:
  - for_task_class: chat_with_docs
    description_template: "Naive RAG с дефолтным embedder и без reranking"
    typical_limitations:
      - "Низкая точность на многоходовых диалогах"
      - "Отсутствие обработки устаревших документов"
    typical_metrics: [retrieval_precision, answer_faithfulness]
  - for_task_class: faq_qa
    description_template: "Naive RAG + поиск по чанкам фиксированного размера"
```

### 2.3. Domain Pack: Simple ML (сжатый фрагмент)

```yaml
domain_pack_id: simple_ml_v1
name: "Простые ML-модели"
version: "1.0.0"
status: active

classification:
  positive_signals:
    - "предсказание значения"
    - "классификация на классы"
    - "регрессия"
    - "обнаружение аномалий"
    - "целевая переменная"
  negative_signals:
    - "поиск в документах"
    - "генерация текста на основе корпуса"

applicability:
  supported_task_classes: [binary_classification, multiclass_classification, regression, anomaly_detection]

extraction_hints:
  data:
    typical_types: [tabular_db, labeled_dataset, log_stream, event_stream]
    attribute_schema:
      schema: object
      rows_count: integer
      target_variable: string | null
      labeled: bool
      class_balance: object | null

mandatory_stakeholders: [data_owner, consumer_of_predictions]

completeness_checklist:
  - field: target_variable
    priority: blocking
    question_hint: "Что именно должна предсказывать модель?"
  - field: labeled_data_availability
    priority: blocking
    question_hint: "Есть ли размеченные данные? Сколько? Кто размечал?"
  - field: prediction_consumption
    priority: blocking
    question_hint: "Как результат предсказания будет использоваться?"

sufficiency_heuristics:
  - for_task_class: binary_classification
    rules:
      - metric: min_rows_per_class
        threshold: 500
        risk_if_below: high
  - for_task_class: multiclass_classification
    rules:
      - metric: min_rows_per_class
        threshold: 200
        risk_if_below: medium
  - for_task_class: regression
    rules:
      - metric: total_rows
        threshold: 1000
        risk_if_below: medium

synthetic_data_policy:
  allowed_for: [tabular_features]
  default_limitations:
    - "Синтетика не отражает реальные распределения признаков"

task_class_taxonomy:
  - class_id: binary_classification
  - class_id: multiclass_classification
  - class_id: regression
  - class_id: anomaly_detection

architecture_patterns:
  - pattern_id: linear_baseline
    name: "Linear/Logistic Regression"
    applicable_to: [binary_classification, regression]
    complexity_level: low
    default_for_mvp: true
  - pattern_id: tree_based
    name: "Decision Tree / Random Forest"
    applicable_to: [binary_classification, multiclass_classification, regression]
    complexity_level: low
  - pattern_id: gradient_boosting
    applicable_to: [binary_classification, multiclass_classification, regression]
    applicability_rules:
      - condition: "total_rows >= 5000"
    complexity_level: medium
  - pattern_id: neural_network
    applicability_rules:
      - condition: "total_rows >= 100000"
    complexity_level: high

requirements_templates:
  functional:
    - id_suffix: "001"
      statement_template: "Система должна принимать вектор признаков указанной схемы"
      default_priority: must
    - id_suffix: "002"
      statement_template: "Система должна валидировать входные данные на соответствие схеме"
      default_priority: must
    - id_suffix: "003"
      statement_template: "Система должна возвращать предсказание указанного типа"
      default_priority: must
    - id_suffix: "004"
      statement_template: "Система должна возвращать уверенность предсказания"
      default_priority: should

baseline_definitions:
  - for_task_class: binary_classification
    description_template: "Logistic regression на сырых признаках"
    typical_metrics: [accuracy, f1, roc_auc]
  - for_task_class: regression
    description_template: "Linear regression на сырых признаках"
    typical_metrics: [mae, rmse, r2]
```

---

## Часть 3. Workflow Template: «Преобразование бизнес-запроса в ТЗ»

### 3.1. Метаданные и конфигурация

```yaml
workflow_id: biz_to_tz_v1
name: "Преобразование бизнес-запроса в ТЗ"
version: "1.0.0"
stage_gate: 1
description: "Вход: сырой бизнес-запрос. Выход: согласованное ТЗ. Работает с любым зарегистрированным Domain Pack."

applicable_domain_packs:
  # Движок принимает любой активный Domain Pack, но Workflow гарантированно протестирован с:
  - rag_v1
  - simple_ml_v1

inputs:
  - name: raw_business_request
    type: text
    required: true

outputs:
  - name: approved_technical_specification
    type: document

context:
  # Глобальные переменные Workflow, заполняются в процессе
  - name: active_domain_pack
    type: DomainPack
    set_by: $nodes.select_domain_pack
  - name: active_task_class
    type: string
    set_by: $nodes.classify_task
```

### 3.2. Фазы и узлы

Workflow структурирован как последовательность фаз. Внутри фазы узлы могут идти параллельно.

```yaml
phases:

  # ==================== Фаза A: Приём и нормализация ====================
  - phase_id: intake
    description: "Приём запроса, проверка полноты, выбор Domain Pack"
    nodes:

      - node_id: parse_request
        template: parse_free_text_to_structured
        inputs:
          source_text: $inputs.raw_business_request
          schema: $config.request_schema
        on_escalation:
          - signal: parsing_failed
            action: abort_workflow
            reason: "Входной запрос не является осмысленным текстом"

      - node_id: check_request_minimum
        template: check_minimum_completeness
        inputs:
          parsed_input: $nodes.parse_request.outputs.parsed_object
          checklist: $config.minimum_request_checklist
        on_escalation:
          - signal: incomplete
            action: request_completion_from_client
            params:
              purpose: "raw_request_incomplete"
              timeout_hours: 72
            on_return: restart_phase

      - node_id: select_domain_pack
        template: classify_against_registry
        inputs:
          object_to_classify: $nodes.parse_request.outputs.parsed_object
          registry: $config.domain_pack_registry
        outputs_binding:
          category_id: $context.active_domain_pack
        on_escalation:
          - signal: unsupported_category
            action: abort_with_escalation
            reason: "Запрос не попадает в поддерживаемые Domain Pack"
          - signal: low_confidence
            action: escalate_to_human
            reason: "Неуверенная классификация поддомена — требуется ручной выбор"

  # ==================== Фаза B: Ранний feasibility check ====================
  - phase_id: feasibility
    description: "Проверка выполнимости до погружения в детали"
    depends_on: [intake]
    nodes:

      - node_id: check_support
        template: check_registry_membership
        inputs:
          item: $context.active_domain_pack
          registry: $config.supported_domains_registry
          filter: {status: active}

      - node_id: detect_unfeasibility
        template: detect_patterns_from_catalog
        inputs:
          source_text: $inputs.raw_business_request
          pattern_catalog: $config.unfeasibility_patterns_catalog
        parallel_with: [check_support]
        on_escalation:
          - signal: blocker_detected
            action: abort_with_escalation
            reason: "Обнаружен блокирующий паттерн нерешаемости"

      - node_id: feasibility_verdict
        template: synthesize_verdict
        inputs:
          signals:
            - $nodes.check_support.outputs.membership_result
            - $nodes.detect_unfeasibility.outputs.detected_patterns
          decision_rules: $config.feasibility_rules
        on_verdict:
          - when: $outputs.verdict.decision == "ABORT"
            action: abort_with_escalation
          - when: $outputs.verdict.decision == "PROCEED_WITH_CONFIRMATION"
            action: notify_client_caveats
            blocking: false

  # ==================== Фаза C: Понимание потребности (Why-анализ) ====================
  - phase_id: need_analysis
    description: "Формирование модели потребности"
    depends_on: [feasibility]
    parallel_with: [declarative_extraction]
    nodes:

      - node_id: extract_goal
        template: extract_declared_goal
        inputs:
          source_text: $inputs.raw_business_request
          domain_pack: $context.active_domain_pack
        on_escalation:
          - signal: no_goal_found
            action: rollback_to_phase
            target_phase: intake
            reason: "Проверка полноты пропустила неполный запрос"

      - node_id: root_cause_hypotheses
        template: generate_hypotheses
        inputs:
          source_text: $inputs.raw_business_request
          context_artifacts: [$nodes.extract_goal.outputs]
          hypothesis_topic: root_cause
          domain_pack: $context.active_domain_pack
        depends_on: [extract_goal]

      - node_id: baseline_hypotheses
        template: generate_hypotheses
        inputs:
          source_text: $inputs.raw_business_request
          context_artifacts: [$nodes.extract_goal.outputs]
          hypothesis_topic: baseline_process
          domain_pack: $context.active_domain_pack
        depends_on: [extract_goal]
        parallel_with: [root_cause_hypotheses]

      - node_id: stakeholders
        template: identify_stakeholders
        inputs:
          source_text: $inputs.raw_business_request
          context_artifacts: [$nodes.extract_goal.outputs]
          domain_pack: $context.active_domain_pack
        depends_on: [extract_goal]
        parallel_with: [root_cause_hypotheses, baseline_hypotheses]

      - node_id: consolidate_need
        template: consolidate_analysis
        inputs:
          artifacts_to_consolidate:
            - $nodes.extract_goal.outputs
            - $nodes.root_cause_hypotheses.outputs
            - $nodes.baseline_hypotheses.outputs
            - $nodes.stakeholders.outputs
          consolidation_schema: $config.need_model_schema
        depends_on: [root_cause_hypotheses, baseline_hypotheses, stakeholders]

  # ==================== Фаза D: Извлечение деклараций ====================
  - phase_id: declarative_extraction
    description: "Параллельное извлечение пяти типов упоминаний"
    depends_on: [feasibility]
    parallel_with: [need_analysis]
    nodes:
      # Одна задача, пять вызовов с разными параметрами
      - node_id: extract_data
        template: extract_typed_mentions
        inputs:
          source_text: $inputs.raw_business_request
          mention_type: "data"
          domain_pack: $context.active_domain_pack

      - node_id: extract_metrics
        template: extract_typed_mentions
        inputs:
          source_text: $inputs.raw_business_request
          mention_type: "metrics"
          domain_pack: $context.active_domain_pack
        parallel_with: [extract_data]

      - node_id: extract_constraints
        template: extract_typed_mentions
        inputs:
          source_text: $inputs.raw_business_request
          mention_type: "constraints"
          domain_pack: $context.active_domain_pack
        parallel_with: [extract_data]

      - node_id: extract_acceptance
        template: extract_typed_mentions
        inputs:
          source_text: $inputs.raw_business_request
          mention_type: "acceptance"
          domain_pack: $context.active_domain_pack
        parallel_with: [extract_data]

      - node_id: extract_integrations
        template: extract_typed_mentions
        inputs:
          source_text: $inputs.raw_business_request
          mention_type: "integrations"
          domain_pack: $context.active_domain_pack
        parallel_with: [extract_data]

  # ==================== Фаза E: Gap-анализ ====================
  - phase_id: gap_analysis
    depends_on: [need_analysis, declarative_extraction]
    nodes:

      - node_id: compare_to_checklist
        template: compare_against_checklist
        inputs:
          items:
            data: $nodes.extract_data.outputs
            metrics: $nodes.extract_metrics.outputs
            constraints: $nodes.extract_constraints.outputs
            acceptance: $nodes.extract_acceptance.outputs
            integrations: $nodes.extract_integrations.outputs
          checklist: $context.active_domain_pack.completeness_checklist
          additional_inputs: [$nodes.consolidate_need.outputs.open_questions]

      - node_id: prioritize_gaps
        template: prioritize_items
        inputs:
          items: $nodes.compare_to_checklist.outputs.gap_report
          priority_rules: $config.gap_priority_rules
          context: {domain_pack: $context.active_domain_pack}
        depends_on: [compare_to_checklist]

      - node_id: structure_gaps
        template: group_and_structure_items
        inputs:
          items: $nodes.prioritize_gaps.outputs
          grouping_schema: $config.gap_grouping_schema
        depends_on: [prioritize_gaps]

  # ==================== Фаза F: Сбор уточнений ====================
  - phase_id: clarification
    depends_on: [gap_analysis]
    skip_when: $nodes.structure_gaps.outputs.total == 0
    iteration:
      max_iterations: 2
      on_max_exceeded: escalate_to_human
    nodes:

      - node_id: decide_strategy
        template: synthesize_verdict
        inputs:
          signals: [$nodes.structure_gaps.outputs.priority_counts]
          decision_rules: $config.clarification_strategy_rules
        # Правило: если blocking + important >= 3 → batch, иначе pointwise

      - node_id: build_questionnaire
        template: generate_batched_questionnaire
        inputs:
          question_specs: $nodes.structure_gaps.outputs.gaps_as_questions
          domain_pack: $context.active_domain_pack
        when: $nodes.decide_strategy.outputs.decision == "batch"

      - node_id: build_point_questions
        template: generate_point_question
        inputs:
          question_spec: $item
          domain_pack: $context.active_domain_pack
        when: $nodes.decide_strategy.outputs.decision == "pointwise"
        foreach: $nodes.structure_gaps.outputs.gaps_flat

      - node_id: request_clarifications
        template: request_user_input_via_gateway
        inputs:
          request_payload: >
            $nodes.build_questionnaire.outputs OR $nodes.build_point_questions.outputs
          purpose: "clarify_request_gaps"
        on_escalation:
          - signal: timeout_exceeded
            action: escalate_to_project_manager
          - signal: cannot_answer
            action: mark_gap_as_unresolvable
            on_blocking_gap: escalate_to_human

      - node_id: parse_clarifications
        template: parse_user_response
        inputs:
          response_raw: $nodes.request_clarifications.outputs
          response_schema: $nodes.build_questionnaire.outputs.response_schema
          original_request: $nodes.build_questionnaire.outputs OR $nodes.build_point_questions.outputs
        depends_on: [request_clarifications]

      - node_id: validate_clarifications
        template: validate_response
        inputs:
          parsed_response: $nodes.parse_clarifications.outputs
          original_questions: $nodes.build_questionnaire.outputs.questions
          prior_knowledge:
            declarations: [all extract_* outputs]
            need_model: $nodes.consolidate_need.outputs
        on_escalation:
          - signal: contradictions_detected
            action: iterate_phase
            iteration_limit: 2
          - signal: critical_unanswered
            action: escalate_to_project_manager

  # ==================== Фаза G: Работа с данными ====================
  - phase_id: data_processing
    depends_on: [clarification]
    nodes:

      - node_id: inventory_data_sources
        template: inventory_items_from_mentions
        inputs:
          mentions: $nodes.extract_data.outputs
          additional_info: $nodes.validate_clarifications.outputs.valid_answers
          object_type_schema: $config.data_source_schema
          domain_pack: $context.active_domain_pack

      - node_id: clarify_data_attributes
        template: generate_clarification_questions
        foreach:
          source: $nodes.inventory_data_sources.outputs
          where: $source.unknown_attributes is not empty
        foreach_attribute_category: [format_volume, quality_labeling, legal_access]
        inputs:
          target_object: $source
          attribute_category: $attribute_category
          domain_pack: $context.active_domain_pack
        on_questions_generated:
          # Если есть вопросы — запускаем фазу clarification снова, не на весь запрос
          action: inject_into_clarification_loop

      - node_id: evaluate_data_sufficiency
        template: evaluate_sufficiency_by_heuristics
        inputs:
          objects: $nodes.inventory_data_sources.outputs
          domain_pack: $context.active_domain_pack
          task_class_id: $context.active_task_class
        depends_on: [inventory_data_sources]

      - node_id: synthetic_decision
        template: decide_synthetic_fallback
        inputs:
          sufficiency_verdict: $nodes.evaluate_data_sufficiency.outputs
          domain_pack: $context.active_domain_pack
          task_class_id: $context.active_task_class
        when: $nodes.evaluate_data_sufficiency.outputs.sufficient == false
        on_escalation:
          - signal: needs_client_confirmation
            action: request_confirmation_via_gateway

  # ==================== Фаза H: Формализация требований ====================
  - phase_id: requirements_formalization
    depends_on: [clarification, data_processing]
    nodes:

      - node_id: generate_functional
        template: generate_requirements_from_templates
        inputs:
          requirement_kind: functional
          context_artifacts:
            - $nodes.consolidate_need.outputs
            - $nodes.validate_clarifications.outputs.valid_answers
          domain_pack: $context.active_domain_pack
          id_prefix: "FR"

      - node_id: generate_non_functional
        template: generate_requirements_from_templates
        inputs:
          requirement_kind: non_functional
          context_artifacts:
            - $nodes.extract_metrics.outputs
            - $nodes.extract_constraints.outputs
            - $nodes.validate_clarifications.outputs.valid_answers
          domain_pack: $context.active_domain_pack
          id_prefix: "NFR"
        parallel_with: [generate_functional]

      - node_id: formalize_constraints
        template: generate_constraints_from_mentions
        inputs:
          constraint_mentions: $nodes.extract_constraints.outputs
          derived_constraints_sources:
            - $nodes.synthetic_decision.outputs
            - $nodes.inventory_data_sources.outputs
          domain_pack: $context.active_domain_pack
        parallel_with: [generate_functional]

      - node_id: generate_acceptance
        template: generate_acceptance_criteria
        inputs:
          requirements:
            - $nodes.generate_functional.outputs
            - $nodes.generate_non_functional.outputs
          metric_mentions: $nodes.extract_metrics.outputs
          acceptance_mentions: $nodes.extract_acceptance.outputs
          domain_pack: $context.active_domain_pack
        depends_on: [generate_functional, generate_non_functional]

      - node_id: check_requirements_consistency
        template: detect_contradictions
        inputs:
          items:
            - $nodes.generate_functional.outputs
            - $nodes.generate_non_functional.outputs
            - $nodes.formalize_constraints.outputs
            - $nodes.generate_acceptance.outputs
          contradiction_rules: $config.requirements_consistency_rules
        depends_on: [generate_acceptance, formalize_constraints]
        on_escalation:
          - signal: critical_contradictions
            action: escalate_for_conflict_resolution
            via: clarification_phase

  # ==================== Фаза I: Архитектурный анализ ====================
  - phase_id: architecture
    depends_on: [requirements_formalization]
    nodes:

      - node_id: classify_task
        template: classify_within_taxonomy
        inputs:
          context_artifacts:
            - $nodes.consolidate_need.outputs
            - $nodes.generate_functional.outputs
          domain_pack: $context.active_domain_pack
        outputs_binding:
          class_id: $context.active_task_class

      - node_id: select_pattern
        template: select_pattern_from_catalog
        inputs:
          task_class_id: $context.active_task_class
          input_characteristics:
            data: $nodes.inventory_data_sources.outputs
            quality_requirements: $nodes.generate_non_functional.outputs
            constraints: $nodes.formalize_constraints.outputs
          domain_pack: $context.active_domain_pack
        depends_on: [classify_task]

      - node_id: arch_rationale
        template: generate_rationale
        inputs:
          chosen_option: $nodes.select_pattern.outputs
          requirements:
            - $nodes.generate_functional.outputs
            - $nodes.generate_non_functional.outputs
            - $nodes.formalize_constraints.outputs
        depends_on: [select_pattern]

      - node_id: define_project_baseline
        template: define_baseline
        inputs:
          task_class_id: $context.active_task_class
          acceptance_criteria: $nodes.generate_acceptance.outputs
          domain_pack: $context.active_domain_pack
        depends_on: [classify_task]
        parallel_with: [arch_rationale]

  # ==================== Фаза J: Сборка ТЗ ====================
  - phase_id: document_assembly
    depends_on: [architecture]
    nodes:
      # Одна задача, шесть вызовов (одна универсальная вместо A01-A06 из v1)
      - node_id: section_context
        template: generate_document_section
        inputs:
          section_spec: $config.sections.context
          input_artifacts:
            - $nodes.consolidate_need.outputs
            - $nodes.parse_request.outputs

      - node_id: section_data
        template: generate_document_section
        inputs:
          section_spec: $config.sections.data
          input_artifacts:
            - $nodes.inventory_data_sources.outputs
            - $nodes.evaluate_data_sufficiency.outputs
            - $nodes.synthetic_decision.outputs
        parallel_with: [section_context]

      - node_id: section_requirements
        template: generate_document_section
        inputs:
          section_spec: $config.sections.requirements
          input_artifacts:
            - $nodes.generate_functional.outputs
            - $nodes.generate_non_functional.outputs
            - $nodes.formalize_constraints.outputs
        parallel_with: [section_context]

      - node_id: section_architecture
        template: generate_document_section
        inputs:
          section_spec: $config.sections.architecture
          input_artifacts:
            - $nodes.select_pattern.outputs
            - $nodes.arch_rationale.outputs
            - $nodes.define_project_baseline.outputs
        parallel_with: [section_context]

      - node_id: section_acceptance
        template: generate_document_section
        inputs:
          section_spec: $config.sections.acceptance
          input_artifacts: [$nodes.generate_acceptance.outputs]
        parallel_with: [section_context]

      - node_id: section_assumptions
        template: generate_document_section
        inputs:
          section_spec: $config.sections.assumptions
          input_artifacts:
            - $nodes.formalize_constraints.outputs
            - $nodes.synthetic_decision.outputs
            - $nodes.feasibility_verdict.outputs
        parallel_with: [section_context]

      - node_id: assemble
        template: assemble_document
        inputs:
          sections:
            - $nodes.section_context.outputs
            - $nodes.section_data.outputs
            - $nodes.section_requirements.outputs
            - $nodes.section_architecture.outputs
            - $nodes.section_acceptance.outputs
            - $nodes.section_assumptions.outputs
          document_metadata: $config.tz_metadata_template
        depends_on: [section_context, section_data, section_requirements, section_architecture, section_acceptance, section_assumptions]

  # ==================== Фаза K: Валидация и согласование ====================
  - phase_id: validation_and_approval
    depends_on: [document_assembly]
    nodes:

      - node_id: check_completeness
        template: check_document_completeness
        inputs:
          document: $nodes.assemble.outputs
          completeness_checklist: $config.tz_completeness_checklist
        on_escalation:
          - signal: critical_missing
            action: rollback_to_phase
            target: $determined_by_missing_item

      - node_id: check_consistency
        template: check_internal_consistency
        inputs:
          document: $nodes.assemble.outputs
          consistency_rules: $config.tz_consistency_rules
        parallel_with: [check_completeness]
        on_escalation:
          - signal: blocker_contradictions
            action: rollback_to_phase
            target: requirements_formalization

      - node_id: check_trace
        template: check_traceability
        inputs:
          document: $nodes.assemble.outputs
          source_artifacts:
            - $inputs.raw_business_request
            - $nodes.validate_clarifications.outputs.valid_answers
          traceability_spec:
            elements_to_trace: [FR, NFR, constraints, acceptance_criteria]
            min_coverage: 0.9
        parallel_with: [check_completeness]
        on_escalation:
          - signal: coverage_below_threshold
            action: escalate_and_request_confirmations
            via: clarification_phase

      - node_id: client_approval
        template: request_approval_via_gateway
        inputs:
          document: $nodes.assemble.outputs
          cover_message: $config.tz_cover_message_template
          timeout_hours: 120
        depends_on: [check_completeness, check_consistency, check_trace]
        on_escalation:
          - signal: timeout_exceeded
            action: escalate_to_project_manager
          - signal: deep_rework_requested
            action: abort_with_escalation

      - node_id: process_comments
        template: classify_comments
        inputs:
          comments_raw: $nodes.client_approval.outputs.comments_raw
          document: $nodes.assemble.outputs
          task_mapping: $config.comment_to_node_mapping
        when: $nodes.client_approval.outputs.decision == "approved_with_comments"
        on_outputs:
          # Использует Bubble Up: помечает triggered_workflow_nodes как Obsolete и перезапускает
          action: rerun_nodes
          nodes: $outputs.classified_comments[*].triggered_workflow_nodes
          then: rerun_phase
          target: validation_and_approval

      - node_id: finalize
        template: finalize_artifact
        inputs:
          artifact_content: $nodes.assemble.outputs
          approval_decision: $nodes.client_approval.outputs
          finalization_spec: $config.tz_finalization_spec
        when: $nodes.client_approval.outputs.decision == "approved"
        outputs_binding:
          finalized_artifact: $outputs.approved_technical_specification
```

### 3.3. Политика реакции на escalation signals

Workflow централизует обработку сигналов. Это даёт возможность менять политику (строгая / мягкая / с дополнительными проверками) без изменения задач.

| Действие | Что делает |
|----------|-----------|
| `abort_workflow` | Завершает Workflow с ошибкой, сообщает Stage-Gate Manager |
| `abort_with_escalation` | То же + Interruption Gateway уведомляет человека с контекстом |
| `escalate_to_human` | Останавливает текущую фазу, ждёт решения человека, после — возобновляет |
| `escalate_to_project_manager` | То же, но адресат — PM (для бизнес-решений) |
| `rollback_to_phase` | Помечает артефакты последующих фаз как Obsolete, запускает фазу заново (штатный Bubble Up) |
| `request_completion_from_client` | Interruption Gateway → заказчик с конкретным запросом |
| `notify_client_caveats` | Неблокирующее уведомление заказчику |
| `iterate_phase` | Запускает фазу повторно (с учётом лимита итераций) |
| `rerun_nodes` | Помечает конкретные узлы Obsolete и перезапускает их с зависимостями (для обработки комментариев) |
| `mark_gap_as_unresolvable` | Записывает пробел как нерешаемый в gap_list, продолжает работу |

---

## Часть 4. Артефакты и трассируемость

### 4.1. Модель артефактов

В модульной архитектуре артефакты делятся на три класса.

**Универсальные артефакты** — типы данных, производимые Task Templates. Не имеют жёсткой привязки к Workflow. Пример: `typed_mentions`, `hypotheses`, `requirements`.

**Artefact bindings Workflow** — конкретные экземпляры артефактов в рамках этого Workflow, адресуемые через `$nodes.<node_id>.outputs.<field>`. Пример: `$nodes.extract_data.outputs.typed_mentions` и `$nodes.extract_metrics.outputs.typed_mentions` — два экземпляра одного типа.

**Внешние артефакты** — Domain Pack'и, конфигурации, реестры, библиотеки промпт-шаблонов. Хранятся в Template Registry.

### 4.2. Маппинг: артефакт v1 → источник в v2

Это справочная таблица для понимания, как сущности из прежней версии выражаются в новой архитектуре.

| Артефакт v1 | Источник в v2 |
|---|---|
| `parsed_request` | `$nodes.parse_request.outputs.parsed_object` |
| `subdomain_classification` | `$nodes.select_domain_pack.outputs` → связан с `$context.active_domain_pack` |
| `feasibility_verdict` | `$nodes.feasibility_verdict.outputs` |
| `declared_goal` | `$nodes.extract_goal.outputs` |
| `root_cause_hypotheses` | `$nodes.root_cause_hypotheses.outputs` |
| `baseline_hypotheses` | `$nodes.baseline_hypotheses.outputs` |
| `stakeholders_map` | `$nodes.stakeholders.outputs` |
| `need_model` | `$nodes.consolidate_need.outputs` |
| `data_mentions` | `$nodes.extract_data.outputs` |
| `metric_mentions` | `$nodes.extract_metrics.outputs` |
| `constraint_mentions` | `$nodes.extract_constraints.outputs` |
| `acceptance_mentions` | `$nodes.extract_acceptance.outputs` |
| `integration_mentions` | `$nodes.extract_integrations.outputs` |
| `gap_list` | `$nodes.structure_gaps.outputs` |
| `clarifications` | `$nodes.validate_clarifications.outputs` |
| `data_sources_inventory` | `$nodes.inventory_data_sources.outputs` |
| `data_sufficiency_verdict` | `$nodes.evaluate_data_sufficiency.outputs` |
| `synthetic_data_decision` | `$nodes.synthetic_decision.outputs` |
| `functional_requirements` | `$nodes.generate_functional.outputs` |
| `non_functional_requirements` | `$nodes.generate_non_functional.outputs` |
| `project_constraints` | `$nodes.formalize_constraints.outputs` |
| `acceptance_criteria` | `$nodes.generate_acceptance.outputs` |
| `architectural_approach` | композиция: `$nodes.classify_task` + `select_pattern` + `arch_rationale` + `define_project_baseline` |
| `section_*` | `$nodes.section_*.outputs` (шесть вызовов одной задачи) |
| `draft_specification_document` | `$nodes.assemble.outputs` |
| `approved_technical_specification` | `$outputs.approved_technical_specification` |

### 4.3. Внешние артефакты (Template Registry)

| Артефакт | Тип | Потребляется |
|---|---|---|
| Domain Packs (`rag_v1`, `simple_ml_v1`, ...) | Domain Pack | Все задачи с параметром `domain_pack` |
| `request_schema` | JSON Schema | parse_request |
| `minimum_request_checklist` | Checklist | check_request_minimum |
| `domain_pack_registry` | Registry | select_domain_pack |
| `supported_domains_registry` | Registry | check_support |
| `unfeasibility_patterns_catalog` | Pattern Catalog | detect_unfeasibility |
| `feasibility_rules` | Decision Rules | feasibility_verdict |
| `need_model_schema` | Schema | consolidate_need |
| `gap_priority_rules` | Priority Rules | prioritize_gaps |
| `gap_grouping_schema` | Schema | structure_gaps |
| `clarification_strategy_rules` | Decision Rules | decide_strategy |
| `data_source_schema` | Object Schema | inventory_data_sources |
| `requirements_consistency_rules` | Contradiction Rules | check_requirements_consistency |
| `tz_sections_config` | Section Specs | все section_* узлы |
| `tz_metadata_template` | Template | assemble |
| `tz_completeness_checklist` | Checklist | check_completeness |
| `tz_consistency_rules` | Rules | check_consistency |
| `comment_to_node_mapping` | Mapping | process_comments |
| `tz_finalization_spec` | Spec | finalize |

---

## Часть 5. Сверка с принципами и покрытием

### 5.1. Покрытие восьми зон из исходного ТЗ

| Зона | Задачи (Task Templates) | Узлы Workflow |
|---|---|---|
| 1. Приём и первичная обработка | parse_free_text_to_structured, check_minimum_completeness, classify_against_registry | parse_request, check_request_minimum, select_domain_pack |
| 2. Понимание потребности | extract_declared_goal, generate_hypotheses (×2), identify_stakeholders, consolidate_analysis | фаза need_analysis целиком |
| 3. Сбор недостающей информации | compare_against_checklist, prioritize_items, group_and_structure_items, generate_batched_questionnaire, generate_point_question, request_user_input_via_gateway, parse_user_response, validate_response | фазы gap_analysis и clarification |
| 4. Работа с данными | extract_typed_mentions, inventory_items_from_mentions, generate_clarification_questions, evaluate_sufficiency_by_heuristics, decide_synthetic_fallback | фаза data_processing + extract_data из declarative_extraction |
| 5. Определение требований | extract_typed_mentions, generate_requirements_from_templates, generate_constraints_from_mentions, generate_acceptance_criteria, detect_contradictions | фаза requirements_formalization + остальные extract_* |
| 6. Архитектурный анализ | classify_within_taxonomy, select_pattern_from_catalog, generate_rationale, define_baseline | фаза architecture |
| 7. Формирование документа | generate_document_section, assemble_document | фаза document_assembly |
| 8. Валидация ТЗ | check_document_completeness, check_internal_consistency, check_traceability, request_approval_via_gateway, classify_comments, finalize_artifact | фаза validation_and_approval |

Дополнительно — ранняя feasibility-проверка реализуется через `detect_patterns_from_catalog` + `check_registry_membership` + `synthesize_verdict` в фазе feasibility.

### 5.2. Соответствие комментарию заказчика

| Критика | Решение в v2 |
|---|---|
| «Жёсткий граф на уровне задач, сложно дебажить» | Граф перенесён в отдельный слой (Workflow). Задачи не знают друг о друге и о месте в пайплайне. |
| «Задачи не переиспользуемы» | Task Templates универсальны. `extract_typed_mentions` используется 5 раз с разными параметрами, `generate_document_section` — 6 раз, `generate_hypotheses` — 2 раза. Любая задача может быть вызвана в другом Workflow. |
| «Доменные знания вшиты в задачи» | Всё доменное знание вынесено в Domain Pack. Задачи параметризуются паком. Добавление поддомена = новый Domain Pack без правки задач и Workflow. |
| «Структура слишком жёстко задана» | Вместо жёсткой иерархии 11 блоков — 11 фаз с явными зависимостями и параллелизмом. Внутри фазы узлы можно менять без каскадных правок. |

### 5.3. Соответствие принципам PoV.md

| Принцип | Как отражён |
|---------|-------------|
| Фокус на потребностях | Фаза `need_analysis` обязательна до любых требований. Фаза `clarification` снимает blocking-пробелы до архитектуры. |
| Прозрачность | Трассировка через `check_traceability` с порогом 0.9. Каждое извлечение упоминаний обязано содержать цитату. |
| Масштабируемость | Добавление поддомена = Domain Pack (не трогает задачи). Добавление гейта = новый Workflow (переиспользует существующие задачи). Добавление новой операции в одной фазе = новый Task Template + одна строка в Workflow. |
| Воспроизводимость | Требование воспроизводимости встроено в `requirements_templates.non_functional` каждого Domain Pack. Декомпозиция на атомарные LLM-вызовы снижает вариативность. |
| Самоконтроль | Фаза `validation_and_approval` автономна (три параллельные проверки). Политика эскалаций централизована в Workflow — задачи только сигнализируют, не решают, что делать. |

### 5.4. Что даёт эта архитектура дополнительно

**Независимое тестирование.** Каждый Task Template можно тестировать в изоляции с моковыми входами. Workflow — отдельно с моковыми задачами.

**Версионирование.** Task Template, Domain Pack и Workflow версионируются независимо. Можно обновить `rag_v1` → `rag_v2` без перевыпуска Workflow — Workflow продолжает работать с новой версией пака, пока совместима схема.

**A/B на уровне паттернов.** Можно выпустить `rag_v1` и `rag_v1_experimental` и направлять запросы в разные паки.

**Переиспользование в других гейтах.** `generate_document_section`, `check_traceability`, `request_approval_via_gateway` явно нужны в Stage Gate 2 (Архитектура) и Stage Gate 3 (Исходный код). Те же задачи, другой Workflow.

**Понятность.** Workflow читается сверху вниз как сценарий: фаза → что делаем → какие задачи → как реагируем на сбой. Логику не нужно восстанавливать из разрозненных YAML-шаблонов задач.

