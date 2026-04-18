# Артефакты и трассируемость

## Модель артефактов

В модульной архитектуре артефакты делятся на три класса.

**Универсальные артефакты** — типы данных, производимые Task Templates. Не имеют привязки к конкретному Workflow.  
Примеры: `typed_mentions`, `hypotheses`, `requirements`.

**Workflow bindings** — конкретные экземпляры артефактов, адресуемые через `$nodes.<node_id>.outputs.<field>`.  
Пример: `$nodes.extract_data.outputs.typed_mentions` и `$nodes.extract_metrics.outputs.typed_mentions` — два экземпляра одного типа.

**Внешние артефакты** — Domain Pack'и, конфигурации, реестры. Хранятся в Template Registry.

---

## Маппинг: артефакт v1 → источник в v2

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
| `architectural_approach` | композиция: `classify_task` + `select_pattern` + `arch_rationale` + `define_project_baseline` |
| `section_*` | `$nodes.section_*.outputs` (шесть вызовов одной задачи `generate_document_section`) |
| `draft_specification_document` | `$nodes.assemble.outputs` |
| `approved_technical_specification` | `$outputs.approved_technical_specification` |

---

## Внешние артефакты (Template Registry)

| Артефакт | Тип | Потребляется узлом |
|---|---|---|
| Domain Packs (`rag_v1`, `simple_ml_v1`, ...) | Domain Pack | все узлы с параметром `domain_pack` |
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
