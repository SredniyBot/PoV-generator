# Пример: Stage Gate 1 на реальном запросе

**Кейс:** Предиктивная аналитика текучести кадров в розничном подразделении.

> Исходный бизнес-запрос:
> «Компания планирует PoC по внедрению предиктивной аналитики для снижения текучести кадров в
> розничном подразделении. Текущий уровень оттока составляет около 40% в год, цель — сократить
> его на 7 п.п. к 2025 году. Построить ML-модель, которая будет еженедельно обновляться и
> прогнозировать вероятность увольнения сотрудников. Входные данные: персональные данные
> сотрудников, показатели выполнения планов продаж, активность и комментарии на корпоративном
> портале. Источники: 1С ЗУП и корпоративный портал через API. Развертывание: on-premise или
> защищённое облако, шифрование/обезличивание ПД, 2FA + ADFS. Допускаются внешние LLM (OpenAI,
> GigaChat). Интерфейс: веб-приложение с BI-дашбордом, AI-выводами, рекомендациями для HR.
> Заказчики: HR C&B + ДИТ. Первые результаты — 1–2 месяца, пилот 5–7 человек Q1–Q2.»

Ниже — пошаговый разбор всех фаз Workflow [`biz_to_tz_v1`](workflow/biz_to_tz_v1.yaml).
Для каждой фазы показаны: какая задача вызывается, что на входе, что на выходе.

---

## Фаза A: Intake — приём и нормализация

**Файлы:** [`workflow/biz_to_tz_v1.yaml`](workflow/biz_to_tz_v1.yaml) § intake,
[`task_templates/01_text_processing.yaml`](task_templates/01_text_processing.yaml)

### Узел `parse_request` → `parse_free_text_to_structured`

**Вход:** сырой текст запроса + `$config.request_schema`

**Выход `parsed_object`:**
```json
{
  "goal": "Снизить текучесть кадров через предиктивную ML-модель",
  "domain_hint": "предсказание вероятности увольнения",
  "target_metric": "текучесть -7 п.п. к 2025",
  "data_sources": ["1С ЗУП", "корпоративный портал"],
  "deployment": "on-premise / защищённое облако",
  "stakeholders": ["HR C&B", "ДИТ"],
  "timeline": "1-2 месяца первые результаты, пилот Q1-Q2",
  "constraints": ["ПД", "2FA + ADFS", "шифрование"],
  "integrations": ["1С ЗУП API", "корпоративный портал API", "OpenAI", "GigaChat"]
}
```

Сигналов эскалации нет — текст осмыслен, парсинг успешен.

---

### Узел `check_request_minimum` → `check_minimum_completeness`

**Вход:** `parsed_object` + минимальный чеклист (наличие цели, хотя бы одного источника данных, заказчика)

**Выход `completeness_verdict`:**
```json
{
  "is_complete": true,
  "missing": [],
  "reasoning": "Цель сформулирована, источники данных указаны (1С ЗУП, портал), заказчики названы (HR C&B, ДИТ)"
}
```

---

### Узел `select_domain_pack` → `classify_against_registry`

**Вход:** `parsed_object` + реестр Domain Pack'ов

Задача сопоставляет сигналы запроса с `classification.positive_signals` каждого пака.

| Domain Pack | Совпадающие сигналы | Исключающие сигналы |
|---|---|---|
| `rag_v1` | — | «предсказание значения», «целевая переменная» |
| `simple_ml_v1` | «предсказание вероятности», «целевая переменная», «классификация» | — |

**Выход `classification_result`:**
```json
{
  "category_id": "simple_ml_v1",
  "confidence": 0.91,
  "reasoning": "Явная целевая переменная (факт увольнения), бинарная классификация, табличные данные из 1С",
  "alternatives": []
}
```

`$context.active_domain_pack` = **`simple_ml_v1`** — используется во всех последующих фазах.

---

## Фаза B: Feasibility — ранняя проверка выполнимости

**Файлы:** [`workflow/biz_to_tz_v1.yaml`](workflow/biz_to_tz_v1.yaml) § feasibility,
[`task_templates/01_text_processing.yaml`](task_templates/01_text_processing.yaml),
[`workflow/escalation_policy.md`](workflow/escalation_policy.md)

### Узлы `check_support` ∥ `detect_unfeasibility` (параллельно)

**`check_support` → `check_registry_membership`**

Проверяет: есть ли `simple_ml_v1` в реестре поддерживаемых доменов со статусом `active`.

```json
{ "is_member": true, "matched_record": { "id": "simple_ml_v1", "status": "active" } }
```

**`detect_unfeasibility` → `detect_patterns_from_catalog`**

