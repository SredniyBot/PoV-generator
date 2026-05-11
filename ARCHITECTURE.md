# Architecture — что это, как устроено, где трогать

> Кратко: PoV Generator — фреймворк управляемого получения проектных артефактов
> (первый сценарий — построение ТЗ) с явной методологией рассуждения,
> декомпозицией задач, провайдер-агностичным исполнителем и UI пирамидой L1→L4
> для бизнес-менеджера с настраиваемой вовлечённостью.
>
> Этот документ — карта системы для разработчика, который только зашёл в репо.
> Источник истины контрактов — [`specs/`](specs/). Этот файл — гид по нему.

---

## 1. Поток одной leaf-задачи (mental model)

```
бизнес-запрос
  └─ Objective (templates/objectives/*.yaml)
       └─ Корневая задача (composite task_template)
            ├─ Дочерние задачи (composite или leaf)
            ├─ Slots ← Domain pack contributions
            └─ ...

на каждое исполнение leaf-задачи:

   ┌──────────────────────────────────────────────────────────┐
   │                  methodology wrapper                     │
   │ goal_framing → jtbd_anchor → option_generation → decision│
   │           (стадии из активного methodology_pack)         │
   └──┬───────────────────────────────────────────────────────┘
      │
      ▼
   ContextManifest (поля state + входные артефакты + summary задачи)
      │
      ▼
   Provider (stub | openrouter | claude_sdk | claude_subscription)
      │
      ▼
   3 artifact'а:                  ┌─ rules от methodology
   - primary (по artifact_contract)│  (AST evaluator of `if:` expressions)
   - reasoning (по schema methodology)
   - methodology_trace (rules_evaluated, candidates_emitted)

   Validation
      ├─ schema(primary) против artifact_contract
      ├─ schema(reasoning) против стадий methodology
      ├─ semantic checks (confidence, blocking_questions, domain expectations)
      └─ quality_gate emit candidate (если review passed)

   Координатор уточнений (ClarificationCoordinator)
      ├─ принимает ClarificationCandidate (от task / validation / methodology / gate / domain_pack)
      ├─ CE11 LLM-подготовка вопроса (description + options + confidence + decision_owner_role)
      ├─ ROLE FLOOR × clarification_mode → ask | assume | defer
      └─ persists ClarificationRequest

   Patches → ProblemState (event-sourced)

   Planner → следующая leaf-задача или blocking
```

---

## 2. Семь kinds реестра (контракт)

Реестр живёт в [`templates/`](templates/). Каждый файл — один объект одного kind.

