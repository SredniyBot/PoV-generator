# domain/ — чистые модели

Слой доменных моделей PoV-генератора: frozen-dataclass'ы, `Literal`-vocabularies и
чистые функции переходов состояния. Без I/O, без БД, без LLM/SDK. Зависит только от
stdlib + `..common` (errors, serialization). Используется слоем `application/*`.

ВАЖНО: имена в коде — это `*Record` / `*Spec` / `*Patch` / `*View`, а не «голые»
сущности. Почти всё `frozen=True`; изменение состояния = новый объект через функцию
перехода или `*Patch`.

## Модуль → что в нём

| Модуль | Содержимое |
| --- | --- |
| `__init__.py` | Пустой пакет-маркер (docstring `Domain entities and pure logic.`). Импортируют подмодули напрямую, реэкспорта нет. |
| `positions.py` | **Layer A, форма знания.** `Position` (frozen, `positions.py:114`) — единая форма любой единицы знания о проекте. Vocabularies: `PositionType` (fact/assumption/decision/constraint/risk, `:23`), `VisibilityLevel` (principal/architectural/technical, `:36`), `PositionScope` (`:49`), `PositionSource` (`:59`), `PositionStatus` (`:69`). `visibility_rank()` (`:87`), `PositionAlternative` (`:98`). |
| `project_knowledge.py` | **Layer A, состояние.** `ProjectKnowledge` (frozen, `project_knowledge.py:46`) — однородная коллекция `Position` по id + `version`. Проекции `active/by_type/by_visibility/...`, `goal()` (id `project.goal`, `:32`). Патчи `Upsert/Supersede/Reject/ElevateVisibility` → `KnowledgePatch` (`:168`); применение `apply_knowledge_patch` (`:179`). |
| `process_state.py` | **Layer B.** `ProcessState` (frozen, `process_state.py:132`) — динамика работы: gaps, readiness, активные domain/methodology-паки, `clarification_mode`. Записи `GapRecord/ReadinessRecord/DomainSignalRecord/Active*PackRecord`. `should_ask_user_for()` + `proactive_ask_levels()` (`:124`). Патчи → `ProcessPatch` (`:283`); `apply_process_patch` (`:300`). |
| `project_state.py` | Композит-агрегат: `ProjectState` (`project_state.py:65`) = `ProjectManifest` (иммутабельные метаданные) + `ProjectKnowledge` (A) + `ProcessState` (B). `StateEvent` (`:47`) — унифицированная запись event-log для обоих слоёв (`layer: StateLayer`). `snapshot_version` = `(knowledge.version, process.version)`. |
| `artifacts.py` | `ArtifactRecord` (frozen, `artifacts.py:137`) — first-class артефакт со `storage_path`, `ArtifactMetadata` (`:39`) и `ArtifactRelations` (`:114`). `ArtifactKind` = primary/synthesized/derived (`:23`). Метаданные несут `used_position_ids`, `reasoning`, `methodology_trace`. Также `Context{Item,Budget,Manifest}` (`:167+`). |
| `tasks.py` | `TaskRecord` (frozen, `tasks.py:26`) — узел графа задач. `TaskStatus` (9 значений, `:10`), `TaskCommand` (`:21`), `TaskOriginKind` (`:22`). Чистая стейт-машина: `apply_task_command` (`:74`), `initial_task_status` (`:68`). |
| `registry.py` | **Спеки реестра (DSL) + парсеры YAML.** `RegistrySnapshot` (`registry.py:456`) + `ObjectRef` (`:23`, `<id>@<semver>`). Спеки: `TemplateSpec` (`:175`), `ArtifactContractSpec` (`:226`), `ObjectiveSpec` (`:56`), `DomainPackSpec` (`:271`), `MethodologyPackSpec` (`:356`), `QualityGateSpec` (`:292`), `Vocabulary` (`:48`). Функции `parse_*` строят спеки из dict (raw YAML). Самый большой файл (~950 строк). |
| `clarifications.py` | `ClarificationCandidate` (frozen, `clarifications.py:38`, сырой) и `ClarificationRequest` (`:72`, показываемый/авто-решённый). `DecisionOwnerRole` (`:18`) — **информационная** ось (UI/CE11), НЕ влияет на ask/assume/defer; решает `visibility`. `ClarificationMode` (autopilot/balanced/control/expert, `:13`), `ClarificationStatus` (`:8`). |
| `execution.py` | Одна попытка LLM-исполнения leaf-задачи: `ExecutionRequest` (`execution.py:13`), `ExecutionResult` (`:42`, со встроенным каналом `methodology_candidates`), `ExecutionOutput`, `ExecutionTrace`. `ExecutionProvider`/`ExecutionStatus` (`:8`). |
| `validation.py` | Результаты валидации/governance: `ValidationRun` (`validation.py:21`), `ValidationFinding` (`:11`, `severity` + `blocking`), `EscalationTicket` (`:33`). |
| `planning.py` | Решение планировщика: `PlanningDecision` (`planning.py:25`) + `CandidateEvaluation` (`:14`) + `AdmissionCheck` (`:7`). Только записи (без логики выбора — она в `application/planning_service`). |
| `workflow_runs.py` | Async-прогон: `WorkflowRunRecord` (frozen, `workflow_runs.py:52`) — весь цикл `run_until_blocked` (pending→running→completed/failed/cancelled), `cancel_requested`. `WorkflowStepRecord` (`:36`). НЕ путать с `ExecutionRequest` (один LLM-вызов). |
| `workspace_views.py` | DTO-проекции для UI/API (~25 frozen `*View`): `Project{Shell,TaskGraph,Situation,Timeline,Clarifications,...}View`, `ArtifactDetailView`, `DecisionLogEntryView`, и т.д. Чисто read-модель, собирается в `application/workspace_query_service`. |
| `llm_settings.py` | Конфиг LLM-провайдеров (value objects): `ProviderConnection` (`llm_settings.py:97`), `ModelRouting` (`:139`, priority/enabled), `ModelAssignment` (`:161`, purpose→model). Канонические purpose-константы `ALL_PURPOSES` (`:61`). Секреты только в распакованном виде (`ProviderCredentials`). |