Сканирует запрос по каталогу паттернов нерешаемости. Обнаружены паттерны:

```json
[
  {
    "pattern_id": "personal_data_processing",
    "evidence_quote": "персональные данные сотрудников",
    "severity": "warning"
  },
  {
    "pattern_id": "external_llm_with_pd",
    "evidence_quote": "активность и комментарии... допускаются внешние LLM (OpenAI, GigaChat)",
    "severity": "warning"
  }
]
```

Severity = `warning`, не `blocker` → сигнал `blocker_detected` не срабатывает.

---

### Узел `feasibility_verdict` → `synthesize_verdict`

**Входные сигналы:** `is_member=true` + два `warning`-паттерна

**Выход `verdict`:**
```json
{
  "decision": "PROCEED_WITH_CONFIRMATION",
  "rationale": "Поддомен поддерживается. Два предупреждения: обработка ПД требует DPA/согласования с DPO; использование внешних LLM с ПД требует либо обезличивания, либо запрета на передачу.",
  "applied_rules": ["rule_pd_warning", "rule_external_llm_pd"]
}
```

Действие по политике [`escalation_policy.md`](workflow/escalation_policy.md): `notify_client_caveats` — неблокирующее уведомление заказчику. Workflow продолжается.

---

## Фазы C ∥ D: Need Analysis и Declarative Extraction (параллельно)

**Файлы:** [`task_templates/02_meaning_analysis.yaml`](task_templates/02_meaning_analysis.yaml),
[`task_templates/03_typed_mentions.yaml`](task_templates/03_typed_mentions.yaml),
[`domain_packs/simple_ml_v1.yaml`](domain_packs/simple_ml_v1.yaml)

---

### Фаза C: Need Analysis

#### Узел `extract_goal` → `extract_declared_goal`

**Выход `declared_goal`:**
```json
{
  "goal_statement": "Еженедельно прогнозировать вероятность увольнения каждого сотрудника розничного подразделения для принятия превентивных HR-решений.",
  "direct_quotes": [
    "прогнозировать вероятность увольнения сотрудников",
    "еженедельно обновляться",
    "снизить текучесть кадров... на 7 п.п."
  ]
}
```

---

#### Узлы `root_cause_hypotheses` ∥ `baseline_hypotheses` ∥ `stakeholders` (параллельно)

**`root_cause_hypotheses` → `generate_hypotheses`** (hypothesis_topic = `root_cause`)

```json
[
  {
    "hypothesis": "Высокая текучесть вызвана перегрузкой сотрудников при низком уровне признания",
    "supporting_signals": ["40% оттока в год — аномально высоко для ритейла"],
    "plausibility": 0.7,
    "verification_question": "Есть ли данные о переработках и результатах опросов удовлетворённости?"
  },
  {
    "hypothesis": "Отток концентрирован в определённых торговых точках или регионах",
    "supporting_signals": ["розничное подразделение — неоднородная структура"],
    "plausibility": 0.65,
    "verification_question": "Есть ли разбивка текучести по магазинам/регионам в 1С ЗУП?"
  },
  {
    "hypothesis": "Прогностическая модель не будет достаточно объяснима для HR без AI-интерпретации",
    "supporting_signals": ["упоминание AI-выводов и рекомендаций в интерфейсе"],
    "plausibility": 0.6,
    "verification_question": "Какой уровень объяснимости предсказаний ожидает HR-аналитик?"
  }
]
```

**`baseline_hypotheses` → `generate_hypotheses`** (hypothesis_topic = `baseline_process`, domain_pack = `simple_ml_v1`)

Domain Pack предоставляет шаблоны baseline для `binary_classification`:
```json
[
  {
    "hypothesis": "Текущий baseline — ручное отслеживание по KPI выполнения плана продаж HR-менеджером",
    "plausibility": 0.8,
    "verification_question": "Как сейчас идентифицируют 'риск-сотрудников'? Есть ли таблицы наблюдений?"
  },
  {
    "hypothesis": "Существующий baseline — правило: 'уволится, если выполнение плана < 70% два месяца подряд'",
    "plausibility": 0.55,
    "verification_question": "Есть ли задокументированные HR-правила для выявления потенциальных увольнений?"
  }
]
```

**`stakeholders` → `identify_stakeholders`**

Domain Pack `simple_ml_v1` задаёт обязательных стейкхолдеров: `data_owner`, `consumer_of_predictions`.