| kind | Что описывает | Где лежит | Спека |
|---|---|---|---|
| `objective` | цель проекта: корневая задача + done_when (artifacts + gates) | `templates/objectives/` | [02_registry_dsl.md](specs/02_registry_dsl.md#L75) |
| `task_template` | тип работы (composite / leaf): входы, выход, контекст, summary | `templates/tasks/<area>/<name>/task.yaml` | [04_task_template_semantics.md](specs/04_task_template_semantics.md) |
| `artifact_contract` | JSON-schema выходного артефакта | `templates/artifacts/` | [02_registry_dsl.md](specs/02_registry_dsl.md#L186) |
| `domain_pack` | «над чем думаем»: сигналы + расширения слотов + темы уточнений | `templates/domains/` | [09_domain_packs.md](specs/09_domain_packs.md) |
| **`methodology_pack`** | **«как мы думаем»**: стадии + правила (`if:`) + complexity overrides | `templates/methodologies/` | [02_registry_dsl.md](specs/02_registry_dsl.md#L251) |
| `quality_gate` | точка остановки: `human_approval`, `external_signoff`, `automated_review` | `templates/gates/` | [02_registry_dsl.md](specs/02_registry_dsl.md#L391), [08_validation_governance.md](specs/08_validation_governance.md#L65) |
| `vocabulary` | общий словарь сущностей (slot ids, readiness dims и т.д.) | `templates/vocabularies/` | — |

Инварианты `R1..R11` и оси ортогональности (`A14`, `R10`, `DP8`) запрещают
смешивать «как думаем» (`methodology_pack`) с «над чем думаем» (`domain_pack`):
конфликт по полю `reasoning_artifact` — ошибка валидации реестра.

---

## 3. Карта модулей (где что живёт)

```
src/pov_generator/
├── domain/                     ← чистые модели (без runtime-зависимостей)
│   ├── registry.py             ─ TemplateSpec, MethodologyPackSpec, QualityGateSpec, ...
│   ├── clarifications.py       ─ ClarificationCandidate, DecisionOwnerRole
│   ├── execution.py            ─ ExecutionRequest, ExecutionResult, ExecutionOutput
│   ├── tasks.py                ─ TaskRecord, статусы
│   ├── problem_state.py        ─ ProblemState и его patches
│   ├── validation.py           ─ ValidationRun, ValidationFinding
│   └── workspace_views.py      ─ DTO для UI
│
├── application/                ← бизнес-логика, оркестрация
│   ├── registry_service.py     ─ загрузка + валидация реестра
│   ├── project_service.py      ─ init_project, set_methodology
│   ├── planning_service.py     ─ expand_graph, plan(), _objective_completed
│   ├── context_service.py      ─ сборка ContextManifest
│   ├── execution_service.py    ─ методологический wrapper + provider call
│   ├── methodology_rules.py    ─ pure-функция evaluate_methodology_rules
│   ├── methodology_rule_eval.py─ AST-эвалюатор `if:` выражений (W1.1)
│   ├── validation_service.py   ─ проверки + gate candidates
│   ├── clarification_service.py─ CE11 LLM-подготовка, role floor, action decision
│   ├── workflow_service.py     ─ run_next / run_until_blocked
│   └── workspace_query_service.py  ─ DTO для projection'ов API
│
├── infrastructure/             ← I/O, внешние SDK
│   ├── sqlite_runtime.py       ─ persistence (event log + projections)
│   ├── filesystem_registry.py  ─ YAML loader
│   ├── openrouter_client.py    ─ chat_json (system/user/schema → dict)
│   ├── claude_sdk_client.py    ─ Anthropic SDK + tool-use
│   └── claude_subscription_client.py ─ claude-agent-sdk через локальный CLI
│
└── interfaces/                 ← HTTP + CLI границы
    ├── api.py                  ─ FastAPI + WebSocket
    └── cli.py                  ─ povgen CLI
```

UI отдельной директорией:

```
ui/workspace/
├── src/
│   ├── App.tsx                 ─ роутинг, page-компоненты L1/L2/L3/L4
│   ├── api.ts                  ─ типизированный REST-клиент
│   ├── types.ts                ─ TS-зеркало domain/workspace_views
│   ├── ui.tsx                  ─ дизайн-система (кнопки, карточки, badges)
│   ├── useProjectRealtime.ts   ─ WebSocket подписка на projection_changed
│   └── styles.css              ─ CSS токены + блоки L1/L2/L3/L4
```

---

## 4. UI пирамида L1→L4

| Уровень | Компонент | Откуда данные | Маршрут |
|---|---|---|---|
| **L1** | `MissionControlPage` | `/api/projects/:id/overview` | `/projects/:id/overview` |
| **L2** | `MethodologyPage` | `/api/registry/methodology-packs` | `/projects/:id/methodology` |
| **L2** | `OverviewPage` (legacy «активность») | situation + timeline + task_graph + ... | `/projects/:id/activity` |
| **L3** | `ReasoningPanel` (в TaskNodeDetail) | `/api/projects/:id/tasks/:taskId/methodology-trace` | drawer задачи |
| **L4** | `ProvenanceViewer` (модалка) | тот же endpoint + блок `execution` | модалка из L3 / артефакта |

Менеджер при открытии проекта попадает на L1. Технические детали ниже доступны
только по явному drill-down — спека [10_ui_workspace.md](specs/10_ui_workspace.md).

---

## 5. Engagement-level: две ортогональные оси

Манипуляция «глубиной вовлечённости» менеджера — это **два** независимых поля:

| Ось | Поле | Значения | Что задаёт |
|---|---|---|---|
| Частота показа | `clarification_mode` (на проекте) + `min_participation_mode` (на кандидате) | `autopilot / balanced / control / expert` | Насколько часто бить тревогу |
| Кто владеет решением | `decision_owner_role` (на кандидате/запросе) | `business / client / methodologist / architect / data_owner / security` | Кому адресован вопрос |

Логика `_decide_action` в [clarification_service.py](src/pov_generator/application/clarification_service.py):

1. Если уверенность системы ≥ 0.72 и есть `default_assumption` → **assume**.
2. Иначе если режим менеджера ≥ `_ROLE_FLOOR[role]` (см. таблицу floor'ов в модуле) → **ask**.
3. Иначе если `default_assumption` отсутствует → всё же **ask** (нельзя молча проигнорировать).
4. Иначе → **assume**.

Floor по умолчанию: `business/client` — autopilot, `security` — balanced,
`methodologist/data_owner` — control, `architect` — expert.

Эмиттеры кандидатов сами проставляют роль (методология → `methodologist`,
gate → из `gate.approver_role` через `_normalize_decision_owner_role`,
validation findings → `business` по умолчанию). При CE11 LLM-подготовке
вопроса роль может быть **переоределена** на основе содержания —
см. `_draft_system_prompt`.

---

## 6. Cookbook — где трогать, чтобы добавить X

### Хочу новую методологию

1. Создай `templates/methodologies/<name>.yaml` со stages, produces, rules
   (см. `process.lean_jtbd.yaml` как образец).
2. Правила в `if:` пишутся на маленькой грамматике AST-эвалюатора:
   литералы, имена (с cross-stage точечными путями + неявной проекцией),
   операторы сравнения/логики/арифметики, функции
   `len/count/max/min/sum/second/is_null`. Полное описание —
   [`methodology_rule_eval.py`](src/pov_generator/application/methodology_rule_eval.py),
   тесты — [`test_methodology_rule_eval.py`](tests/test_methodology_rule_eval.py).
3. `povgen registry validate` поймает структурные ошибки.
4. Активация на проект: `POST /api/projects/:id/commands/set-methodology`
   или (планируется) `povgen problem methodology-set`.

### Хочу новый domain pack

1. `templates/domains/<area>/<name>.yaml` — сигналы + contributes в слоты.
2. Все упомянутые task_template должны существовать в реестре.
3. Использовать только слоты, которые объявлены в `templates/vocabularies/slot_*.yaml`.
4. Спека: [09_domain_packs.md](specs/09_domain_packs.md) и [DP1..DP8](specs/09_domain_packs.md#L181).

### Хочу новый human_approval gate

См. `templates/gates/common/client_requirements_signoff.yaml` как живой
образец. Подключить в `objective.done_when.gates`.
Тест-образец: [`test_human_approval_gate.py`](tests/test_human_approval_gate.py).

### Хочу новый провайдер LLM

1. Добавь клиент в `infrastructure/<name>_client.py` с методом
   `chat_json(system_prompt, user_prompt, schema) -> dict`.
2. Зарегистрируй в `execution_service.execute_task` (там switch по
   `active_provider`).
3. Добавь branch в `clarification_service._build_draft` для CE11
   (или CE11 будет работать через openrouter fallback).

### Хочу понять «откуда этот вывод»

UI: открой задачу → drawer → панель «Рассуждение» → кнопка «Откуда это»
→ ProvenanceViewer (L4) покажет стадии, сработавшие правила, кандидатов,
execution_run, provider, model, context_manifest.

CLI: пока нет, в roadmap.

---

## 7. Запустить с нуля (5 минут)

```powershell
# 1. зависимости (constraints из lockfile дают воспроизводимость)
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install -e .[dev] --constraint requirements.lock

# 2. собрать UI
cd ui\workspace; npm ci; npm run build; cd ..\..

# 3. проверить тесты + реестр
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\povgen registry validate

# 4. поднять API + UI
.\.venv\Scripts\povgen-api
# → http://127.0.0.1:8788/
```

Обновить lockfile (после правки `pyproject.toml`):

```powershell
.\.venv\Scripts\uv pip compile pyproject.toml --extra dev --output-file requirements.lock
```

Подробнее по операционным сценариям — в [README.md](README.md).
Источник истины контрактов — [`specs/`](specs/), читать в порядке
00 → 01 → 02 → 04 → 05 → 06 → 07 → 08 → 09 → 10 → 11 → 12.

---

## 8. Что точно НЕ делать (anti-patterns)

| Не делай | Почему |
|---|---|
| Не описывай стадии рассуждения в `task_template` | R8/TS9: это работа `methodology_pack` |
| Не используй `recipe` / `recipe_fragment` | Устаревшая терминология, удалена из реестра |
| Не давай LLM задавать вопрос пользователю напрямую | CE1: только через `ClarificationCandidate` |
| Не мутируй артефакт после создания | EC4: исправление = новый артефакт |
| Не пиши rule с regex/`exec` в `if:` | AST-эвалюатор whitelist'ит узлы; `eval()` молча вернёт False |
| Не зашивай ответ модели в Python | Если хочется задать defaults — `default_assumption` на кандидате |

---

## 9. Следующие архитектурные шаги (roadmap)

В порядке убывания важности (см. также BACKLOG.md):

1. **DAG методологии** (вместо линейной последовательности стадий) — для
   условных переходов.
2. **CLI scaffold** для bootstrap новой задачи / методологии / domain.
3. **Несколько активных методологий на проект** (PS10 ограничивает MVP).
4. **Цепочки objective** (ТЗ → архитектура → реализация).
5. **Cost tracking** токенов и денег в `ExecutionResult`.

Закрытые архитектурные шаги (для справки):

- **Per-stage CoT mode** (W3.1) — methodology pack теперь поддерживает
  `stage_execution_mode: per_stage_cot`. Каждая активная стадия — отдельный
  LLM-вызов с накопительным контекстом, плюс финальный вызов на primary.
- **Pre-selector сложности** (W3.2) — `complexity_selector_service`
  активируется через `POV_COMPLEXITY_SELECTOR=on` и может переопределить
  declared `template.complexity` по фактическому контексту.
- **Stub → JSON фикстуры** (W3.3) — 25 статических stub-payload'ов вынесены
  в `templates/stub_fixtures/<artifact_role>.json`. Compose-кейсы
  (requirements_spec, review_report, solution_tradeoff_matrix) остались
  в Python.
