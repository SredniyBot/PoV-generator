# PoV Generator

В репозитории реализован уже не только фундамент `M0–M4`, но и следующие рабочие вертикальные срезы:

- `M5`: хранилище артефактов и `Context Engine`
- `M6`: исполняющий слой (`stub` и OpenRouter)
- `M7`: первый end-to-end поток `бизнес-запрос -> уточнения -> ТЗ -> ревью`
- `M8`: базовая валидация результатов, findings и escalation
- `M9`: server-side operator surface
  - read-models под UI
  - Query API
  - Command API
  - realtime-обновления через WebSocket

Важно: это **ещё не вся целевая платформа**, но уже рабочий модуль, который можно гонять руками:

- на `stub`-исполнителе без ИИ;
- на живом OpenRouter через недорогую модель;
- с базовым `common`-recipe;
- с доменным `frontend`-расширением.

## Что система умеет сейчас

Сейчас система уже умеет:

- хранить декларативные правила в YAML;
- собирать `recipe` проекта из базового сценария и `domain pack`;
- вести `ProblemState`;
- создавать задачи по детерминированному planner'у;
- собирать минимальный контекст под конкретный шаг;
- исполнять шаг через `stub` или OpenRouter;
- сохранять артефакты, execution traces и validation runs;
- выпускать черновик ТЗ и отчёт ревью;
- честно останавливать поток при проблемах валидации.
- отдавать серверные проекции проекта для UI:
  - `shell`
  - `journey`
  - `situation`
  - `timeline`
  - `artifacts`
  - `review`
  - `state`
  - `debug`
- уведомлять UI об изменении этих проекций через WebSocket.

## Главные сущности простым языком

### Template
`Template` — это один тип локального шага.

Примеры:

- уточнить цель;
- разобрать user story;
- рассмотреть альтернативы;
- подготовить черновик ТЗ;
- провести ревью ТЗ.

### Recipe
`Recipe` — это обязательная цепочка шагов.

Для базового сценария подготовки ТЗ она сейчас такая:

1. `goal_clarification`
2. `user_story_scan`
3. `alternatives_scan`
4. `requirements_spec_generation`
5. `requirements_spec_review`

### Domain Pack
`Domain Pack` — это доменное расширение процесса.

Пример:

- `frontend.web_app_requirements@1.0.0`

Он добавляет в цепочку подготовки ТЗ дополнительный шаг:

- `frontend_user_flow_analysis`

И заставляет итоговое ТЗ содержать `frontend_requirements`.

### ProblemState
`ProblemState` — текущее формализованное понимание проекта:

- исходный запрос;
- цель;
- gaps;
- readiness;
- активные доменные пакеты;
- состав собранного recipe.

### Context Manifest
`Context Manifest` — объяснимый ответ на вопрос:

- что именно система отдала в контекст шага;
- какие поля `ProblemState` использованы;
- какие артефакты подтянуты;
- сколько токенов это заняло.

### Artifact
`Artifact` — артефакт конкретного шага.

Сейчас поддерживаются:

- `clarification_notes`
- `user_story_map`
- `alternatives_analysis`
- `ui_requirements_outline`
- `requirements_spec`
- `review_report`

## Установка

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e .[dev]
```

## Запуск из PyCharm

Рекомендуемая конфигурация:

- `Module name`: `pov_generator`
- `Working directory`: `F:\0work\python\PoV-generator`
- `Parameters`: например `registry validate`
- `Interpreter`: `F:\0work\python\PoV-generator\.venv\Scripts\python.exe`

Рабочие варианты из терминала:

```powershell
.\.venv\Scripts\povgen registry validate
.\.venv\Scripts\python -m pov_generator registry validate
.\.venv\Scripts\python src\pov_generator\__main__.py registry validate
```

## Настройка OpenRouter

Для живого запуска через ИИ система читает настройки из переменных окружения.

Минимальный набор:

```powershell
$env:POV_EXECUTION_PROVIDER = "openrouter"
$env:POV_OPENROUTER_API_KEY = "<ваш ключ>"
$env:POV_OPENROUTER_MODEL = "openai/gpt-4.1-mini"
```

По умолчанию для локальной проверки можно ничего не задавать и использовать `stub`.

## Запуск server-side API (`M9`)

### 1. Поднять API

```powershell
.\.venv\Scripts\povgen-api
```

По умолчанию сервер стартует на:

- `http://127.0.0.1:8788`