```json
[
  { "role": "HR C&B (заказчик)", "interest": "Снизить текучесть, получить инструмент раннего реагирования", "explicit_in_source": true },
  { "role": "ДИТ (заказчик)", "interest": "Техническая реализация, безопасность, интеграции", "explicit_in_source": true },
  { "role": "Сотрудники розницы", "interest": "Субъекты ПД — интерес к защите данных", "explicit_in_source": false },
  { "role": "DPO (офицер по защите данных)", "interest": "Правомерность обработки ПД, согласия", "explicit_in_source": false },
  { "role": "Руководители торговых точек", "interest": "Потребители рекомендаций от HR", "explicit_in_source": false }
]
```

`explicit_in_source=false` для DPO и руководителей → сигнал для gap-анализа.

---

#### Узел `consolidate_need` → `consolidate_analysis`

**Выход `consolidated_model`:**
```json
{
  "core_need": "Превентивное управление текучестью через еженедельный ML-скоринг сотрудников",
  "business_problem": "40% отток в год ведёт к прямым затратам на найм и потере экспертизы",
  "success_criterion": "Снижение оттока на 7 п.п. измеримо через 12 месяцев после внедрения",
  "internal_contradictions": [
    "Допускаются внешние LLM (OpenAI) + обязательная защита ПД — противоречие требует разрешения",
    "Срок 1-2 месяца + еженедельное переобучение — неясно, кто обслуживает модель после пилота"
  ],
  "open_questions": [
    "Каков правовой статус обработки ПД сотрудников для ML? Есть ли согласие DPO?",
    "Если OpenAI/GigaChat — данные передаются обезличенными или нет?",
    "Кто ответственен за MLOps (переобучение еженедельно) после завершения PoC?",
    "Есть ли исторические данные об уволившихся с метками (факт увольнения) за 2+ года?",
    "Как будут использоваться рекомендации — только информирование или блокировка действий?"
  ]
}
```

---

### Фаза D: Declarative Extraction (5 параллельных вызовов `extract_typed_mentions`)

**Задача:** [`task_templates/03_typed_mentions.yaml`](task_templates/03_typed_mentions.yaml)
**Контекст из Domain Pack:** `simple_ml_v1.extraction_hints`

#### `extract_data` (mention_type = `data`)

```json
[
  {
    "mention": "Персональные данные сотрудников",
    "quote": "персональные данные сотрудников",
    "extracted_attributes": { "type": "labeled_dataset", "labeled": null, "rows_count": null, "target_variable": "факт увольнения" }
  },
  {
    "mention": "Показатели выполнения планов продаж",
    "quote": "показатели выполнения планов продаж",
    "extracted_attributes": { "type": "tabular_db", "source": "1С ЗУП", "update_frequency": "периодически" }
  },
  {
    "mention": "Активность и комментарии на корпоративном портале",
    "quote": "активность и комментарии на корпоративном портале",
    "extracted_attributes": { "type": "log_stream", "labeled": null }
  }
]
```

`labeled: null` для двух источников → явный пробел для gap-анализа.

#### `extract_metrics` (mention_type = `metrics`)

```json
[
  { "mention": "Текущий уровень оттока 40% в год", "quote": "текущий уровень оттока составляет около 40% в год", "extracted_attributes": { "metric": "turnover_rate", "current_value": 0.4 } },
  { "mention": "Цель: снижение на 7 п.п.", "quote": "сократить его на 7 процентных пунктов к 2025 году", "extracted_attributes": { "metric": "turnover_reduction", "target_delta": -0.07, "deadline": "2025" } }
]
```

Специфичные для ML метрики (accuracy, F1, ROC-AUC) не упомянуты — пробел.

#### `extract_constraints` (mention_type = `constraints`)

```json
[
  { "mention": "On-premise или защищённое облако", "quote": "развертывание предпочтительно on-premise или в защищённом облаке", "extracted_attributes": { "type": "deployment", "is_hard": null } },
  { "mention": "Шифрование/обезличивание данных", "quote": "с шифрованием/обезличиванием данных", "extracted_attributes": { "type": "security" } },
  { "mention": "2FA + ADFS", "quote": "авторизация через 2FA + ADFS", "extracted_attributes": { "type": "auth", "is_hard": true } },
  { "mention": "Защита персональных данных", "quote": "персональные данные сотрудников", "extracted_attributes": { "type": "compliance", "regulation": null } }
]
```

#### `extract_acceptance` (mention_type = `acceptance`)

```json
[
  { "mention": "Первые результаты через 1-2 месяца", "quote": "первые результаты ожидаются через 1–2 месяца", "extracted_attributes": { "type": "timeline" } },
  { "mention": "Пилот Q1-Q2 силами 5-7 человек", "quote": "пилотное тестирование — силами 5–7 человек в период Q1–Q2", "extracted_attributes": { "type": "pilot", "team_size": "5-7", "period": "Q1-Q2" } }
]
```

