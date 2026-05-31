# tests/ — pytest-сьют

Pytest-сьют PoV-generator (~35 файлов `test_*.py`). Тесты бьют по слоям
`pov_generator.application` / `.infrastructure` / `.interfaces`, поднимая реальные сервисы
поверх временной SQLite-runtime и заменяя реальный LLM stub-провайдером.
Конфиг минимальный — `[tool.pytest.ini_options]` в `pyproject.toml:45`: `testpaths = ["tests"]`,
без маркеров/addopts. Тестовые зависимости: `pytest`, `httpx` (для FastAPI TestClient) — в
`[project.optional-dependencies].dev` (`pyproject.toml:28`).

## Запуск

```bash
pytest -q                                              # весь сьют
pytest tests/test_foundation.py                        # один файл
pytest tests/test_foundation.py::test_registry_validation_passes_for_task_graph_corpus  # один тест
pytest -k "clarification and provider"                 # по подстроке имени
```

(Если активна среда Poetry/uv — `poetry run pytest …` / `uv run pytest …`.)

## Фикстуры и изоляция

ВАЖНО: общего `conftest.py` НЕТ (проверено: ни в `tests/`, ни в корне). Каждый файл сам собирает
окружение через локальные helper-функции (`build_services()`, `init_workspace()` / `init_project()`),
а не через pytest-фикстуры. Из встроенных фикстур используются только `tmp_path` и `monkeypatch`.

Типовой паттерн (`test_foundation.py:19`, `test_m5_m8.py:30`, `test_m9_api.py:67`):
- `RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))` — реестр читается из
  каталога `templates/` репозитория (`REPO_ROOT = Path(__file__).resolve().parents[1]`).
  Перед использованием: `snapshot, report = registry_service.validate(); assert report.is_valid`.
- `SqliteRuntime()` — runtime без явного пути; БД временная, состояние не утекает между тестами.
- workspace создаётся под `tmp_path` (`tmp_path / "case"` или `runtime_root / "caseN"`).
- Цепочка сервисов: `ProjectService`, `PlanningService`, `ContextService`, `ExecutionService`,
  `ValidationService(... ClarificationService(runtime, provider="stub"))`, `WorkflowService`.

Изоляция LLM. Две независимые механики:
1. Stub-исполнение задач: всюду передаётся `provider="stub"` (в `ExecutionService`,
   `ClarificationService`, `DomainPackSelectionService`, `WorkflowService.run_until_blocked(...)`).
   `stub` — это НЕ один из LLM-провайдеров реестра, а fixture-замена: статические payload'ы лежат
   в `templates/stub_fixtures/*.json` и подставляются через `ExecutionService._load_stub_fixture(...)`
   с подстановкой placeholder'ов `{{goal}}` / `{{business_request}}` (`test_stub_fixtures.py`).
   Compose-роли (`requirements_spec`, `review_report`, `solution_tradeoff_matrix`) намеренно НЕ
   имеют фикстур — собираются в Python из входных артефактов/доменных флагов.
2. Реальные LLM-провайдеры (`openrouter`, `claude_sdk`, `claude_subscription`) живут в
   `infrastructure/llm/`; тесты их НЕ вызывают по сети — либо проверяют только построение адаптера
   через env (`test_llm_provider_registry.py`, всё через `monkeypatch.setenv`), либо подменяют
   билдер: `monkeypatch.setattr(svc._llm, "_build_from_connection", lambda ...: MagicMock())`
   (`test_settings_api.py:211`). Для clarification есть локальные fake-классы, напр.
   `FakeClarificationDraftProvider` (`test_m9_api.py:29`), внедряемый через
   `ClarificationService(runtime, draft_provider=...)`.

Подмена реестра: тест копирует `templates/` в `tmp_path` через `shutil.copytree` и правит YAML
(`test_foundation.py:57`, `test_m5_m8.py:438`). Подмена поведения через env: `monkeypatch.setenv(...)`,
напр. `POV_DISABLE_TEMPLATE_CONTEXT_BUDGET` (`test_m5_m8.py:437`).

API-тесты: `fastapi.testclient.TestClient` поверх `create_app(repo_root=REPO_ROOT,
runtime_root=tmp_path/"runtime", websocket_poll_interval=...)` (`test_m9_api.py:129`).
WebSocket проверяется через `client.websocket_connect(...)` + рабочий поток-триггер
(`test_m9_api.py:176`). `test_settings_api._build_client` зачищает env LLM-ключей, чтобы
`ensure_default_settings` не создавал лишних connection'ов.

## Карта тестов по темам

- Реестр/foundation: `test_foundation.py` (валидация task-graph корпуса, expand_graph,
  идемпотентность, активация методологии, planner-decision), `test_extensibility.py`.
