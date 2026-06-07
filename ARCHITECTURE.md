# Architecture

> Карта системы для разработчика, который только зашёл в репо. Источник истины
> контрактов — [`specs/`](specs/). Этот файл — гид по нему.

PoV Generator — фреймворк управляемого получения проектных артефактов (первый
сценарий — построение ТЗ). Ключевые свойства: явная методология рассуждения,
декомпозиция задач, провайдер-агностичный исполнитель, UI пирамида L1→L4
для бизнес-менеджера с настраиваемой вовлечённостью.

---

## 1. Поток одной leaf-задачи

Сегодня реестр содержит несколько objective'ов (`common.requirements_specification` —
коммерческое ТЗ, `architecture.system_design` — архитектурный документ); проект
привязан к одному objective. Для каждого активна одна методология: ТЗ-поток
использует `process.lean_jtbd` (decision-style), архитектурный — `process.descriptive_decomposition`
(descriptive-style); выбор делается в `project_service.init_project` по префиксу
`objective_ref.identifier`.

```
бизнес-запрос
  └─ Objective (templates/objectives/*.yaml)
       └─ корневая задача (composite task_template)
            ├─ дочерние задачи (composite или leaf)
            └─ slots ← вклад domain pack'ов

на каждое исполнение leaf-задачи:

   methodology wrapper (стадии активного methodology_pack)
   → ContextManifest (state + входные артефакты + summary)
   → Provider (stub | openrouter | claude_sdk | claude_subscription)
   → три артефакта: primary + reasoning + methodology_trace
   → validation (schema + semantic + quality_gate candidate)
   → ClarificationCoordinator (candidate → ask | assume | defer)
   → patches → ProjectKnowledge / ProcessState
   → planner: следующая задача или блокировка
```

---

## 2. Восемь kinds реестра

Реестр живёт в [`templates/`](templates/). Каждый YAML — один объект одного kind.

| kind | Что описывает | Где лежит | Спека |
|---|---|---|---|
| `objective` | цель проекта: корневая задача + done_when | `templates/objectives/` | [02_registry_dsl.md](specs/02_registry_dsl.md) |
| `task_template` | тип работы (composite/leaf/fan_out): входы, выход, контекст | `templates/tasks/<area>/` | [04_task_template_semantics.md](specs/04_task_template_semantics.md) |
| `artifact_contract` | JSON-schema выходного артефакта | `templates/artifacts/` | [02_registry_dsl.md](specs/02_registry_dsl.md) |
| `domain_pack` | «над чем думаем»: сигналы + расширения слотов | `templates/domains/` | [09_domain_packs.md](specs/09_domain_packs.md) |
| `methodology_pack` | «как мы думаем»: стадии + правила | `templates/methodologies/` | [02_registry_dsl.md](specs/02_registry_dsl.md) |
| `quality_gate` | `human_approval` / `external_signoff` / `automated_review` | `templates/gates/` | [08_validation_governance.md](specs/08_validation_governance.md) |
| `capability_profile` | «что и насколько мы умеем реализовать»: умения (tech/предусловия/пределы/зрелость) + cannot_do | `templates/capabilities/` | [docs/plans/2026-06-06-realizability-capabilities-redesign.md](docs/plans/2026-06-06-realizability-capabilities-redesign.md) |
| `vocabulary` | общие словари (slot ids, readiness dims, capabilities) | `templates/vocabularies/` | — |

Жёсткое правило ортогональности: `methodology_pack` («как думаем») и
`domain_pack` («над чем думаем») не смешиваются. Конфликт по
`reasoning_artifact` — ошибка валидации.

Третья ось — **реализуемость** (`capability_profile`): «что и насколько мы реально
умеем построить». Привязка задачи к профилю — поле `capability_ref` (ортогонально
обычному `executor: llm`, а не новый механизм исполнения): execution-слой
подмешивает контракт умений в system-prompt. Оценка реализуемости консервативна
(по умолчанию «не реализуемо, пока не доказано»), непокрытое становится «зоной
роста», а требуемые от пользователя данные — «реквизитами». Подробности и roadmap
доработки — в
[docs/plans/2026-06-06-realizability-capabilities-redesign.md](docs/plans/2026-06-06-realizability-capabilities-redesign.md).

---

## 3. Карта модулей

