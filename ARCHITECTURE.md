# Architecture

> Карта системы для разработчика, который только зашёл в репо. Источник истины
> контрактов — [`specs/`](specs/). Этот файл — гид по нему.

PoV Generator — фреймворк управляемого получения проектных артефактов (первый
сценарий — построение ТЗ). Ключевые свойства: явная методология рассуждения,
декомпозиция задач, провайдер-агностичный исполнитель, UI пирамида L1→L4
для бизнес-менеджера с настраиваемой вовлечённостью.

---

## 1. Поток одной leaf-задачи

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

## 2. Семь kinds реестра

Реестр живёт в [`templates/`](templates/). Каждый YAML — один объект одного kind.

| kind | Что описывает | Где лежит | Спека |
|---|---|---|---|
| `objective` | цель проекта: корневая задача + done_when | `templates/objectives/` | [02_registry_dsl.md](specs/02_registry_dsl.md) |
| `task_template` | тип работы (composite/leaf): входы, выход, контекст | `templates/tasks/<area>/` | [04_task_template_semantics.md](specs/04_task_template_semantics.md) |
| `artifact_contract` | JSON-schema выходного артефакта | `templates/artifacts/` | [02_registry_dsl.md](specs/02_registry_dsl.md) |
| `domain_pack` | «над чем думаем»: сигналы + расширения слотов | `templates/domains/` | [09_domain_packs.md](specs/09_domain_packs.md) |
| `methodology_pack` | «как мы думаем»: стадии + правила | `templates/methodologies/` | [02_registry_dsl.md](specs/02_registry_dsl.md) |
| `quality_gate` | `human_approval` / `external_signoff` / `automated_review` | `templates/gates/` | [08_validation_governance.md](specs/08_validation_governance.md) |
| `vocabulary` | общие словари (slot ids, readiness dims) | `templates/vocabularies/` | — |

Жёсткое правило ортогональности: `methodology_pack` («как думаем») и
`domain_pack` («над чем думаем») не смешиваются. Конфликт по
`reasoning_artifact` — ошибка валидации.

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

## 6. Cookbook — где трогать, чтобы добавить X

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

### «Откуда этот вывод?»

UI: задача → drawer → панель «Рассуждение» → «Откуда это» →
`ProvenanceViewer` показывает стадии, сработавшие правила, кандидатов,
execution_run, provider, model, context_manifest.

---

## 7. Anti-patterns

| Не делай | Почему |
|---|---|
| Описывать стадии рассуждения в `task_template` | R8/TS9: это работа `methodology_pack` |
| `recipe` / `recipe_fragment` | устаревшая терминология, удалена |
| LLM задаёт вопрос пользователю напрямую | CE1: только через `ClarificationCandidate` |
| Мутация артефакта после создания | EC4: исправление = новый артефакт |
| `regex` / `exec` в `if:` правил | AST-эвалюатор whitelist'ит узлы; `eval()` молча вернёт False |
| Зашивать ответ модели в Python | Если нужны defaults — `default_assumption` на кандидате |

---

## 8. Roadmap

В порядке убывания важности:

1. **DAG методологии** вместо линейной последовательности стадий — для условных переходов.
2. **CLI scaffold** для bootstrap новой задачи / методологии / domain.
3. **Несколько активных методологий на проект** (PS10 ограничивает MVP).
4. **Цепочки objective** (ТЗ → архитектура → реализация).
5. **Cost tracking** токенов и денег в `ExecutionResult`.

Уже сделано (для справки): per-stage CoT mode (`stage_execution_mode: per_stage_cot`),
pre-selector сложности (`POV_COMPLEXITY_SELECTOR=on`), stub-payloads вынесены
в `templates/stub_fixtures/<artifact_role>.json`.