## Ключевые понятия и инварианты

- **Двухслойная модель состояния проекта** (spec `05_problem_state.md`):
  - Layer A = `project_knowledge.ProjectKnowledge` (знание о проекте: положения).
  - Layer B = `process_state.ProcessState` (динамика работы: gaps, readiness, паки, режим).
  - Композиция в `project_state.ProjectState` — три слоя со своими жизненными циклами,
    у каждого свой `version`. Не смешивать концерны.
- **Положение (`Position`) — единая форма знания.** Факт/допущение/решение/ограничение/
  риск различаются только полем `type`, структура одна. Группировки по type/visibility/
  scope — это проекции-методы, не отдельные хранилища. Инварианты в `__post_init__`
  (`positions.py:149`): `confidence ∈ [0,1]`; `superseded` ⇒ есть `superseded_at`;
  `rejected` ⇒ есть `rejection_reason`.
- **Event-sourcing состояния через патчи.** Прямая мутация Layer A/B запрещена:
  `apply_knowledge_patch` / `apply_process_patch` возвращают НОВЫЙ снимок с
  инкрементом `version` и обновлённым `updated_at`. `StateEvent` (`project_state.py:47`) —
  единый event-log на проект; поле `layer` различает слой. Persist event-log живёт в
  `application/infrastructure`, но форма события — здесь.
- **Visibility vs DecisionOwnerRole.** Решение «спрашивать ли пользователя» зависит ТОЛЬКО
  от `VisibilityLevel` + `clarification_mode` (`process_state.proactive_ask_levels`,
  `:116`). `DecisionOwnerRole` (`clarifications.py:18`) — чисто информационная ось для UI и
  CE11-драйвера. Не путать их. Право оспорить положение любого уровня — универсально,
  не зависит от режима.
- **Повышение видимости — только вверх.** `ElevateVisibilityPatch` требует
  `visibility_rank(new) > rank(current)` (principal=3 > architectural=2 > technical=1),
  иначе `ConflictError` (`project_knowledge.py:272`).