Формальных метрических критериев приёмки (порог F1, точность прогноза) — нет.

#### `extract_integrations` (mention_type = `integrations`)

```json
[
  { "mention": "1С ЗУП API", "quote": "данные будут поступать из системы 1С ЗУП... через API", "extracted_attributes": { "system": "1С ЗУП", "method": "API" } },
  { "mention": "Корпоративный портал API", "quote": "корпоративного портала через API", "extracted_attributes": { "system": "корпоративный портал", "method": "API" } },
  { "mention": "OpenAI / GigaChat", "quote": "допускается использование внешних LLM (OpenAI, GigaChat и др.)", "extracted_attributes": { "type": "external_llm", "providers": ["OpenAI", "GigaChat"] } },
  { "mention": "Интеграция с 1С ЗУП из интерфейса", "quote": "возможна интеграция с 1С ЗУП", "extracted_attributes": { "direction": "bidirectional", "system": "1С ЗУП" } }
]
```

---

## Фаза E: Gap Analysis

**Файлы:** [`task_templates/04_gap_analysis.yaml`](task_templates/04_gap_analysis.yaml),
[`domain_packs/simple_ml_v1.yaml`](domain_packs/simple_ml_v1.yaml) § completeness_checklist

### Узел `compare_to_checklist` → `compare_against_checklist`

Сверяем извлечённые декларации с `simple_ml_v1.completeness_checklist`:

| Поле чеклиста | Приоритет | Есть в запросе? | Вывод |
|---|---|---|---|
| `target_variable` | blocking | ✅ «вероятность увольнения» | OK |
| `labeled_data_availability` | blocking | ❌ Не указано | **GAP** |
| `prediction_consumption` | blocking | ✅ «дашборд, рекомендации для HR» | OK |

Дополнительные пробелы из `consolidate_need.open_questions`:

| Поле | Приоритет | Источник |
|---|---|---|
| Правовой статус ПД / согласие DPO | blocking | open_questions |
| Обезличивание при передаче в LLM | blocking | open_questions |
| MLOps после PoC | important | open_questions |
| Метрики качества модели (F1, ROC-AUC) | important | extract_metrics (пусто) |
| Объяснимость предсказаний | important | root_cause_hypotheses |

---

### Узел `prioritize_gaps` → `prioritize_items`

```json
[
  { "item": "labeled_data_availability", "priority": "blocking", "reasoning": "Без меток (факт увольнения) модель обучить невозможно — блокирует весь проект" },
  { "item": "pd_legal_status", "priority": "blocking", "reasoning": "ПД сотрудников — чувствительная категория. Без DPO-согласования проект может быть остановлен регулятором" },
  { "item": "llm_pd_anonymization", "priority": "blocking", "reasoning": "Передача ПД в OpenAI без обезличивания нарушает 152-ФЗ и GDPR (если применимо)" },
  { "item": "model_quality_metrics", "priority": "important", "reasoning": "Без метрик приёмки (F1, ROC-AUC) непонятно, когда считать модель готовой" },
  { "item": "mlops_ownership", "priority": "important", "reasoning": "Еженедельное переобучение требует DevOps/MLOps ресурса после PoC" },
  { "item": "explainability_requirements", "priority": "important", "reasoning": "HR-аналитики должны понимать, почему модель поставила высокий риск конкретному сотруднику" }
]
```

---

### Узел `structure_gaps` → `group_and_structure_items`

```json
{
  "total": 6,
  "priority_counts": { "blocking": 3, "important": 3, "nice_to_have": 0 },
  "groups": {
    "data": ["labeled_data_availability"],
    "compliance": ["pd_legal_status", "llm_pd_anonymization"],
    "requirements": ["model_quality_metrics", "explainability_requirements"],
    "operations": ["mlops_ownership"]
  }
}
```

`blocking + important = 6 >= 3` → стратегия сбора уточнений: **batch** (один пакетный опросник).

---

## Фаза F: Clarification — сбор уточнений

**Файлы:** [`task_templates/05_user_interaction.yaml`](task_templates/05_user_interaction.yaml),
[`domain_packs/simple_ml_v1.yaml`](domain_packs/simple_ml_v1.yaml) § clarification_templates

### Узел `build_questionnaire` → `generate_batched_questionnaire`

Domain Pack предоставляет шаблоны из `simple_ml_v1.clarification_templates`.
Задача формирует человекочитаемый опросник:

---

**Опросник для HR C&B / ДИТ**

_Блок 1: Данные и разметка_ [BLOCKING]

