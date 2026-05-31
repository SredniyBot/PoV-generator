# application/ — оркестрация

Application-слой связывает `domain/` (инварианты) и `infrastructure/`
(SQLite-runtime, LLM-провайдеры) в рабочие сценарии: планирование, исполнение,
валидация, уточнения, UI-команды. Каждый сервис — тонкая обёртка над
`SqliteRuntime`; стейт хранится в БД (per-call connection, thread-safe).

## Поток исполнения leaf-задачи

Один шаг = `WorkflowService.run_next` (`workflow_service.py:41`). Автономный
прогон гоняет его в цикле: `WorkflowRunnerService._run_loop`
(`workflow_runner_service.py:147`) в фоновом потоке, обновляя `workflow_runs`
после каждого шага (mtime БД → инвалидация realtime_token → WS-broadcast).

Внутри одного шага (`workflow_service.py:41`–`:102`):

1. **Планирование** — `PlanningService.plan(mode="apply")` (`planning_service.py:66`):
   expand_graph (root + composites) → `_recompute_admission` → выбор max по score
   (`:81`). `outcome != "selected"` → шаг терминальный (`objective_completed` /
   `blocked`), задача не исполняется.
2. **Исполнение** — `ExecutionService.execute_task` (`execution_service.py:49`).
   Внутри: `ContextService.build_for_task` собирает `context_manifest`
   (`execution_service.py:62`), резолв провайдера/сложности/методологии, LLM/stub/merge,
   запись artifact (json+md) + execution_run. Возвращает `ExecutionBundle`
   (request/result/traces, в т.ч. `methodology_candidates`, `proposed_goal`).
3. **Валидация** — `ValidationService.validate_execution` (`validation_service.py:80`):
   JSON-schema (`:122`), доменный `_semantic_analysis` (`:135`), + эмиссия
   clarification-кандидатов (см. ниже). Возвращает `ValidationRun.passed`.
4. **proposed_goal** — если исполнение предложило цель, применяется ДО transition
   (`workflow_service.py:84`), чтобы admission следующих задач увидел свежую цель.
5. **Transition** — `complete` если passed, иначе `fail` (`workflow_service.py:89`).
   Failed задачу admission больше не выберет; auto-resume берёт следующую готовую.

**Clarification-петля (вне run_next, из UI при ответе пользователя):**
`ClarificationService.answer_clarification` (`clarification_service.py:251`) →
пишет decision-position в knowledge (`apply_knowledge_patch`, `:274`) →
`_propagate_answer_to_duplicates` закрывает дубли того же вопроса (`:360`) →
`_auto_retry_failed_tasks` переводит зависшие failed-задачи в retry (`:331`).

## Сервисы

- **registry_service** — загрузка DSL-реестра, `validate()` → immutable `RegistrySnapshot`. spec/02.
- **project_service** — init workspace + двухслойный state (knowledge/process); команды:
  цель, gap, readiness, методология, домен, engagement-режим. spec/05.
- **planning_service** — `plan` (`:66`), `expand_graph` (`:38`), `_recompute_admission`
  (`:263`): топология task graph + admission. spec/06, spec/03.
- **context_service** — `build_for_task`: `context_manifest` с приоритетами; конвенция
  входных артефактов `source_ref="artifact:<id>"`. spec/07.
- **execution_service** — `execute_task` (`:49`): wrapper методологии + вызов провайдера. Ниже. spec/07.
- **methodology_rules** — `evaluate_methodology_rules` (`:135`): прогон `if`-правил
  пакета через AST-эвалюатор → `ClarificationCandidate`. spec/02.
- **methodology_rule_eval** — узкий AST-эвалюатор `if:`-выражений. Ниже.
- **validation_service** — `validate_execution` (`:80`): schema + governance + эмиссия
  уточнений. Ниже. spec/08.
- **clarification_service** — `register_candidates` (`:146`), `_decide_action` (`:1101`),
  `answer_clarification` (`:251`), `set_mode` (`:584`). Ниже. spec/12.
- **complexity_selector_service** — `select_complexity` (`:66`): pre-selector сложности. Ниже.
- **domain_pack_selection_service** — авто-выбор domain packs по бизнес-запросу (stub-matcher
  по `entry_signals` или LLM). spec/09.
