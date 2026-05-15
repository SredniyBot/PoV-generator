# PoV Generator

Фреймворк управляемой подготовки проектных артефактов — в первую очередь
коммерческих ТЗ для PoV/PoC. На входе — бизнес-запрос. На выходе —
структурированный документ, прошедший методологически явное рассуждение,
валидацию и точки ручного согласования.

Подробности устройства — в [`ARCHITECTURE.md`](ARCHITECTURE.md). Контракты — в
[`specs/`](specs/). Реестр шаблонов — в [`templates/`](templates/).

---

## Требования

* **Python 3.11+**
* **Node 20+** (для сборки UI)

Работает на Linux, macOS и Windows.

---

## Установка

```bash
# 1. Виртуальное окружение
python -m venv .venv

# 2. Активировать
#   Linux / macOS:
source .venv/bin/activate
#   Windows PowerShell:
.\.venv\Scripts\Activate.ps1
#   Windows cmd:
.venv\Scripts\activate.bat

# 3. Зависимости (lockfile фиксирует версии)
python -m pip install -e .[dev] --constraint requirements.lock

# 4. Собрать UI
npm --prefix ui/workspace ci
npm --prefix ui/workspace run build

# 5. Настройка окружения (опционально)
cp .env.example .env     # отредактируйте под себя
```

`.env` в gitignore. Шаблон с описанием всех переменных — [`.env.example`](.env.example).

---

## Запуск

После активации venv:

```bash
povgen-api                       # production-режим, http://127.0.0.1:8788/
povgen-api --reload              # dev-режим с hot-reload
povgen-api --port 9000 --host 0.0.0.0
```

Эквивалентно через модуль (если console scripts недоступны):

```bash
python -m pov_generator.interfaces.api --reload
```

После запуска доступны:

| Адрес | Что |
|---|---|
| <http://127.0.0.1:8788/> | UI |
| <http://127.0.0.1:8788/docs> | Swagger / API |
| <http://127.0.0.1:8788/api/health> | Healthcheck |

### Frontend в режиме разработки

Два терминала: backend (`povgen-api`) и vite dev-server
(`npm --prefix ui/workspace run dev`). Открывать <http://127.0.0.1:5173/> —
vite сам проксирует `/api` и `/ws` на 8788.

---

## CLI

```bash
povgen registry validate                   # проверить реестр
povgen workflow run-until-blocked \
       --workspace runtime/demo            # прогнать workflow
povgen --help                              # все команды
```

Эквивалент: `python -m pov_generator <subcommand>`.

---

## Выбор LLM-провайдера

Управляется переменной `POV_EXECUTION_PROVIDER` в `.env` или окружении.
По умолчанию — `stub` (детерминированные фикстуры, без сети).

| Провайдер | Что нужно |
|---|---|
| `stub` | Ничего; локальные фикстуры в `templates/stub_fixtures/`. |
| `openrouter` | `POV_OPENROUTER_API_KEY` + опц. `POV_OPENROUTER_MODEL`. |
| `claude_sdk` | `POV_ANTHROPIC_API_KEY` (Anthropic API). |
| `claude_subscription` | Локально установленный и залогиненный `claude` CLI. |

Все доступные переменные описаны в [`.env.example`](.env.example).

---

## Разработка

```bash
python -m pytest -q                # тесты
python -m ruff check src tests     # линт
python -m pov_generator registry validate   # реестр
```

CI (`.github/workflows/ci.yml`) гонит то же самое на матрице
**Linux × Windows × macOS** × **Python 3.11 + 3.12** + сборку UI.

---

## Структура репозитория

```
src/pov_generator/   ← бэкенд (domain / application / infrastructure / interfaces)
ui/workspace/        ← React + Vite SPA (пирамида L1→L4)
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
* [`specs/`](specs/) — нормативные спецификации (порядок чтения: `00 → 12`).