- **Иммутабельность артефактов и versioning.** `ArtifactRecord` frozen; новая версия —
  новая запись с `relations.parent_artifact_id` на предыдущую + `is_superseded` на старой.
  `synthesized`-артефакты держат `child_artifact_ids`. Обратные (downstream) ссылки НЕ
  хранятся — вычисляются обходом. primary-артефакт ОБЯЗАН нести
  `metadata.used_position_ids` (замыкает граф «положение → артефакт», spec этап 1.4).
- **Стейт-машины — чистые функции, не методы.** `apply_task_command` (`tasks.py:74`) и
  `apply_*_patch` принимают объект+команду и возвращают новый объект; запрещённые
  переходы → `ConflictError`. `TaskRecord` пересобирается через `{**task.__dict__, ...}`.
- **Reasoning/methodology_trace — метаданные, не отдельные артефакты** (`artifacts.py:68-80`).
  Один прогон leaf-задачи = один primary-артефакт.
- **registry.py — это и модель, и парсер.** `parse_*`-функции принимают raw dict (из YAML)
  и валидируют через `require_*`/`ValidationError`. `ObjectRef` = `<id>@<semver>`,
  semver обязателен (`parse_semver`). `MethodologyPackSpec.stages_for(...)` (`registry.py:374`)
  фильтрует стадии по `methodology_mode` + `complexity` — единственная нетривиальная
  логика в спеках.

## Зависимости

- **Наружу из domain — НЕ чистый stdlib:** `process_state.py`, `project_knowledge.py`,
  `tasks.py`, `registry.py` импортируют `..common.errors` (`ConflictError`, `NotFoundError`,
  `ValidationError`) и `..common.serialization.utc_now_iso`. Других внешних зависимостей
  нет (никаких БД/LLM/HTTP). При добавлении кода держать этот инвариант.
- **Внутри domain:** `project_state` → `process_state`, `project_knowledge`;
  `project_knowledge` → `positions`; `clarifications` → `positions` (`VisibilityLevel`);
  `execution` → `clarifications`; `tasks` → `registry` (`ObjectRef`). `workspace_views`
  самодостаточен (только dataclass'ы).
- **Кто использует:** только `application/*` (services). Активнее всего импортируется
  `domain.registry` (спеки/ObjectRef), затем `positions`/`process_state`/`project_knowledge`
  (project_service), `workspace_views` (query/command services), `llm_settings`
  (provider_settings_service). `infrastructure/` и `interfaces/` напрямую domain не
  импортируют (идут через application).

## Спеки

В репозитории есть только `specs/00_*` … `specs/12_*` (см. `specs/`).

- `specs/05_problem_state.md` (v3.0) — Layer A/B, формы положений (`Position`),
  версионирование, патчи, event-log. Прямой источник для `positions`, `project_knowledge`,
  `process_state`, `project_state`.
- `specs/12_clarification_escalation.md` — clarification candidate/request, visibility ↔
  engagement (CE12: visibility — единственная ось ask/assume/defer; `decision_owner_role`
  информационна), эскалации (`validation.EscalationTicket`).
- `specs/02_registry_dsl.md`, `specs/04_task_template_semantics.md`,
  `specs/09_domain_packs.md` — DSL спеков из `registry.py`.
- `specs/03_task_graph.md` — `tasks.TaskRecord` / стейт-машина. `specs/06_planning.md` —
  `planning.PlanningDecision`. `specs/07_execution_context.md` — `execution.*` +
  `ContextManifest`. `specs/08_validation_governance.md` — `validation.*`.
- `specs/10_ui_workspace.md` / `specs/11_ui_realtime_api.md` — `workspace_views.*`.
- Артефакты (`artifacts.ArtifactRecord`, граф связей, used_position_ids) и
  `llm_settings.*` собственной нумерованной спеки не имеют — см. docstring'и модулей и
  `ARCHITECTURE.md` (на него ссылаются docstring'и `artifacts.py`).

> Номера строк актуальны на момент написания; при расхождении доверяй коду.