```
src/pov_generator/
├── domain/                  ← чистые модели
│   ├── registry.py          ─ TemplateSpec, MethodologyPackSpec, QualityGateSpec
│   ├── clarifications.py    ─ ClarificationCandidate, DecisionOwnerRole
│   ├── execution.py         ─ ExecutionRequest, ExecutionResult
│   ├── tasks.py             ─ TaskRecord
│   ├── project_knowledge.py ─ Layer A: положения + KnowledgePatch
│   ├── process_state.py     ─ Layer B: пробелы/готовность/паки
│   ├── project_state.py     ─ ProjectManifest + StateEvent
│   └── workspace_views.py   ─ DTO для UI
│
├── application/             ← оркестрация
│   ├── registry_service.py
│   ├── project_service.py        ─ init_project, set_methodology
│   ├── planning_service.py       ─ expand_graph, plan
│   ├── context_service.py        ─ сборка ContextManifest
│   ├── execution_service.py      ─ wrapper методологии + provider call
│   ├── methodology_rules.py      ─ evaluate_methodology_rules
│   ├── methodology_rule_eval.py  ─ AST-эвалюатор `if:` выражений
│   ├── validation_service.py     ─ проверки + gate candidates
│   ├── clarification_service.py  ─ role floor, action decision
│   ├── workflow_service.py
│   └── workspace_query_service.py
│
├── infrastructure/
│   ├── sqlite_runtime.py             ─ event log + projections
│   ├── filesystem_registry.py        ─ YAML loader
│   ├── openrouter_client.py
│   ├── claude_sdk_client.py          ─ Anthropic SDK + tool-use
│   └── claude_subscription_client.py ─ claude-agent-sdk через локальный CLI
│
└── interfaces/
    ├── api.py               ─ FastAPI + WebSocket
    └── cli.py               ─ povgen CLI
```

```
ui/workspace/src/
├── App.tsx               ─ роутинг, страницы L1/L2/L3/L4
├── api.ts, types.ts      ─ типизированный REST + TS-зеркало DTO
├── ui.tsx                ─ дизайн-система
├── useProjectRealtime.ts ─ WebSocket-подписка
└── styles.css            ─ CSS-токены
```

---

## 4. UI пирамида L1→L4

| Уровень | Компонент | Источник |
|---|---|---|
| **L1** Mission Control | `MissionControlPage` | `/api/projects/:id/overview` |
| **L2** Методология | `MethodologyPage` | `/api/registry/methodology-packs` |
| **L2** Активность | `OverviewPage` | проекции situation/timeline/task_graph |
| **L3** Рассуждение | `ReasoningPanel` | `/api/projects/:id/tasks/:taskId/methodology-trace` |
| **L4** Provenance | `ProvenanceViewer` | тот же endpoint + блок `execution` |

При открытии проекта менеджер попадает на L1. Технические детали ниже —
только по явному drill-down. Спека: [10_ui_workspace.md](specs/10_ui_workspace.md).

---

## 5. Engagement-level: две ортогональные оси

Глубина вовлечённости менеджера — это **два** независимых поля:

| Ось | Поле | Значения |
|---|---|---|
| Частота показа | `clarification_mode` + `min_participation_mode` | `autopilot / balanced / control / expert` |
| Кто решает | `decision_owner_role` | `business / client / methodologist / architect / data_owner / security` |

Логика `_decide_action` в `clarification_service.py`:

1. Confidence ≥ 0.72 и есть `default_assumption` → **assume**.
2. Иначе режим менеджера ≥ `_ROLE_FLOOR[role]` → **ask**.
3. Иначе если `default_assumption` отсутствует → всё же **ask**.
4. Иначе → **assume**.

Floor по умолчанию: `business/client` — autopilot, `security` — balanced,
`methodologist/data_owner` — control, `architect` — expert.

---

## 6. Откат шага (step rollback)

Откат возвращает проект к состоянию **до** выполнения выбранного шага.
Семантика — **только зависимые**: инвалидируются целевой шаг и все транзитивно
зависящие от него; независимые ветки сохраняются. Артефакты откаченных шагов
**архивируются** (не удаляются), задачи сбрасываются для повторного прохода.
Опирается на уже существующие провенанс, событийный лог состояния и отмену —
это системная фича, а не костыль.

**Почему это вообще возможно.** `knowledge`/`process` событийные: снимок-
последнего + append-only лог патчей `state_events` (версия + `patch_type` +
`payload_json`). Состояние восстановимо реплеем патчей. Перед каждым листовым
шагом снимается `step_checkpoint` (pre-state). Проекции (`artifacts`,
`decisions`, …) мутабельны, но с провенансом (`created_by_task_id`,
`source_task_id`, `state_events.task_id`).

