# Сверка с принципами и покрытием

## Покрытие восьми зон из исходного ТЗ

| Зона | Task Templates | Узлы Workflow |
|---|---|---|
| 1. Приём и первичная обработка | `parse_free_text_to_structured`, `check_minimum_completeness`, `classify_against_registry` | parse_request, check_request_minimum, select_domain_pack |
| 2. Понимание потребности | `extract_declared_goal`, `generate_hypotheses` (×2), `identify_stakeholders`, `consolidate_analysis` | фаза need_analysis |
| 3. Сбор недостающей информации | `compare_against_checklist`, `prioritize_items`, `group_and_structure_items`, `generate_batched_questionnaire`, `generate_point_question`, `request_user_input_via_gateway`, `parse_user_response`, `validate_response` | фазы gap_analysis и clarification |
| 4. Работа с данными | `extract_typed_mentions`, `inventory_items_from_mentions`, `generate_clarification_questions`, `evaluate_sufficiency_by_heuristics`, `decide_synthetic_fallback` | фаза data_processing + extract_data из declarative_extraction |
| 5. Определение требований | `extract_typed_mentions`, `generate_requirements_from_templates`, `generate_constraints_from_mentions`, `generate_acceptance_criteria`, `detect_contradictions` | фаза requirements_formalization + остальные extract_* |
| 6. Архитектурный анализ | `classify_within_taxonomy`, `select_pattern_from_catalog`, `generate_rationale`, `define_baseline` | фаза architecture |
| 7. Формирование документа | `generate_document_section`, `assemble_document` | фаза document_assembly |
| 8. Валидация ТЗ | `check_document_completeness`, `check_internal_consistency`, `check_traceability`, `request_approval_via_gateway`, `classify_comments`, `finalize_artifact` | фаза validation_and_approval |

Ранняя feasibility-проверка: `detect_patterns_from_catalog` + `check_registry_membership` + `synthesize_verdict` в фазе feasibility.

---

## Соответствие комментарию заказчика (v1 → v2)

| Критика v1 | Решение в v2 |
|---|---|
| «Жёсткий граф на уровне задач, сложно дебажить» | Граф перенесён в Workflow. Task Templates не знают друг о друге и о своём месте в пайплайне. |
| «Задачи не переиспользуемы» | `extract_typed_mentions` вызывается 5 раз, `generate_document_section` — 6 раз, `generate_hypotheses` — 2 раза. Любая задача может быть вызвана в другом Workflow. |
| «Доменные знания вшиты в задачи» | Всё доменное знание вынесено в Domain Pack. Добавление поддомена = новый файл в `domain_packs/`. |
| «Структура слишком жёстко задана» | 11 фаз с явными зависимостями и параллелизмом. Внутри фазы узлы меняются без каскадных правок. |

---

## Соответствие принципам PoV.md

| Принцип | Как отражён |
|---|---|
| Фокус на потребностях | Фаза `need_analysis` обязательна до любых требований. Фаза `clarification` снимает blocking-пробелы до архитектуры. |
| Прозрачность | `check_traceability` с порогом 0.9. Каждое извлечение упоминаний обязано содержать цитату. |
| Масштабируемость | Добавление поддомена = Domain Pack. Добавление гейта = новый Workflow с переиспользованием задач. |
| Воспроизводимость | NFT-шаблоны в каждом Domain Pack. Декомпозиция на атомарные LLM-вызовы снижает вариативность. |
| Самоконтроль | Фаза `validation_and_approval`: три параллельные проверки. Политика эскалаций централизована в Workflow. |

---

## Дополнительные преимущества архитектуры

**Независимое тестирование.** Task Template тестируется в изоляции с моковыми входами. Workflow — отдельно с моковыми задачами.

**Версионирование.** Task Template, Domain Pack и Workflow версионируются независимо. Обновление `rag_v1` → `rag_v2` не требует перевыпуска Workflow.

**A/B на уровне паттернов.** Можно выпустить `rag_v1` и `rag_v1_experimental` и маршрутизировать запросы в разные паки.

**Переиспользование в других гейтах.** `generate_document_section`, `check_traceability`, `request_approval_via_gateway` явно нужны в Stage Gate 2 и Stage Gate 3.
