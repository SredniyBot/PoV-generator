# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> Карта системы для разработчика — [`ARCHITECTURE.md`](ARCHITECTURE.md).
> Нормативные контракты — [`specs/`](specs/) (порядок чтения `00 → 12`).
> Этот файл — навигация и рабочие правила; детали по компонентам — в `CLAUDE.md`
> внутри каждой папки (ссылки в разделе «Карта компонентов»).

## Что это

Фреймворк управляемой подготовки проектных артефактов (первый сценарий —
коммерческое ТЗ для PoV/PoC). Вход — бизнес-запрос, выход — структурированный
документ, прошедший явное методологическое рассуждение, валидацию и точки
ручного согласования. Backend на Python (FastAPI + WebSocket), декларативный
реестр на YAML (`templates/`), SPA на React+Vite (`ui/workspace/`).

## Локальное окружение

**Python:** 3.11 (минимум по `pyproject.toml`; CI гоняет 3.11 + 3.12).  
**Пакетный менеджер:** `uv`.  
**Виртуальное окружение:** `.venv/` в корне проекта (в `.gitignore`).

**Первичная настройка:**

```bash
# Системная зависимость: pkg-config нужен для сборки pycairo
# (транзитив: xhtml2pdf → svglib → rlpycairo → pycairo)
brew install pkg-config   # cairo уже есть в macOS через Homebrew

# Создать окружение с Python 3.11
uv venv --python 3.11

# Установить все зависимости строго по lockfile
PKG_CONFIG_PATH="/opt/homebrew/lib/pkgconfig" \
  uv pip install -e ".[dev]" --constraint requirements.lock --python .venv/bin/python
```

Активация: `source .venv/bin/activate` (или использовать `.venv/bin/python` напрямую).

Обновить lockfile: `uv pip compile pyproject.toml --extra dev -o requirements.lock`.

## Команды

```bash
# Установка (lockfile фиксирует транзитивные версии)
python -m pip install -e .[dev] --constraint requirements.lock

# Запуск API + UI (http://127.0.0.1:8788/)
povgen-api                 # prod ; --reload — dev hot-reload ; --port / --host
python -m pov_generator.interfaces.api --reload   # если console scripts недоступны

# UI dev-server (vite проксирует /api и /ws на 8788) → http://127.0.0.1:5173/
# При первом запуске (нет node_modules): сначала npm --prefix ui/workspace install
# подробнее: docs/dev_startup_notes.md
npm --prefix ui/workspace run dev
npm --prefix ui/workspace run build      # tsc + vite сборка (CI гоняет это)

# Проверки (то же самое гонит CI на Linux×Windows×macOS × py3.11+3.12)
python -m pytest -q                          # все тесты
python -m pytest tests/test_foundation.py -q # один файл
python -m pytest tests/test_foundation.py::test_registry_validation_passes_for_task_graph_corpus
python -m pytest -k "clarification and provider"   # по подстроке имени
python -m ruff check src tests               # линт (line-length 120; см. pyproject)
python -m pov_generator registry validate    # целостность YAML-реестра

# CLI
povgen registry validate
povgen workflow run-until-blocked --workspace runtime/demo
povgen --help                                # = python -m pov_generator <subcommand>
```

**Перед коммитом:** `ruff check --fix && ruff format` и убедиться, что
`pytest -q` и `povgen registry validate` зелёные. `.env`, `runtime/` —
в `.gitignore`, не коммитить.

## Архитектура: поток одной leaf-задачи

```
бизнес-запрос → Objective (templates/objectives/) → корневая composite-задача
  → дочерние задачи (composite | leaf) + slots ← вклад domain pack'ов

на каждое исполнение leaf-задачи:
  methodology wrapper (стадии активного methodology_pack)
  → ContextManifest (state + входные артефакты + summary)
  → Provider (stub | openrouter | claude_sdk | claude_subscription)
  → 3 артефакта: primary + reasoning + methodology_trace
  → validation (schema + semantic + quality_gate candidate)
  → ClarificationCoordinator (candidate → ask | assume | defer)
  → patches → ProjectKnowledge (Layer A) / ProcessState (Layer B)
  → planner: следующая задача или блокировка
```