**Граф зависимостей** (`rollback_graph.py`): звуковой, не эвристика. Ребро
X→Y, если `write-set(X) ∩ read-set(Y) ≠ ∅` и `seq(X) < seq(Y)`; откатываемое
множество — транзитивное замыкание от целевого шага.

**Реконструкция** (`rollback_service.py`): берём чекпоинт самого раннего
откаченного шага как базу; реплеим «пережившие» патчи (не аннулированные, не из
откаченных шагов, по версии своего слоя — счётчики `state_events.id` и
`step_checkpoints.seq` независимы, поэтому сравниваем именно версии
knowledge/process); пишем новый снимок; аннулируем патчи откаченных шагов
(`rolled_back_by`); архивируем их артефакты/решения; сбрасываем задачи командой
`rollback_reset` (+ структурные родители для реплана).

**Конкуррентность** (`rollback_coordinator.py` + `project_lock.py`): координатор
берёт эксклюзивный `project_lock`, форсированно гасит активный прогон и ждёт
оседания, выполняет откат и снимает замок (в `finally`). Пока замок держится,
мутации проекта (`run`, активация objective, ответы на решения, повторный
откат) отклоняются через `ensure_project_unlocked` → `ConflictError` (409).

| Слой | Модуль |
|---|---|
| Домен | `domain/rollback.py` (StepCheckpoint, RollbackRecord/Result, ProjectLock) |
| Граф/реплей | `application/rollback_graph.py`, `application/state_patch_codec.py` |
| Движок | `application/rollback_service.py` |
| Шлюз/оркестрация | `application/project_lock.py`, `application/rollback_coordinator.py` |
| Инфра | `sqlite_runtime`: `step_checkpoints`/`rollbacks`/`project_locks`, `rolled_back_by` |
| API | `GET …/rollback/preview`, `GET …/rollback/history`, `POST …/commands/rollback` |
| UI | кнопка «↶ Откатить» на завершённом листе графа + `RollbackPreviewModal` + история |

Дизайн-документ: [docs/plans/2026-06-07-step-rollback.md](docs/plans/2026-06-07-step-rollback.md).

---

## 7. Cookbook — где трогать, чтобы добавить X

### Новая методология

1. `templates/methodologies/<name>.yaml` — стадии, produces, rules.
   Образец: `process.lean_jtbd.yaml`.
2. `if:` — маленькая грамматика AST-эвалюатора: литералы, имена с
   точечными путями, операторы, функции `len/count/max/min/sum/is_null`.
   Описание — `methodology_rule_eval.py`, тесты — `test_methodology_rule_eval.py`.
3. `povgen registry validate` — структурные проверки.
4. Активация: `POST /api/projects/:id/commands/set-methodology`.

### Новый domain pack

1. `templates/domains/<area>/<name>.yaml` — сигналы + contributes в слоты.
2. Все упомянутые `task_template` должны существовать.
3. Слоты — только из `templates/vocabularies/slot_*.yaml`.
4. Спека: [09_domain_packs.md](specs/09_domain_packs.md).

### Новый human_approval gate

Образец: `templates/gates/common/client_requirements_signoff.yaml`.
Подключить в `objective.done_when.gates`.
Тест: `tests/test_human_approval_gate.py`.

### Новый LLM-провайдер

1. `infrastructure/<name>_client.py` с `chat_json(system, user, schema) -> dict`.
2. Зарегистрировать в `execution_service.execute_task` (switch по `active_provider`).
3. (Опц.) Branch в `clarification_service._build_draft` для CE11.

### Новый harness-адаптер (агент-исполнитель)

1. `infrastructure/harness/providers/<name>.py` — подкласс `SandboxHarnessProvider`;
   переопредели `_build_command` (как запустить агент-CLI), при нужде `_prepare`
   (напр. git baseline) и `_harvest` (сбор результата: по соглашению или diff).
2. Зарегистрируй в `infrastructure/harness/registry.py` (`_ADAPTER_BUILDERS` +
   `ADAPTER_CAPABILITIES`).
3. Выбор адаптера — из настроек (`/machine-room`) или env `POV_HARNESS_PROVIDER`;
   ничего в шаблонах задач менять не нужно (`executor: harness` провайдер-агностичен).

### «Откуда этот вывод?»

UI: задача → drawer → панель «Рассуждение» → «Откуда это» →
`ProvenanceViewer` показывает стадии, сработавшие правила, кандидатов,
execution_run, provider, model, context_manifest. Для узлов-агентов
(`provider = harness:*`) рядом — `HarnessProvenanceViewer`: адаптер, brief,
транскрипт, результаты гейтов.

---

## 8. Anti-patterns