1. **Исторические данные об увольнениях.** Есть ли в 1С ЗУП история увольнений за последние 2–3 года с датами и причинами? Это ключевое условие для обучения модели.
   _Ожидаемый формат: да/нет + приблизительное количество записей_

2. **Размеченность данных.** Можно ли автоматически получить метку «уволился / остался» для каждого сотрудника за прошлые периоды? Кто в компании владеет этими данными?
   _Ожидаемый формат: описание процесса получения меток_

_Блок 2: Персональные данные и комплаенс_ [BLOCKING]

3. **Согласование с DPO.** Был ли привлечён офицер по защите данных (DPO) для оценки проекта? Какие ограничения на использование ПД сотрудников для ML уже известны?
   _Ожидаемый формат: да/нет + контакт DPO или известные ограничения_

4. **Внешние LLM и ПД.** Планируется ли передача данных с именами/идентификаторами сотрудников во внешние LLM, или только обезличенных признаков (числа, категории)?
   _Ожидаемый формат: описание сценария использования LLM_

_Блок 3: Требования к модели_ [IMPORTANT]

5. **Метрика успеха модели.** Какой минимально приемлемый уровень точности прогнозирования ожидается? Например: «модель должна правильно идентифицировать 70% увольняющихся за 4 недели до события».
   _Ожидаемый формат: порог метрики (Precision/Recall/F1) или описание на бизнес-языке_

6. **Объяснимость.** Должна ли система объяснять HR, почему сотрудник попал в группу риска? Или достаточно скорингового балла?
   _Ожидаемый формат: да/нет + примеры ожидаемых объяснений_

---

### Узел `request_clarifications` → `request_user_input_via_gateway`

Опросник уходит заказчику. `timeout_hours = 48`. Workflow ждёт события `response_received`.

---

### Узлы `parse_clarifications` + `validate_clarifications`

**Предположим, пришёл ответ заказчика:**

> «1. Да, данные за 3 года есть. Около 2800 уволившихся и ~5000 оставшихся. 2. Метки можно получить автоматически из 1С. 3. DPO пока не привлекался, планируем на следующей неделе. 4. LLM используется только для генерации текстовых рекомендаций HR по обезличенным признакам — имена и ID не передаются. 5. Хотим ловить 60-65% реальных увольнений при приемлемом числе ложных тревог. 6. Да, объяснения нужны — "топ-3 фактора риска" для каждого сотрудника.»

**`parse_clarifications` → `parse_user_response`**

```json
{
  "labeled_data_availability": {
    "exists": true,
    "period_years": 3,
    "churned_count": 2800,
    "retained_count": 5000,
    "auto_label": true,
    "source": "1С ЗУП"
  },
  "pd_legal_status": {
    "dpo_engaged": false,
    "planned": "следующая неделя"
  },
  "llm_pd_anonymization": {
    "anonymized": true,
    "llm_use_case": "генерация текстовых рекомендаций по обезличенным признакам"
  },
  "model_quality_metrics": {
    "target_recall": 0.60,
    "constraint": "приемлемое число ложных тревог"
  },
  "explainability_requirements": {
    "required": true,
    "format": "топ-3 фактора риска"
  },
  "not_answered": ["mlops_ownership"]
}
```

**`validate_clarifications` → `validate_response`**

Противоречий с ранее известным нет. `critical_unanswered` = [`mlops_ownership`] — вопрос имел приоритет `important`, не `blocking`, поэтому Workflow продолжается (пробел фиксируется в gap_list как нерешаемый на данном этапе).

---

## Фаза G: Data Processing — работа с данными

**Файлы:** [`task_templates/06_inventory.yaml`](task_templates/06_inventory.yaml),
[`domain_packs/simple_ml_v1.yaml`](domain_packs/simple_ml_v1.yaml) § sufficiency_heuristics, synthetic_data_policy

### Узел `inventory_data_sources` → `inventory_items_from_mentions`

```json
[
  {
    "id": "ds_001",
    "name": "1С ЗУП — кадровые данные",
    "type": "labeled_dataset",
    "known_attributes": {
      "rows_count": 7800,
      "target_variable": "факт_увольнения",
      "labeled": true,
      "period_years": 3,
      "access_method": "API"
    },
    "unknown_attributes": ["schema", "class_balance", "feature_list"]
  },
  {
    "id": "ds_002",
    "name": "Корпоративный портал — активность и комментарии",
    "type": "log_stream",
    "known_attributes": { "access_method": "API" },
    "unknown_attributes": ["rows_count", "schema", "labeled", "update_frequency", "text_language"]
  },
  {
    "id": "ds_003",
    "name": "1С ЗУП — планы продаж",
    "type": "tabular_db",
    "known_attributes": { "source": "1С ЗУП", "update_frequency": "периодически" },
    "unknown_attributes": ["schema", "granularity", "history_depth"]
  }
]
```

