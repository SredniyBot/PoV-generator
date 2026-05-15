# PoV Generator

Фреймворк управляемой подготовки проектных артефактов — в первую очередь
коммерческих ТЗ для PoV/PoC. На входе — бизнес-запрос. На выходе — структурированный
документ, прошедший методологически явное рассуждение, валидацию и точки
ручного согласования.

Подробности устройства — в [`ARCHITECTURE.md`](ARCHITECTURE.md). Контракты — в
[`specs/`](specs/). Реестр шаблонов — в [`templates/`](templates/).

---

## Быстрый старт

Требования: **Python 3.11**, **Node 20**, Windows PowerShell.

```powershell
# 1. Зависимости (lockfile фиксирует версии)
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install -e .[dev] --constraint requirements.lock

# 2. Собрать UI
cd ui\workspace; npm ci; npm run build; cd ..\..

# 3. Запустить
.\.venv\Scripts\povgen-api
```

Или одной командой через helper (он сам соберёт UI, если `dist/` отсутствует):

```powershell
.\run_workspace.ps1            # запустить
.\run_workspace.ps1 -BuildUi   # пересобрать UI и запустить
```

После запуска:

| Адрес | Что |
|---|---|
| <http://127.0.0.1:8788/> | UI |
| <http://127.0.0.1:8788/docs> | Swagger / API |
| <http://127.0.0.1:8788/api/health> | Healthcheck |

---

## Frontend в режиме разработки

Backend в одном терминале, vite dev-server в другом:

```powershell
# терминал 1
.\.venv\Scripts\povgen-api

# терминал 2
cd ui\workspace; npm run dev
```

Открывать <http://127.0.0.1:5173/> — vite сам проксирует `/api` и `/ws` на 8788.

---

## Выбор LLM-провайдера

Провайдер задаётся через `POV_EXECUTION_PROVIDER`. По умолчанию — `stub`
(детерминированные фикстуры, без сети).

### OpenRouter

```powershell
$env:POV_EXECUTION_PROVIDER = "openrouter"
$env:POV_OPENROUTER_API_KEY = "<ключ>"
$env:POV_OPENROUTER_MODEL   = "deepseek/deepseek-chat"   # опционально
```

### Claude через Anthropic API

```powershell
$env:POV_EXECUTION_PROVIDER = "claude_sdk"
$env:POV_ANTHROPIC_API_KEY  = "<ключ>"
```

Маппинг сложности задачи на модель управляется env-переменными
`POV_CLAUDE_MODEL_TRIVIAL`, `POV_CLAUDE_MODEL_STANDARD`,
`POV_CLAUDE_MODEL_COMPLEX` (или общая `POV_CLAUDE_MODEL`).

### Claude через подписку (без API-ключа)

Требует установленного и залогиненного CLI `claude`:

```powershell
npm install -g @anthropic-ai/claude-code
claude login

$env:POV_EXECUTION_PROVIDER = "claude_subscription"
```

---

## Команды CLI

```powershell
.\.venv\Scripts\povgen registry validate           # проверить реестр
.\.venv\Scripts\povgen workflow run-until-blocked --workspace runtime\demo
```

`povgen --help` — список всех команд.

---

## Тесты

```powershell
.\.venv\Scripts\python -m pytest -q
ruff check src tests
```

CI (`.github/workflows/ci.yml`) гонит то же самое + сборку UI на каждом push'е.

---

## Структура репозитория

```
src/pov_generator/   ← бэкенд (domain / application / infrastructure / interfaces)
ui/workspace/        ← React + Vite SPA (L1→L4 пирамида)
templates/           ← декларативный реестр (objectives / tasks / artifacts /
                       domains / methodologies / gates / vocabularies)
specs/               ← нормативные контракты (00 → 12)
tests/               ← pytest-сьют
runtime/             ← локальные workspace'ы (gitignored)
```

---

## Куда смотреть дальше

* [`ARCHITECTURE.md`](ARCHITECTURE.md) — карта системы и cookbook для расширения.
* [`templates/README.md`](templates/README.md) — навигация по реестру.
* [`specs/`](specs/) — нормативные спецификации. Порядок чтения: `00 → 01 → 02
  → 04 → 05 → 06 → 07 → 08 → 09 → 10 → 11 → 12`.
