# Политика реакции на escalation signals

Workflow централизует обработку сигналов. Это позволяет менять политику
(строгая / мягкая / с дополнительными проверками) без изменения Task Templates.

**Принцип:** задачи только _сигнализируют_. Что делать при сигнале — решает Workflow.

---

## Справочник действий

| Действие | Что делает |
|---|---|
| `abort_workflow` | Завершает Workflow с ошибкой, сообщает Stage-Gate Manager |
| `abort_with_escalation` | То же + Interruption Gateway уведомляет человека с полным контекстом |
| `escalate_to_human` | Останавливает текущую фазу, ждёт решения человека, после — возобновляет |
| `escalate_to_project_manager` | То же, но адресат — PM (для бизнес-решений) |
| `rollback_to_phase` | Помечает артефакты последующих фаз как Obsolete, запускает фазу заново (штатный Bubble Up) |
| `request_completion_from_client` | Interruption Gateway → заказчик с конкретным запросом |
| `notify_client_caveats` | Неблокирующее уведомление заказчику |
| `iterate_phase` | Запускает фазу повторно (с учётом лимита итераций) |
| `rerun_nodes` | Помечает конкретные узлы Obsolete и перезапускает их с зависимостями (для обработки комментариев) |
| `mark_gap_as_unresolvable` | Записывает пробел как нерешаемый в gap_list, продолжает работу |

---

## Маппинг: сигнал → действие по умолчанию

| Сигнал | Узел | Действие |
|---|---|---|
| `parsing_failed` | parse_request | `abort_workflow` |
| `schema_violation` | parse_request | retry → abort |
| `incomplete` | check_request_minimum | `request_completion_from_client` |
| `unsupported_category` | select_domain_pack | `abort_with_escalation` |
| `low_confidence` | select_domain_pack | `escalate_to_human` |
| `blocker_detected` | detect_unfeasibility | `abort_with_escalation` |
| `no_goal_found` | extract_goal | `rollback_to_phase` → intake |
| `all_low_plausibility` | root_cause_hypotheses / baseline_hypotheses | `escalate_to_human` |
| `extreme_distribution` | prioritize_gaps | `notify_client_caveats` |
| `too_many_questions` | build_questionnaire | trim to max, log warning |
| `timeout_exceeded` | request_clarifications | `escalate_to_project_manager` |
| `cannot_answer` | request_clarifications | `mark_gap_as_unresolvable` (если blocking → `escalate_to_human`) |
| `contradictions_detected` | validate_clarifications | `iterate_phase` (лимит 2) |
| `critical_unanswered` | validate_clarifications | `escalate_to_project_manager` |
| `inventory_empty` | inventory_data_sources | inject question → clarification loop |
| `insufficient_no_fallback` | evaluate_data_sufficiency | `escalate_to_human` |
| `needs_client_confirmation` | synthetic_decision | `request_confirmation_via_gateway` |
| `base_set_unachievable` | generate_functional / generate_non_functional | `escalate_to_human` |
| `critical_contradictions` | check_requirements_consistency | `escalate_for_conflict_resolution` |
| `no_matching_class` | classify_task | `escalate_to_human` (пересмотр Domain Pack) |
| `no_pattern_satisfies_constraints` | select_pattern | `escalate_to_human` |
| `critical_missing` | check_completeness | `rollback_to_phase` |
| `blocker_contradictions` | check_consistency | `rollback_to_phase` → requirements_formalization |
| `coverage_below_threshold` | check_trace | `escalate_and_request_confirmations` |
| `timeout_exceeded` | client_approval | `escalate_to_project_manager` |
| `deep_rework_requested` | client_approval | `abort_with_escalation` |
| `comment_unlocalizable` | process_comments | log, skip comment |
| `scope_change_required` | process_comments | `abort_with_escalation` |
| `storage_failure` | finalize | retry → abort |
