# Stage Gate 1 — Преобразование бизнес-запроса в ТЗ (v2, модульная)

**Область применения:** первый макро-гейт платформы E2E-генерации PoV.  
**Поддомены первой очереди:** RAG-системы, простые ML-модели.

---

## Архитектурная модель

```
┌─────────────────────────────────────────────────────────────────┐
│  Слой 1: Task Templates (универсальные задачи)                  │
│  — Чистые I/O-контракты                                         │
│  — Не знают, в каком гейте используются                         │
│  — Принимают Domain Pack как входной параметр                   │
│  — Пример: extract_typed_mentions, generate_document_section    │
└─────────────────────────────────────────────────────────────────┘
                          ▲                    ▲
                          │ use               │ parameterizes
┌──────────────────────────┴───┐   ┌──────────┴──────────────────┐
│  Слой 2: Workflow Templates  │   │  Слой 3: Domain Packs       │
│  — Граф вызовов Task Templ.  │   │  — Плагины поддоменов       │
│  — Привязка данных           │   │  — Чеклисты, паттерны,      │
│  — Специфика гейта           │   │    шаблоны требований       │
│  — Политика эскалаций        │   │  — Пример: rag_v1, ml_v1    │
└──────────────────────────────┘   └─────────────────────────────┘
```

### Принципы разделения

| Слой | Отвечает на вопрос | Знает о других слоях |
|---|---|---|
| Task Template | **Что** делает задача | Нет |
| Domain Pack | **Какие данные** характерны для поддомена | Нет |
| Workflow | **Когда, зачем, что при сбое** | Да — оркестрирует оба |

**Добавление поддомена** = создать файл в `domain_packs/` + зарегистрировать в реестре. Задачи и Workflow не меняются.

**Добавление нового гейта** = создать файл в `workflow/`. Задачи переиспользуются.

---

## Навигация по файлам

### Task Templates (`task_templates/`)

| Файл | Категория | Задачи |
|---|---|---|
| [01_text_processing.yaml](task_templates/01_text_processing.yaml) | Обработка входного текста | `parse_free_text_to_structured`, `check_minimum_completeness`, `classify_against_registry`, `check_registry_membership`, `detect_patterns_from_catalog`, `synthesize_verdict` |
| [02_meaning_analysis.yaml](task_templates/02_meaning_analysis.yaml) | Анализ смысла текста | `extract_declared_goal`, `generate_hypotheses`, `identify_stakeholders`, `consolidate_analysis` |
| [03_typed_mentions.yaml](task_templates/03_typed_mentions.yaml) | Извлечение упоминаний | `extract_typed_mentions` |
| [04_gap_analysis.yaml](task_templates/04_gap_analysis.yaml) | Gap-анализ | `compare_against_checklist`, `prioritize_items`, `group_and_structure_items` |
| [05_user_interaction.yaml](task_templates/05_user_interaction.yaml) | Взаимодействие с пользователем | `generate_batched_questionnaire`, `generate_point_question`, `request_user_input_via_gateway`, `parse_user_response`, `validate_response` |
| [06_inventory.yaml](task_templates/06_inventory.yaml) | Инвентаризация объектов | `inventory_items_from_mentions`, `generate_clarification_questions`, `evaluate_sufficiency_by_heuristics`, `decide_synthetic_fallback` |
| [07_requirements.yaml](task_templates/07_requirements.yaml) | Формализация требований | `generate_requirements_from_templates`, `generate_constraints_from_mentions`, `generate_acceptance_criteria`, `detect_contradictions` |
| [08_architecture.yaml](task_templates/08_architecture.yaml) | Архитектурный анализ | `classify_within_taxonomy`, `select_pattern_from_catalog`, `generate_rationale`, `define_baseline` |
| [09_document.yaml](task_templates/09_document.yaml) | Сборка и валидация документов | `generate_document_section`, `assemble_document`, `check_document_completeness`, `check_internal_consistency`, `check_traceability`, `request_approval_via_gateway`, `classify_comments`, `finalize_artifact` |

### Domain Packs (`domain_packs/`)

| Файл | Содержимое |
|---|---|
| [_schema.yaml](domain_packs/_schema.yaml) | Эталонная схема Domain Pack — все поля с описаниями |
| [rag_v1.yaml](domain_packs/rag_v1.yaml) | Domain Pack для RAG-систем |
| [simple_ml_v1.yaml](domain_packs/simple_ml_v1.yaml) | Domain Pack для простых ML-моделей |

### Workflow (`workflow/`)

| Файл | Содержимое |
|---|---|
| [biz_to_tz_v1.yaml](workflow/biz_to_tz_v1.yaml) | Workflow: метаданные + фазы A–K (11 фаз) |
| [escalation_policy.md](workflow/escalation_policy.md) | Справочник действий при escalation signals |

