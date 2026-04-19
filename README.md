# PoV Generator: фундамент M0-M4

Сейчас в репозитории реализован не весь продукт, а **первый исполняемый фундамент системы**:

- `M0`: исполняемый декларативный слой
- `M1`: registry для vocabulary, templates, recipes, recipe fragments и domain packs
- `M2`: versioned `ProblemState` с patch-операциями и историей
- `M3`: task runtime, FSM, task events и прогресс по recipe
- `M4`: детерминированный planner с `admission-before-selection`

Важно: на этом этапе система **ещё не генерирует ТЗ через LLM**.  
Она уже умеет:

- хранить правила системы в YAML;
- собирать `recipe` проекта из базового recipe и доменных расширений;
- хранить состояние проекта как проблемы;
- показывать, почему следующий шаг допустим или заблокирован;
- материализовывать задачи и вести их lifecycle.

## Что здесь есть простыми словами

### Template
`Template` — это тип одного локального шага.

Примеры:

- уточнить цель;
- разобрать user story;
- рассмотреть альтернативы;
- подготовить черновик ТЗ;
- сделать review.

Файлово шаблоны теперь раскладываются по доменным папкам:

- [templates/templates/common](F:\0work\python\PoV-generator\templates\templates\common)
- [templates/templates/frontend](F:\0work\python\PoV-generator\templates\templates\frontend)

При этом технические `id` остаются namespaced, например `common.goal_clarification` и `frontend.user_flow_analysis`, чтобы ссылки внутри registry были стабильными.

### Recipe
`Recipe` — это базовый обязательный путь выполнения.

Например:

1. уточнить цель;
2. разобрать user story;
3. рассмотреть альтернативы;
4. подготовить черновик ТЗ;
5. провести review.

### Domain Pack
`Domain Pack` — это подключаемый доменный пакет, который:

- добавляет доменные шаблоны;
- добавляет `recipe fragments`;
- влияет на итоговый состав шагов;
- тем самым меняет путь подготовки ТЗ.

Пример в репозитории:

- `frontend.web_app_requirements@1.0.0`

Он добавляет в процесс подготовки ТЗ дополнительный шаг:

- анализ пользовательских потоков интерфейса до генерации спецификации.

### ProblemState
`ProblemState` — это текущее формализованное понимание проекта:

- исходный бизнес-запрос;
- цель;
- gaps;
- readiness;
- включённые domain packs;
- текущая композиция recipe.

### Planner
Planner не “угадывает” следующий шаг через LLM.  
Он:

- смотрит на `ProblemState`;
- берёт текущий `composed recipe`;
- проверяет обязательные предыдущие шаги;
- проверяет readiness и открытые gaps;
- объяснимо выбирает следующий допустимый шаг.

## Установка

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e .[dev]
```

## Как запускать из PyCharm

Рекомендуемый вариант:

- `Module name`: `pov_generator`
- `Working directory`: `F:\0work\python\PoV-generator`
- `Parameters`: например `registry validate`
- `Interpreter`: `.venv`

Работают и эти варианты:

```powershell
.\.venv\Scripts\povgen registry validate
.\.venv\Scripts\python -m pov_generator registry validate
.\.venv\Scripts\python src\pov_generator\__main__.py registry validate
```

## Быстрый сценарий 1: базовый проект без доменного расширения

### 1. Проверить registry

```powershell
.\.venv\Scripts\povgen registry validate
```

### 2. Создать кейс

```powershell
.\.venv\Scripts\povgen project init `
  --workspace runtime\demo_case `
  --name "Демо: базовое ТЗ" `
  --recipe common.build_requirements_spec@1.0.0 `
  --request-text "Нужно превратить сырой бизнес-запрос в качественное ТЗ."