`ds_002` и `ds_003` имеют незаполненные атрибуты → генерируются уточняющие вопросы и инжектируются в clarification loop (если будет вторая итерация).

---

### Узел `evaluate_data_sufficiency` → `evaluate_sufficiency_by_heuristics`

Применяем `simple_ml_v1.sufficiency_heuristics` для `binary_classification`:

| Метрика | Порог | Факт | Риск |
|---|---|---|---|
| `min_rows_per_class` | 500 | min(2800, 5000) = 2800 | ✅ low |

**Выход `sufficiency_verdict`:**
```json
{
  "sufficient": true,
  "gaps": ["class_balance неизвестен — соотношение 2800:5000 (36:64%) приемлемо, но нужна валидация"],
  "risk_level": "low"
}
```

`sufficient=true` → узел `synthetic_decision` пропускается (`when: sufficient == false`).

---

## Фаза H: Requirements Formalization

**Файлы:** [`task_templates/07_requirements.yaml`](task_templates/07_requirements.yaml),
[`domain_packs/simple_ml_v1.yaml`](domain_packs/simple_ml_v1.yaml) § requirements_templates

### Узел `generate_functional` → `generate_requirements_from_templates` (id_prefix = `FR`)

Базовый набор из `simple_ml_v1.requirements_templates.functional` + контекст проекта:

| ID | Требование | Приоритет | Источник |
|---|---|---|---|
| FR-001 | Система должна принимать вектор признаков сотрудника указанной схемы | must | шаблон simple_ml_v1 |
| FR-002 | Система должна валидировать входные данные на соответствие схеме признаков | must | шаблон simple_ml_v1 |
| FR-003 | Система должна возвращать вероятность увольнения (float 0–1) для каждого сотрудника | must | шаблон + цель |
| FR-004 | Система должна возвращать топ-3 фактора риска для каждого предсказания | must | validate_clarifications |
| FR-005 | Система должна автоматически переобучать модель еженедельно на новых данных из 1С ЗУП | must | declared_goal |
| FR-006 | Система должна предоставлять веб-интерфейс с BI-дашбордом и AI-рекомендациями для HR | must | parsed_request |
| FR-007 | Система должна поддерживать авторизацию через 2FA + ADFS | must | extract_constraints |
| FR-008 | Система должна хранить и обрабатывать ПД только в обезличенном виде при взаимодействии с внешними LLM | must | validate_clarifications |

---

### Узел `generate_non_functional` → `generate_requirements_from_templates` (id_prefix = `NFR`)

| ID | Категория | Требование | Приоритет |
|---|---|---|---|
| NFR-001 | performance | Скоринг всей базы сотрудников должен выполняться не дольше 10 минут | should |
| NFR-002 | reliability | Recall модели на исторических данных ≥ 0.60 при Precision ≥ 0.50 | must |
| NFR-003 | security | Персональные данные сотрудников шифруются at-rest и in-transit (AES-256 / TLS 1.2+) | must |
| NFR-004 | maintainability | Все вызовы LLM логируются с входными/выходными данными для воспроизведения | must |
| NFR-005 | security | Развёртывание on-premise или в сертифицированном облаке (ФСТЭК / аналог) | must |

---

### Узел `formalize_constraints` → `generate_constraints_from_mentions`

| ID | Тип | Ограничение | Жёсткое? |
|---|---|---|---|
| CON-001 | compliance | Обработка ПД сотрудников требует правового основания (согласие или трудовой договор) + согласования с DPO | true |
| CON-002 | deployment | Данные не покидают периметр компании (кроме обезличенных признаков в LLM API) | true |
| CON-003 | auth | Единая точка аутентификации через ADFS, второй фактор обязателен | true |
| CON-004 | timeline | PoC должен показать первые результаты в течение 1–2 месяцев от старта | false |
| CON-005 | scope | Использование внешних LLM разрешено только для генерации текстов по обезличенным данным | true |

---

### Узел `generate_acceptance` → `generate_acceptance_criteria`

| ID | Требование | Критерий | Метод проверки |
|---|---|---|---|
| AC-001 | NFR-002 (Recall ≥ 0.60) | На hold-out выборке 2024 г.: Recall ≥ 0.60, Precision ≥ 0.50 | Offline-оценка перед пилотом |
| AC-002 | FR-004 (топ-3 фактора) | Каждое предсказание содержит 3 именованных фактора с весом | Ручная проверка 50 случайных записей |
| AC-003 | FR-005 (недельное обновление) | Пайплайн переобучения завершается без ошибок 4 недели подряд | Мониторинг логов MLOps |
| AC-004 | CON-001 (DPO) | Заключение DPO получено до начала пилота | Документ от DPO |
| AC-005 | NFR-003 (шифрование) | Pentest-отчёт: ПД не передаются в открытом виде | Внешний аудит или ДИТ |