| Не делай | Почему |
|---|---|
| Описывать стадии рассуждения в `task_template` | R8/TS9: это работа `methodology_pack` |
| `recipe` / `recipe_fragment` | устаревшая терминология, удалена |
| LLM задаёт вопрос пользователю напрямую | CE1: только через `ClarificationCandidate` |
| Мутация артефакта после создания | EC4: исправление = новый артефакт |
| `regex` / `exec` в `if:` правил | AST-эвалюатор whitelist'ит узлы; `eval()` молча вернёт False |
| Зашивать ответ модели в Python | Если нужны defaults — `default_assumption` на кандидате |

---

## 9. Harness-исполнители (второй бэкенд исполнения)

Узел задачи с `executor: harness` исполняется автономным агентом в песочнице —
второй бэкенд за тем же контрактом артефакта, что LLM. Полный дизайн —
`docs/plans/2026-06-07-harness-runtime.md`.

**Слои** (`infrastructure/harness/`, зеркало `infrastructure/llm/`):
- `protocol.py` — контракт `HarnessProvider` (`run(spec) -> result`), `HarnessGate`/`GateResult`.
- `sandbox.py` — `SandboxRuntime` (Docker / in-memory stub); контейнер ephemeral,
  egress deny-by-default, cgroup-лимиты; `SandboxSpec.volume` — общий том группы.
- `providers/base.py` — `SandboxHarnessProvider`: общий жизненный цикл
  (провижн → посев brief+входов → `_prepare` → команда → гейты → сбор → teardown).
  Адаптеры (`claude_code`, `aider`, `command`) — тонкие специализации.
- `registry.py` — резолв адаптера из `HarnessConnection` (настройки/env), матрица
  возможностей; `gates.py` — прогон DoD-гейтов; `slots.py`/`budget.py`/`capacity.py`
  — класс конкуррентности и governance-лимиты.

**Цикл «спека → реализация» (Ф8)** — `objective implementation.realize`:
`component_model` → веер по компонентам → harness-узлы `component_implementation`
(код-бандл + гейты build/test) → `realization_index` (сводный манифест). Узлы
одной группы делят общий том (`volume = parent_task_id` у код-узлов).

**Наблюдаемость** — страница `/machine-room` (Docker/ёмкость/слоты/бюджет +
онбординг + выбор адаптера); провенанс прогона (brief/транскрипт/гейты) —
в `ArtifactMetadata.harness_trace` и drill-down карточки артефакта.

**Инварианты**: секреты (креды модели) не хранятся — подаются в песочницу
эфемерно; деньги/время — внутренние лимиты прогона, не оценки заказчику; без
Docker всё деградирует на `stub` (CI зелёный без Docker).

---

## 10. Roadmap

В порядке убывания важности:

1. **DAG методологии** вместо линейной последовательности стадий — для условных переходов.
2. **CLI scaffold** для bootstrap новой задачи / методологии / domain.
3. **Несколько активных методологий на проект** (PS10 ограничивает MVP).
4. **Цепочки objective** (ТЗ → архитектура → реализация). Сегодня один проект = один objective.
5. **Multi-objective `ProjectManifest`** — пока `objective_ref` singular; чтобы в одном workspace получить и ТЗ, и архитектуру, нужны два workspace'а.
6. **Архитектурный реестр рисков** — `common.project_risk_register` ещё требует артефактов ТЗ-потока (`normalized_request`, `solution_option_inventory`, `constraint_inventory`), поэтому помечен `required: false` в архитектурном композите; без ТЗ документ опускает секцию Risks. Решение — тонкая обёртка `architecture.*` с другими `requires`. (Развёртывание уже решено: `architecture.deployment_map` зависит только от `component_model`.)
7. ~~Server-side Mermaid → SVG для PDF~~ — **сделано**: `mermaid_render` рендерит диаграммы через `mmdc` и встраивает в PDF вектором (SVG через svglib, с санитизацией нулевого dash и проверкой конвертируемости) с автоматическим фоллбеком на PNG, затем на code-block. Подробности — в `CLAUDE.md`.
8. **Schema-driven рендеринг markdown** — сегодня `render_markdown` в `application/artifact_contracts.py` это hand-coded switch по `artifact_role`. Новый артефакт = новая ветка в Python.
9. **Cost tracking** токенов и денег в `ExecutionResult`.

Уже сделано (для справки): per-stage CoT mode (`stage_execution_mode: per_stage_cot`),
pre-selector сложности (`POV_COMPLEXITY_SELECTOR=on`), stub-payloads вынесены
в `templates/stub_fixtures/<artifact_role>.json`.