- End-to-end milestones: `test_m5_m8.py` (полный stub-flow до objective_completed, escalation,
  low-confidence validation, доменные пакеты меняют схему spec), `test_m9_api.py` (операторские
  проекции, retry-task, websocket, clarification через HTTP).
- Прогресс/движок/состояние: `test_process_state.py`, `test_project_state.py`,
  `test_project_knowledge.py`, `test_context_project_state.py`, `test_workflow_runner.py`,
  `test_autopilot_reevaluation.py`, `test_human_approval_gate.py`.
- Методологии и стадии: `test_methodology_rule_eval.py` (AST-эвалюатор if-правил: грамматика,
  cross-stage refs, whitelisted-функции, безопасность — `eval`/`__import__` → False),
  `test_methodology_and_clients.py`, `test_per_stage_cot.py`, `test_complexity_selector.py`,
  `test_prompt_authority.py`.
- Clarifications: `test_clarification_events.py`, `test_clarification_cross_task_dedup.py`,
  `test_clarification_source_stability.py`, `test_decision_owner_role.py`,
  `test_visibility_engagement.py`, `test_confidence_metadata.py`.
- Артефакты/merge/граф: `test_artifact_graph.py`, `test_positions.py`,
  `test_workspace_view_helpers.py`, `test_merge_execution.py`, `test_merge_strategies.py`.
- PDF/экспорт: `test_pdf_export.py`, `test_pdf_table_layout.py` (markdown→HTML→PDF через
  xhtml2pdf/reportlab, см. зависимости `pyproject.toml:21-24`; проверяется сигнатура `%PDF-`,
  MediaBox-ориентация, встроенный Unicode-subset для кириллицы).
- Провайдеры/настройки LLM: `test_llm_provider_registry.py`, `test_llm_settings_store.py`,
  `test_provider_settings_service.py`, `test_settings_api.py`, `test_env_loading.py`.
- Stub-фикстуры: `test_stub_fixtures.py` — контракт детерминированных stub-payload'ов.

## Ключевой тест целостности реестра

`test_foundation.py:43` `test_registry_validation_passes_for_task_graph_corpus` — поставляемый
корпус `templates/` обязан проходить `RegistryService.validate()` (`report.is_valid`) с фиксированными
инвариантами: 1 objective, ≥21 template, ≥16 artifact_contracts, ровно 4 domain_packs, ≥1
methodology_pack, ≥2 quality_gates, ровно 5 vocabularies. Рядом — негативный кейс
`test_registry_validation_detects_unknown_domain_slot` (`:57`: битый `contributes.to` → ошибка валидации)
и `test_planner_expands_objective_into_hierarchical_task_graph` (`:88`: ожидается ровно 23 задачи в графе).
ВНИМАНИЕ: эти числовые инварианты захардкожены — при добавлении шаблонов/доменов/задач в `templates/`
их надо синхронно обновлять.

## Что проверять при изменениях

- Правишь `templates/` (objectives, шаблоны, domain/methodology packs, vocabularies, quality gates)
  или `application/registry_service.py` → `test_foundation.py` (включая счётчики 23 задач, ≥21 шаблон и т.д.).
- Правишь stub-фикстуры (`templates/stub_fixtures/`) или `_load_stub_fixture` → `test_stub_fixtures.py`.
- Правишь реальные провайдеры (`infrastructure/llm/`) или их настройки → `test_llm_provider_registry.py`,
  `test_llm_settings_store.py`, `test_provider_settings_service.py`, `test_settings_api.py`.
  Следи, чтобы тесты не уходили в реальный LLM/сеть (всё через monkeypatch/MagicMock).
- Правишь движок (`planning_service`, `execution_service`, `workflow_service`, `validation_service`)
  → `test_m5_m8.py`, `test_workflow_runner.py`, `test_autopilot_reevaluation.py`.
- Правишь API (`interfaces/api.py`, проекции) → `test_m9_api.py`, `test_settings_api.py`.
  Помни про hardcoded инварианты (`debug.tasks >= 16`, `execution_runs >= 11`,
  `timeline.total_entries >= 12`): рост графа задач может их сдвинуть.
- Правишь методологии/правила стадий → `test_methodology_rule_eval.py`,
  `test_methodology_and_clients.py`, `test_per_stage_cot.py`.
- Правишь clarification → `test_clarification_*.py`, `test_decision_owner_role.py`,
  `test_visibility_engagement.py`.
- Правишь рендер/экспорт артефактов → `test_artifact_graph.py`, `test_pdf_export.py`,
  `test_pdf_table_layout.py`.