```

### 3. Посмотреть состояние проекта

```powershell
.\.venv\Scripts\povgen problem show --workspace runtime\demo_case
.\.venv\Scripts\povgen problem history --workspace runtime\demo_case
```

### 4. Посмотреть, какой шаг planner считает следующим

```powershell
.\.venv\Scripts\povgen plan dry-run --workspace runtime\demo_case
```

В ответе будет видно:

- какие шаги сейчас рассматриваются;
- какие проверки admission прошли или не прошли;
- почему выбран именно следующий шаг;
- почему основной шаг нельзя запускать слишком рано.

### 5. Материализовать шаг

```powershell
.\.venv\Scripts\povgen plan apply --workspace runtime\demo_case
.\.venv\Scripts\povgen tasks list --workspace runtime\demo_case
```

### 6. Руками имитировать прогресс

```powershell
.\.venv\Scripts\povgen tasks transition --workspace runtime\demo_case --task-id <task-id> --command start
.\.venv\Scripts\povgen problem goal-set --workspace runtime\demo_case --text "Подготовить согласованное ТЗ."
.\.venv\Scripts\povgen problem readiness-set --workspace runtime\demo_case --dimension goal_clarity --status ready --blocking false
.\.venv\Scripts\povgen problem gap-close --workspace runtime\demo_case --gap-id unclear_goal
.\.venv\Scripts\povgen tasks transition --workspace runtime\demo_case --task-id <task-id> --command complete
.\.venv\Scripts\povgen plan dry-run --workspace runtime\demo_case
```

## Быстрый сценарий 2: проект с frontend domain pack

Этот сценарий показывает главное отличие новой архитектуры:
домен влияет не только на будущую реализацию, но уже на **подготовку ТЗ**.

### 1. Посмотреть сам доменный пакет

```powershell
.\.venv\Scripts\povgen registry show-domain-pack --domain-pack frontend.web_app_requirements@1.0.0
.\.venv\Scripts\povgen registry show-fragment --fragment frontend.requirements_extension@1.0.0
```

### 2. Создать проект сразу с frontend-паком

```powershell
.\.venv\Scripts\povgen project init `
  --workspace runtime\frontend_case `
  --name "Демо: ТЗ с frontend" `
  --recipe common.build_requirements_spec@1.0.0 `
  --domain-pack frontend.web_app_requirements@1.0.0 `
  --request-text "Нужен сервис с личным кабинетом, экраном статуса и формой подачи заявки."
```

### 3. Посмотреть, как собрался итоговый recipe

```powershell
.\.venv\Scripts\povgen problem composition-show --workspace runtime\frontend_case
.\.venv\Scripts\povgen plan show-composed-recipe --workspace runtime\frontend_case
```

Там будет видно:

- базовый recipe;
- включённый domain pack;
- подключённый fragment;
- дополнительный шаг `frontend_user_flow_analysis`.

### 4. Дойти до места, где доменный шаг начинает блокировать core-задачу

После прохождения:

- `goal_clarification`
- `user_story_scan`
- `alternatives_scan`

planner не пустит систему сразу в `requirements_spec_generation`, а сначала потребует:

- `frontend_user_flow_analysis`

То есть domain pack реально влияет на логику подготовки ТЗ.

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

### Planner

```powershell
.\.venv\Scripts\povgen plan dry-run --workspace runtime\demo_case
.\.venv\Scripts\povgen plan apply --workspace runtime\demo_case
.\.venv\Scripts\povgen plan history --workspace runtime\demo_case
.\.venv\Scripts\povgen plan show-composed-recipe --workspace runtime\demo_case
```

### Tasks

```powershell
.\.venv\Scripts\povgen tasks list --workspace runtime\demo_case
.\.venv\Scripts\povgen tasks events --workspace runtime\demo_case
.\.venv\Scripts\povgen tasks transition --workspace runtime\demo_case --task-id <task-id> --command start
.\.venv\Scripts\povgen tasks recipe-progress --workspace runtime\demo_case
```

## Что можно проверить руками уже сейчас

1. Что registry валиден и все ссылки между vocabulary, templates, recipes, fragments и domain packs корректны.
2. Что `ProblemState` хранит:
   - gaps,
   - readiness,
   - enabled domain packs,
   - recipe composition.
3. Что `plan dry-run` показывает объяснимый admission по каждому шагу.
4. Что `domain pack` действительно меняет состав `recipe`, а не остаётся просто метаданными.
5. Что planner не перескакивает к основной задаче раньше обязательных meta-шагов.

## Автотесты

```powershell
.\.venv\Scripts\python -m pytest -q
```

Тесты покрывают:

- валидацию sample registry;
- сохранение и историю `ProblemState`;
- lifecycle задач;
- планирование базового пути;
- влияние `frontend`-домена на состав recipe и выбор следующего шага.

## Ограничения текущего этапа

Сейчас ещё **не реализованы**:

- `Artifact Store` и `Context Engine`;
- `Execution Runtime` и LLM;
- реальная генерация ТЗ;
- validation/governance loops;
- UI.

То есть это **проверяемый каркас оркестрации**, а не финальный продукт.

## Структура declarative layer

- [templates/templates](F:\0work\python\PoV-generator\templates\templates) — шаблоны по доменным папкам
- [templates/recipes](F:\0work\python\PoV-generator\templates\recipes) — базовые recipes
- [templates/recipe_fragments](F:\0work\python\PoV-generator\templates\recipe_fragments) — доменные расширения recipes
- [templates/domain_packs](F:\0work\python\PoV-generator\templates\domain_packs) — описания доменных пакетов
- [templates/vocabularies](F:\0work\python\PoV-generator\templates\vocabularies) — общий словарь сущностей