### 2. Проверить, что API жив

Откройте:

- [http://127.0.0.1:8788/api/health](http://127.0.0.1:8788/api/health)
- [http://127.0.0.1:8788/docs](http://127.0.0.1:8788/docs)

### 3. Что именно даёт API сейчас

API уже умеет отдавать раздельные серверные проекции проекта:

- `/api/projects`
- `/api/projects/{project_id}/shell`
- `/api/projects/{project_id}/journey`
- `/api/projects/{project_id}/situation`
- `/api/projects/{project_id}/timeline`
- `/api/projects/{project_id}/artifacts`
- `/api/projects/{project_id}/artifacts/{artifact_id}`
- `/api/projects/{project_id}/review`
- `/api/projects/{project_id}/state`
- `/api/projects/{project_id}/debug`

Команды:

- `/api/projects/{project_id}/commands/run-next`
- `/api/projects/{project_id}/commands/run-until-blocked`
- `/api/projects/{project_id}/commands/retry-task`
- `/api/projects/{project_id}/commands/set-goal`
- `/api/projects/{project_id}/commands/close-gap`
- `/api/projects/{project_id}/commands/set-readiness`
- `/api/projects/{project_id}/commands/enable-domain-pack`

Realtime:

- `ws://127.0.0.1:8788/ws/projects/{project_id}`

Клиент может подписаться на изменения проекций и получать сообщения вида:

- `snapshot`
- `projection_changed`

То есть UI не читает внутренние таблицы напрямую и не ждёт giant payload. Он работает с отдельными read-models и обновляет только нужные блоки экрана.

## Структура declarative layer

- [templates/templates](F:\0work\python\PoV-generator\templates\templates) — шаблоны по доменным папкам
- [templates/recipes](F:\0work\python\PoV-generator\templates\recipes) — базовые recipes
- [templates/recipe_fragments](F:\0work\python\PoV-generator\templates\recipe_fragments) — доменные расширения recipes
- [templates/domain_packs](F:\0work\python\PoV-generator\templates\domain_packs) — описания доменных пакетов
- [templates/vocabularies](F:\0work\python\PoV-generator\templates\vocabularies) — общий словарь сущностей

## Быстрый сценарий 1: полный прогон на `stub`

### 1. Проверить registry

```powershell
.\.venv\Scripts\povgen registry validate
```

### 2. Создать кейс

```powershell
.\.venv\Scripts\povgen project init `
  --workspace runtime\demo_case `
  --name "Демо: построение ТЗ" `
  --recipe common.build_requirements_spec@1.0.0 `
  --request-text "Нужно превратить сырой бизнес-запрос в качественное техническое задание."
```

### 3. Посмотреть стартовое состояние

```powershell
.\.venv\Scripts\povgen problem show --workspace runtime\demo_case
.\.venv\Scripts\povgen plan dry-run --workspace runtime\demo_case
```

### 4. Прогнать весь сценарий автоматически

```powershell
.\.venv\Scripts\povgen workflow run-until-blocked --workspace runtime\demo_case --provider stub
```

### 5. Посмотреть результат

```powershell
.\.venv\Scripts\povgen artifacts list --workspace runtime\demo_case
.\.venv\Scripts\povgen validation runs --workspace runtime\demo_case
.\.venv\Scripts\povgen execute runs --workspace runtime\demo_case
.\.venv\Scripts\povgen problem show --workspace runtime\demo_case
```

После этого в `runtime\demo_case\artifacts\` появятся:

- JSON-артефакты каждого шага;
- рядом Markdown-рендеры тех же результатов.

### 6. Посмотреть тот же кейс через API

Когда кейс уже создан, можно открыть его через server-side projections.

Сначала получите список проектов:

```powershell
Invoke-RestMethod http://127.0.0.1:8788/api/projects
```

Потом, зная `project_id`, смотрите нужные части:

```powershell
Invoke-RestMethod http://127.0.0.1:8788/api/projects/<project_id>/shell
Invoke-RestMethod http://127.0.0.1:8788/api/projects/<project_id>/situation
Invoke-RestMethod http://127.0.0.1:8788/api/projects/<project_id>/journey
Invoke-RestMethod http://127.0.0.1:8788/api/projects/<project_id>/timeline
Invoke-RestMethod http://127.0.0.1:8788/api/projects/<project_id>/artifacts
Invoke-RestMethod http://127.0.0.1:8788/api/projects/<project_id>/review
```

Это уже те read-models, на которых можно строить UI.

## Быстрый сценарий 2: тот же поток с `frontend`-доменом

### 1. Создать проект с доменным пакетом

```powershell
.\.venv\Scripts\povgen project init `
  --workspace runtime\frontend_case `
  --name "Демо: ТЗ с frontend" `
  --recipe common.build_requirements_spec@1.0.0 `
  --domain-pack frontend.web_app_requirements@1.0.0 `
  --request-text "Нужен сервис с личным кабинетом, экраном статуса и формой подачи заявки."
```

### 2. Посмотреть собранный recipe

```powershell
.\.venv\Scripts\povgen problem composition-show --workspace runtime\frontend_case
.\.venv\Scripts\povgen plan show-composed-recipe --workspace runtime\frontend_case
```

### 3. Прогнать весь поток

```powershell
.\.venv\Scripts\povgen workflow run-until-blocked --workspace runtime\frontend_case --provider stub
```

### 4. Убедиться, что домен реально повлиял на ТЗ

```powershell
.\.venv\Scripts\povgen artifacts list --workspace runtime\frontend_case
```

Вы увидите дополнительный артефакт:

- `ui_requirements_outline`

Итоговый `requirements_spec` будет содержать:

- `frontend_requirements.user_roles`
- `frontend_requirements.user_flows`
- `frontend_requirements.screens`
- `frontend_requirements.ux_constraints`

## Быстрый сценарий 3: ручная работа по шагам

Если нужно не автоматическое выполнение, а разбор каждого шага:

### 1. Создать проект

```powershell
.\.venv\Scripts\povgen project init `
  --workspace runtime\manual_case `
  --name "Ручной сценарий" `
  --recipe common.build_requirements_spec@1.0.0 `
  --request-text "Нужно подготовить ТЗ по новому сервису."
```

### 2. Выбрать следующий шаг

```powershell
.\.venv\Scripts\povgen plan apply --workspace runtime\manual_case
.\.venv\Scripts\povgen tasks list --workspace runtime\manual_case
```

### 3. Построить контекст для задачи

```powershell
.\.venv\Scripts\povgen context build --workspace runtime\manual_case --task-id <task-id>
```

### 4. Исполнить задачу

```powershell
.\.venv\Scripts\povgen execute task --workspace runtime\manual_case --task-id <task-id> --provider stub
```

### 5. Посмотреть traces и валидацию

```powershell
.\.venv\Scripts\povgen execute traces --workspace runtime\manual_case
.\.venv\Scripts\povgen validation runs --workspace runtime\manual_case
```

### 6. Завершить шаг автоматически через workflow

Обычно удобнее использовать `workflow run-next`, потому что он делает полный цикл:

- materialize
- execute
- validate
- apply patches
- complete task

```powershell
.\.venv\Scripts\povgen workflow run-next --workspace runtime\manual_case --provider stub
```

## Запуск через OpenRouter

После настройки переменных окружения можно прогонять те же команды, но с живым исполнителем:

```powershell
.\.venv\Scripts\povgen workflow run-until-blocked --workspace runtime\live_case --provider openrouter
```

Или пошагово:

```powershell
.\.venv\Scripts\povgen execute task --workspace runtime\manual_case --task-id <task-id> --provider openrouter
```

Важно:

- system prompt и user prompt формируются автоматически;
- все финальные инструкции для LLM сейчас собираются на русском;
- ответ ожидается строго в JSON по схеме артефакта.

## Полезные команды

### Registry

```powershell
.\.venv\Scripts\povgen registry validate
.\.venv\Scripts\povgen registry show-template --template common.goal_clarification@1.0.0
.\.venv\Scripts\povgen registry show-recipe --recipe common.build_requirements_spec@1.0.0
.\.venv\Scripts\povgen registry show-fragment --fragment frontend.requirements_extension@1.0.0
.\.venv\Scripts\povgen registry show-domain-pack --domain-pack frontend.web_app_requirements@1.0.0
```

### Project / ProblemState

```powershell
.\.venv\Scripts\povgen project show --workspace runtime\demo_case
.\.venv\Scripts\povgen problem show --workspace runtime\demo_case
.\.venv\Scripts\povgen problem history --workspace runtime\demo_case
.\.venv\Scripts\povgen problem composition-show --workspace runtime\demo_case
.\.venv\Scripts\povgen problem domain-pack-enable --workspace runtime\demo_case --domain-pack frontend.web_app_requirements@1.0.0
```

### Planner / Tasks

```powershell
.\.venv\Scripts\povgen plan dry-run --workspace runtime\demo_case
.\.venv\Scripts\povgen plan apply --workspace runtime\demo_case
.\.venv\Scripts\povgen plan history --workspace runtime\demo_case
.\.venv\Scripts\povgen plan show-composed-recipe --workspace runtime\demo_case
.\.venv\Scripts\povgen tasks list --workspace runtime\demo_case
.\.venv\Scripts\povgen tasks events --workspace runtime\demo_case
.\.venv\Scripts\povgen tasks recipe-progress --workspace runtime\demo_case
```

### Artifacts / Context / Execution / Validation

```powershell
.\.venv\Scripts\povgen artifacts list --workspace runtime\demo_case
.\.venv\Scripts\povgen artifacts show --workspace runtime\demo_case --artifact-id <artifact-id>
.\.venv\Scripts\povgen context build --workspace runtime\demo_case --task-id <task-id>
.\.venv\Scripts\povgen execute task --workspace runtime\demo_case --task-id <task-id> --provider stub
.\.venv\Scripts\povgen execute runs --workspace runtime\demo_case
.\.venv\Scripts\povgen execute traces --workspace runtime\demo_case
.\.venv\Scripts\povgen validation runs --workspace runtime\demo_case
.\.venv\Scripts\povgen validation escalations --workspace runtime\demo_case
```

### Workflow

```powershell
.\.venv\Scripts\povgen workflow run-next --workspace runtime\demo_case --provider stub
.\.venv\Scripts\povgen workflow run-until-blocked --workspace runtime\demo_case --provider stub --max-steps 20
```

### Operator API (`M9`)

```powershell
Invoke-RestMethod http://127.0.0.1:8788/api/projects
Invoke-RestMethod http://127.0.0.1:8788/api/projects/<project_id>/shell
Invoke-RestMethod http://127.0.0.1:8788/api/projects/<project_id>/journey
Invoke-RestMethod http://127.0.0.1:8788/api/projects/<project_id>/situation
Invoke-RestMethod http://127.0.0.1:8788/api/projects/<project_id>/timeline
Invoke-RestMethod http://127.0.0.1:8788/api/projects/<project_id>/artifacts
Invoke-RestMethod http://127.0.0.1:8788/api/projects/<project_id>/review
Invoke-RestMethod http://127.0.0.1:8788/api/projects/<project_id>/state
Invoke-RestMethod http://127.0.0.1:8788/api/projects/<project_id>/debug

Invoke-RestMethod -Method Post http://127.0.0.1:8788/api/projects/<project_id>/commands/run-next -ContentType "application/json" -Body '{"provider":"stub"}'
Invoke-RestMethod -Method Post http://127.0.0.1:8788/api/projects/<project_id>/commands/run-until-blocked -ContentType "application/json" -Body '{"provider":"stub","max_steps":20}'
```

## Что покрыто тестами

```powershell
.\.venv\Scripts\python -m pytest -q
```

Сейчас тестами покрыты:

- валидация sample registry;
- `ProblemState` и patch-history;
- базовое планирование и recipe progress;
- влияние `frontend`-домена на состав recipe;
- сборка контекста для `requirements_spec_generation`;
- end-to-end `stub`-workflow по базовому recipe;
- end-to-end `stub`-workflow по `frontend`-recipe;
- создание escalation при провале валидации.
- Query API и read-models `M9`;
- WebSocket-уведомления об изменении серверных проекций.

## Ограничения текущего этапа

Сейчас ещё **не сделано**:

- полноценное semantic retrieval и summarization;
- repair loop после findings;
- сложные tool policies;
- UI;
- автоматическое определение нужных `domain pack` по тексту запроса;
- полноценные stage-gate и waiver-механики.

Но уже есть первый реальный рабочий модуль, который можно руками и тестами проверять на кейсах.