- **merge_strategies** — `structural_merge` (`:27`): детерминированный deep-merge. Ниже.
- **artifact_contracts** — `artifact_schema` (`:54`) / `schema_instruction` (`:978`) /
  `render_markdown` (`:1634`) / `validate_json_schema` (`:934`): схема, промпт-инструкция,
  md-рендер по `artifact_role` + активным domain packs. Самый большой модуль (контракты артефактов).
- **provider_settings_service** — CRUD connections/routings, резолв провайдера/модели по purpose,
  `ensure_default_settings` (bootstrap из env).
- **workflow_service** — `run_next` (`:41`): дирижёр одного шага (см. выше).
- **workflow_runner_service** — фоновый прогон до блокировки (`_run_loop`, `:147`). spec/06.
- **workspace_query_service** — read-модель (DTO) для веб-воркспейса. spec/10, spec/11.
- **workspace_command_service** — write-команды из UI, делегирует профильным сервисам.
- **workspace_catalog** — каталог доступных воркспейсов (project_id → workspace path).
- **pdf_export** — markdown артефакта → PDF (markdown→HTML→xhtml2pdf); регистрирует Unicode-TTF
  для кириллицы; авто-ширина колонок + landscape для широких таблиц.
- **attachment_service** — `AttachmentService`: загрузка входных файлов, фоновое извлечение текста
  (pypdf/.pdf, python-docx/.docx, plain .txt/.md/.json/.csv), подача в Layer A как `Position`,
  удаление-до-использования (лимиты ≤25MB/≤50 файлов; failed/unsupported в контекст не попадают).

## Нетривиальная логика и gotchas

**execution_service — switch по провайдеру (`:90`–`:186`).** Три пути резолва LLM:
- non-LLM (`active_provider == "stub"` или `merge.strategy == "structural"`, `:91`) — провайдер не нужен.
- legacy явное имя (`openrouter` / `claude_sdk` / `claude_subscription`, `:96`) —
  `llm_registry.get(...)`, кредиты из env. Для тестов/CLI.
- **основной путь** (`provider=None`, `:104`) — `resolve_for_purpose("execution", ...)`:
  модель и connection из settings-store. **Gotcha:** env-переменные больше НЕ управляют
  выбором провайдера в основном workflow (`:69`–`:74`) — только bootstrap для
  `ensure_default_settings`. Параметр `provider` у `execute_task` — явный override (CLI/тест).
- Диспетч генерации (`:136`–`:186`): structural-merge / stub / LLM. `merge.strategy=="hybrid"`
  → `ConflictError` (не реализовано, `:143`).
- `chat_json` теперь возвращает `LLMResult` (payload + usage); расход токенов на каждый вызов
  пишется в таблицу `llm_usage` (best-effort, не валит исполнение).
- **Версионирование:** retry того же role+task связывает версии через `parent_artifact_id`,
  предыдущий помечается superseded (`:228`–`:279`). `overall_confidence` вынесен из тела
  артефакта в метаданные (`:270`).

**Per-stage CoT (`stage_execution_mode`).** `_execute_single_call` (`:410`) vs
`_execute_per_stage_cot` (`:445`). Mode читается из активного methodology_pack
(`active_methodology.stage_execution_mode == "per_stage_cot"`, `:166`); без методологии —
всегда single_call. Per-stage: отдельный LLM-вызов на каждую стадию с накопительным
контекстом + финальный на primary. single_call: primary+reasoning в одной объединённой схеме.

**methodology_rule_eval — AST-эвалюатор (почему не eval).** `evaluate_rule`
(`methodology_rule_eval.py:46`) парсит выражение через `ast.parse(mode="eval")` и обходит
дерево whitelist-визитором `_Evaluator` (`:82`). `eval()` запрещён — иначе YAML-правило
домена исполняло бы произвольный Python. Whitelist:
- узлы — только `_visit_*` (`:100`–`:190`): Constant, Name, Attribute, Subscript, Call,
  UnaryOp, BinOp, Compare, BoolOp, List, Tuple. Любой другой узел → `_RuleEvalError`.
- функции (`_functions`, `:90`–`:98`): `len`, `count` (=len), `max`, `min`, `sum`,
  `second` (второй по убыванию), `is_null`; числовые фильтруют через `_filter_numeric`
  (`:275`, bool исключён).