Один шаг дирижирует `application/workflow_service.run_next`; автономный прогон —
`workflow_runner_service` в фоновом потоке.

Состояние проекта — per-workspace SQLite (`infrastructure/sqlite_runtime.py`):
**snapshot-таблицы** `knowledge_snapshots` (Layer A) + `process_snapshots`
(Layer B), обновляемые применением патчей в домене; `state_events` —
**append-only аудит** (state из него НЕ пересобирается — это не event-sourcing).
Прямая мутация Layer A/B запрещена: только через `apply_*_patch` с инкрементом
`version`. Артефакт после создания неизменяем (EC4: исправление = новый артефакт
с `parent_artifact_id`). Настройки LLM — отдельная system-wide `settings.db`.

## Ключевые инварианты (нарушение = баг или ошибка валидации)

- **Ортогональность `methodology_pack` («как думаем») и `domain_pack`
  («над чем думаем»)** — не смешивать. Конфликт по `reasoning_artifact` — ошибка
  валидации. Стадии рассуждения описываются ТОЛЬКО в `methodology_pack`, никогда
  в `task_template` (R8/TS9).
- **LLM не задаёт вопросы пользователю напрямую** — только через
  `ClarificationCandidate` (CE1). Решение ask/assume/defer зависит от **visibility
  + engagement-mode** (`clarification_service._decide_action`), НЕ от confidence;
  порог уверенности (low-confidence finding) живёт в `validation_service`.
  `decision_owner_role` — информационная ось (UI/CE11), на ask/assume не влияет.
- **`if:`-правила методологий** — узкий AST-эвалюатор с whitelist узлов
  (`methodology_rule_eval.py`). Никаких `regex`/`exec`/`eval`; любая ошибка
  выражения молча → False (workflow не падает, но и опечатка не сработает).
- **Ссылки в реестре** — всегда `<id>@<semver>`, без `latest`. Контракт не
  меняется задним числом: правка → новая `version`.
- **Провайдер в основном потоке резолвится через settings-store**
  (`resolve_for_purpose`), env-переменные больше им не управляют (только bootstrap
  дефолтов). Явное имя провайдера — legacy-путь для CLI/тестов.
- Устаревшая терминология `recipe` / `recipe_fragment` удалена — не использовать.

## Карта компонентов

| Папка | Что | Детали |
|---|---|---|
| `src/pov_generator/domain/` | Чистые модели (без I/O): registry, tasks, positions, knowledge/process state | `src/pov_generator/domain/CLAUDE.md` |
| `src/pov_generator/application/` | Оркестрация: planning, context, execution, validation, clarification, workflow | `src/pov_generator/application/CLAUDE.md` |
| `src/pov_generator/infrastructure/` | I/O: SQLite-runtime, YAML-loader, LLM-провайдеры (`llm/`), шифрование настроек | `src/pov_generator/infrastructure/CLAUDE.md` |
| `src/pov_generator/interfaces/` | FastAPI+WebSocket (`api.py`, `povgen-api`), CLI (`cli.py`, `povgen`) | `src/pov_generator/interfaces/CLAUDE.md` |
| `src/pov_generator/common/` | env, errors, serialization | — |
| `templates/` | Декларативный реестр (7 kinds) | [`templates/README.md`](templates/README.md) |
| `ui/workspace/` | React+Vite SPA, пирамида L1→L4 | `ui/workspace/CLAUDE.md` |
| `tests/` | pytest-сьют (~35 файлов; stub-провайдер вместо реального LLM) | `tests/CLAUDE.md` |
| `specs/` | Нормативные контракты `00→12` | — |

## Соглашения

- Человекочитаемое — на русском; английский для имён кода, путей, логов, внешних
  API. Commit messages — английский (`feat/fix/refactor/...`).
- Не править `pyproject.toml`, `requirements.lock` без явного запроса
  (lockfile регенерируется `uv pip compile pyproject.toml --extra dev -o requirements.lock`).
- **No speculation**: сначала читать файл, потом утверждать; факт — `file:line`.
- Числовые инварианты реестра захардкожены в `tests/test_foundation.py`
  (кол-во objective/templates/domain_packs/vocabularies, 23 задачи в графе) —
  при расширении `templates/` синхронно обновлять.