---

### Узел `check_requirements_consistency` → `detect_contradictions`

Обнаружено одно потенциальное противоречие:

```json
{
  "is_consistent": false,
  "contradictions": [
    {
      "description": "FR-008 запрещает передачу ПД в LLM, но FR-006 требует AI-рекомендаций для HR на основе данных сотрудника. Неясно, на каких данных строятся рекомендации.",
      "involved_items": ["FR-006", "FR-008", "CON-005"],
      "severity": "warning"
    }
  ]
}
```

Severity = `warning` → эскалация не блокирует, но противоречие добавляется в cover_message при согласовании.

---

## Фаза I: Architecture — архитектурный анализ

**Файлы:** [`task_templates/08_architecture.yaml`](task_templates/08_architecture.yaml),
[`domain_packs/simple_ml_v1.yaml`](domain_packs/simple_ml_v1.yaml) § task_class_taxonomy, architecture_patterns, baseline_definitions

### Узел `classify_task` → `classify_within_taxonomy`

```json
{
  "class_id": "binary_classification",
  "class_name": "Бинарная классификация",
  "confidence": 0.94,
  "reasoning": "Целевая переменная — бинарная (уволится / останется), данные табличные, история 3 года с метками",
  "alternatives": []
}
```

`$context.active_task_class` = **`binary_classification`**

---

### Узел `select_pattern` → `select_pattern_from_catalog`

Проверяем `simple_ml_v1.architecture_patterns` с `input_characteristics`:

| Паттерн | Применимо к `binary_classification` | Условие применимости | Подходит? |
|---|---|---|---|
| `linear_baseline` | ✅ | всегда | ✅ (но как baseline) |
| `tree_based` | ✅ | всегда | ✅ |
| `gradient_boosting` | ✅ | `total_rows >= 5000` | ✅ (7800 строк) |
| `neural_network` | ✅ | `total_rows >= 100000` | ❌ (7800 < 100000) |

**Выход `pattern_choice`:**
```json
{
  "pattern_id": "gradient_boosting",
  "components": ["feature_engineering", "LightGBM/XGBoost", "SHAP_explainer", "weekly_retraining_pipeline"],
  "rationale": "7800 строк превышает порог 5000 для gradient_boosting. FR-004 (топ-3 фактора) требует объяснимости — SHAP встроен в экосистему LightGBM. NFR-002 (Recall ≥ 0.60) достижимее на gradient boosting, чем на линейных моделях для несбалансированных данных."
}
```

---

### Узел `arch_rationale` → `generate_rationale`

**Таблица сопоставления требование → как удовлетворяется:**

| Требование | Как удовлетворяется в LightGBM + SHAP |
|---|---|
| FR-003 (вероятность 0–1) | `predict_proba()` нативно |
| FR-004 (топ-3 фактора) | SHAP values → top-3 по абсолютному значению |
| FR-005 (недельное переобучение) | Airflow DAG / cron + incremental refit |
| NFR-002 (Recall ≥ 0.60) | Порог классификации настраивается на validation set |
| NFR-003 (шифрование) | Данные в pipeline не выходят за периметр |

**Риски:**
1. _Дрейф данных_ — поведение сотрудников меняется, модель деградирует без мониторинга. Митигация: PSI-мониторинг признаков еженедельно.
2. _Малый объём для некоторых торговых точек_ — модель может быть нестабильной на подвыборках < 100 человек. Митигация: единая глобальная модель с признаком «торговая точка».

---

### Узел `define_project_baseline` → `define_baseline`

Из `simple_ml_v1.baseline_definitions` для `binary_classification`:

```json
{
  "description": "Logistic Regression на сырых признаках из 1С ЗУП (без признаков портала и без настройки порога)",
  "expected_limitations": [
    "Не учитывает нелинейные зависимости между KPI и риском",
    "Чувствителен к несбалансированным классам (36:64%) без SMOTE/class_weight",
    "Не использует текстовые сигналы с корпоративного портала"
  ],
  "comparison_metrics": ["recall", "precision", "roc_auc", "f1"]
}
```

---

## Фаза J: Document Assembly — сборка ТЗ

**Файлы:** [`task_templates/09_document.yaml`](task_templates/09_document.yaml)

Задача `generate_document_section` вызывается **6 раз параллельно** с разными `section_spec`:

| Узел | Раздел | Ключевые входные артефакты |
|---|---|---|
| `section_context` | «Контекст и цели» | `consolidated_model`, `parsed_request` |
| `section_data` | «Данные» | `objects_inventory`, `sufficiency_verdict` |
| `section_requirements` | «Требования» | FR-001..008, NFR-001..005, CON-001..005 |
| `section_architecture` | «Архитектура» | `pattern_choice`, `arch_rationale`, `baseline` |
| `section_acceptance` | «Критерии приёмки» | AC-001..005 |
| `section_assumptions` | «Допущения и риски» | CON-001..005, `feasibility_verdict` |

После параллельной генерации узел `assemble` собирает документ с оглавлением и сквозной нумерацией.

---

## Фаза K: Validation and Approval — валидация и согласование

**Файлы:** [`task_templates/09_document.yaml`](task_templates/09_document.yaml),
[`workflow/escalation_policy.md`](workflow/escalation_policy.md),
[`reference/artifacts.md`](reference/artifacts.md)

### Параллельные проверки

**`check_completeness` → `check_document_completeness`**
Все обязательные разделы присутствуют. `is_complete = true`.

**`check_consistency` → `check_internal_consistency`**
Найдено предупреждение FR-006 vs FR-008 (из фазы H) — включается в документ, не блокирует.

**`check_trace` → `check_traceability`**
```json
{
  "traced": 17,
  "untraced": [
    { "element_id": "NFR-001", "content": "10 минут для скоринга базы" }
  ],
  "coverage": 0.94
}
```

`0.94 >= 0.9` → сигнал `coverage_below_threshold` не срабатывает.

---

### Узел `client_approval` → `request_approval_via_gateway`

ТЗ с cover_message уходит на согласование HR C&B + ДИТ.

**cover_message** подсвечивает ключевые допущения:
> «Ключевые допущения, требующие подтверждения:
> 1. DPO даёт заключение до старта пилота (AC-004).
> 2. Использование LLM — только по обезличенным признакам (CON-005, FR-008).
> 3. MLOps-ответственность (кто переобучает еженедельно) остаётся открытым вопросом.
> 4. Противоречие FR-006 vs FR-008 требует уточнения на архитектурном ревью.»

**Заказчик возвращает:** `approved_with_comments`

> «Согласовано. Уточнение по п.4: AI-рекомендации формируются на основе SHAP-факторов (числа), имена сотрудников в LLM не передаются. Это не противоречие, а уточнение реализации. Добавьте это в раздел архитектуры.»

---

### Узел `process_comments` → `classify_comments`

```json
[
  {
    "comment_text": "AI-рекомендации формируются на основе SHAP-факторов, имена не передаются",
    "target": "section_architecture + FR-008",
    "change_type": "clarification",
    "criticality": "low",
    "triggered_workflow_nodes": ["section_architecture", "section_requirements"]
  }
]
```

Действие `rerun_nodes`: перезапускаются `section_architecture` и `section_requirements` с уточнённым контекстом. Затем снова `assemble` и `validation_and_approval`.

При повторном согласовании заказчик отвечает: `approved`.

---

### Узел `finalize` → `finalize_artifact`

```json
{
  "content": "<<ТЗ: Предиктивная аналитика текучести кадров v1.0>>",
  "version": "1.0",
  "approved_at": "2025-02-14T11:30:00Z",
  "approved_by": ["HR C&B", "ДИТ"],
  "trace_id": "sg1-churn-poc-001"
}
```

**`$outputs.approved_technical_specification`** — финальный артефакт Stage Gate 1 передаётся в Stage Gate 2.

---

## Итоговая карта артефактов

Маппинг в терминах [`reference/artifacts.md`](reference/artifacts.md):

| Артефакт | Значение в примере |
|---|---|
| `need_model` | «Предотвращение текучести через ML-скоринг, цель −7 п.п.» |
| `active_domain_pack` | `simple_ml_v1` |
| `active_task_class` | `binary_classification` |
| `data_sources_inventory` | 3 источника: 1С ЗУП (кадры), 1С ЗУП (продажи), корпоративный портал |
| `data_sufficiency_verdict` | `sufficient=true`, `risk_level=low` |
| `functional_requirements` | FR-001 … FR-008 |
| `non_functional_requirements` | NFR-001 … NFR-005 |
| `project_constraints` | CON-001 … CON-005 (ПД, ADFS, LLM, on-premise) |
| `acceptance_criteria` | AC-001 … AC-005 |
| `architectural_approach` | gradient_boosting + SHAP + weekly Airflow pipeline |
| `approved_technical_specification` | ТЗ v1.0, согласовано HR C&B + ДИТ |