### Справочники (`reference/`)

| Файл | Содержимое |
|---|---|
| [artifacts.md](reference/artifacts.md) | Модель артефактов, маппинг v1→v2, внешние артефакты |
| [verification.md](reference/verification.md) | Покрытие зон, соответствие комментарию заказчика и принципам PoV.md |

---

## Быстрый поиск задачи по template_id

| template_id | Файл |
|---|---|
| `parse_free_text_to_structured` | [01_text_processing.yaml](task_templates/01_text_processing.yaml) |
| `check_minimum_completeness` | [01_text_processing.yaml](task_templates/01_text_processing.yaml) |
| `classify_against_registry` | [01_text_processing.yaml](task_templates/01_text_processing.yaml) |
| `check_registry_membership` | [01_text_processing.yaml](task_templates/01_text_processing.yaml) |
| `detect_patterns_from_catalog` | [01_text_processing.yaml](task_templates/01_text_processing.yaml) |
| `synthesize_verdict` | [01_text_processing.yaml](task_templates/01_text_processing.yaml) |
| `extract_declared_goal` | [02_meaning_analysis.yaml](task_templates/02_meaning_analysis.yaml) |
| `generate_hypotheses` | [02_meaning_analysis.yaml](task_templates/02_meaning_analysis.yaml) |
| `identify_stakeholders` | [02_meaning_analysis.yaml](task_templates/02_meaning_analysis.yaml) |
| `consolidate_analysis` | [02_meaning_analysis.yaml](task_templates/02_meaning_analysis.yaml) |
| `extract_typed_mentions` | [03_typed_mentions.yaml](task_templates/03_typed_mentions.yaml) |
| `compare_against_checklist` | [04_gap_analysis.yaml](task_templates/04_gap_analysis.yaml) |
| `prioritize_items` | [04_gap_analysis.yaml](task_templates/04_gap_analysis.yaml) |
| `group_and_structure_items` | [04_gap_analysis.yaml](task_templates/04_gap_analysis.yaml) |
| `generate_batched_questionnaire` | [05_user_interaction.yaml](task_templates/05_user_interaction.yaml) |
| `generate_point_question` | [05_user_interaction.yaml](task_templates/05_user_interaction.yaml) |
| `request_user_input_via_gateway` | [05_user_interaction.yaml](task_templates/05_user_interaction.yaml) |
| `parse_user_response` | [05_user_interaction.yaml](task_templates/05_user_interaction.yaml) |
| `validate_response` | [05_user_interaction.yaml](task_templates/05_user_interaction.yaml) |
| `inventory_items_from_mentions` | [06_inventory.yaml](task_templates/06_inventory.yaml) |
| `generate_clarification_questions` | [06_inventory.yaml](task_templates/06_inventory.yaml) |
| `evaluate_sufficiency_by_heuristics` | [06_inventory.yaml](task_templates/06_inventory.yaml) |
| `decide_synthetic_fallback` | [06_inventory.yaml](task_templates/06_inventory.yaml) |
| `generate_requirements_from_templates` | [07_requirements.yaml](task_templates/07_requirements.yaml) |
| `generate_constraints_from_mentions` | [07_requirements.yaml](task_templates/07_requirements.yaml) |
| `generate_acceptance_criteria` | [07_requirements.yaml](task_templates/07_requirements.yaml) |
| `detect_contradictions` | [07_requirements.yaml](task_templates/07_requirements.yaml) |
| `classify_within_taxonomy` | [08_architecture.yaml](task_templates/08_architecture.yaml) |
| `select_pattern_from_catalog` | [08_architecture.yaml](task_templates/08_architecture.yaml) |
| `generate_rationale` | [08_architecture.yaml](task_templates/08_architecture.yaml) |
| `define_baseline` | [08_architecture.yaml](task_templates/08_architecture.yaml) |
| `generate_document_section` | [09_document.yaml](task_templates/09_document.yaml) |
| `assemble_document` | [09_document.yaml](task_templates/09_document.yaml) |
| `check_document_completeness` | [09_document.yaml](task_templates/09_document.yaml) |
| `check_internal_consistency` | [09_document.yaml](task_templates/09_document.yaml) |
| `check_traceability` | [09_document.yaml](task_templates/09_document.yaml) |
| `request_approval_via_gateway` | [09_document.yaml](task_templates/09_document.yaml) |
| `classify_comments` | [09_document.yaml](task_templates/09_document.yaml) |
| `finalize_artifact` | [09_document.yaml](task_templates/09_document.yaml) |