- `.attr` на `list[dict]` — неявная проекция (`_attr_or_project`, `:212`); маркер `[*]`
  стрипается регэкспом (`:60`).
- **Gotcha:** любая ошибка (синтаксис, неизвестное имя, неподдерживаемый узел) → `False`,
  не исключение (`:70`–`:79`). Неполный reasoning не валит workflow, но и опечатка в правиле
  молча не срабатывает.

**clarification_service — `_decide_action` (`:1101`): ask / assume / defer.**
Решение основано на **visibility + engagement-mode**, НЕ на confidence (`:1116`):
1. `candidate.visibility in proactive_ask_levels(mode)` → `ask`. Таблица
   (`domain/process_state.py:68`): autopilot→∅, balanced→{principal}, expert→{principal,
   architectural, technical}.
2. иначе если есть `default_assumption` → `assume` (тихо принять допущение).
3. иначе если `blocking_scope == "objective"` → `ask` (страховка для gate-signoff).
4. иначе `defer` (мягкий skip, остаётся в инбоксе под «Отложено»).
- Дефолтная visibility роли — `_ROLE_DEFAULT_VISIBILITY` (`:45`), не «confidence floor»:
  business/client/security→principal, data_owner/methodologist→architectural, architect→technical.
- `register_candidates` (`:146`): дедуп по (source) + cross-task по нормализованному вопросу
  (`:159`–`:194`), затем `_decide_action` решает initial_status. `set_mode` (`:584`)
  переоценивает уже-открытые вопросы при смене режима (autopilot не должен оставлять open).
- **Где порог confidence:** в `validation_service`, а не здесь —
  `template.validation.confidence_threshold` (`validation_service.py:182`). Ниже порога →
  finding `low_confidence` + clarification-кандидат (`:200`).

**validation_service — эмиссия уточнений.** `validate_execution` порождает кандидатов из трёх
источников и регистрирует через `clarification_service.register_candidates`: (1) сработавшие
methodology-правила из `execution_bundle.methodology_candidates` (`:147`); (2) `blocking_questions`
из метаданных артефакта (`:161`); (3) низкая уверенность (`:200`). gate human_approval →
`_maybe_emit_gate_candidates` (blocking_scope=objective).

**merge_strategies — structural_merge (`:27`).** Чистая функция без LLM: рекурсивный deep-merge
inputs слева направо. dict → merge по ключам; list → по `conflict_policy` (`union` дедуп с
сохранением порядка / `first_wins` / `last_wins` / `fail_on_conflict`); scalar → по умолчанию
first_wins. Входы не мутируются (`_clone`, `:148`). Результат отдельно валидируется против контракта.

**complexity_selector (`POV_COMPLEXITY_SELECTOR`).** `select_complexity` (`:66`). Default **off** →
declared `template.complexity` без вызова. `=stub` → детерминированный `_stub_select` (`:118`:
≥3 domain packs / ≥3 открытых уточнений → complex). `=on` → `_llm_select` (`:153`, дешёвая модель
через `resolve_for_purpose("complexity_selector")`). Любая ошибка LLM → fallback на declared,
никогда не блокирует. Override: `POV_COMPLEXITY_SELECTOR_MODEL` / `_PROVIDER`.

**Как добавить LLM-провайдера.** Реализация — в `infrastructure/llm` (`LLMProvider` /
`LLMProviderRegistry`). Для основного потока: завести connection + routing на purpose `execution`
через `provider_settings_service`; `execute_task` подхватит через `resolve_for_purpose`. Явное имя
в ветке `:96` — только legacy/тесты. Для нового purpose (как complexity_selector,
clarification_ce11) — добавить routing и звать `resolve_for_purpose(<purpose>, ...)`.

**Как добавить stub-фикстуру / artifact_role.** Статические stub-payload'ы — JSON в
`templates/stub_fixtures/` + запись в реестре, без правки Python (`execution_service.py:853`).
Compose-stub'ы (requirements_spec, review_report, solution_tradeoff_matrix) собираются из
parsed_inputs в `_execute_stub` (`:821`). Новый `artifact_role` требует схемы/рендера в
`artifact_contracts.py` (`artifact_schema` / `schema_instruction` / `render_markdown`).
