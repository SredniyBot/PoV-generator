# BACKLOG — текущее состояние и roadmap

> Этот файл показывает, что закрыто и что осталось. Источник истины контрактов
> — `specs/*.md`. Для онбординга — `ARCHITECTURE.md`.

---

## 0. Снимок состояния

- Тестов: **83 passed** (`pytest -q`)
- Регистр: **valid** (5 vocabularies / 1 objective / 21 templates / 16 artifact_contracts / 4 domain_packs / 1 methodology_pack / 2 quality_gates)
- UI: `npm run build` зелёный (tsc + vite)
- CI: `.github/workflows/ci.yml` гонит pytest + registry validate + UI build

Ветка: `spec/methodology-pack-v2.1`. Базовый коммит до текущей сессии —
`8abb39d`.

---

## 1. Жёсткие правила (что нельзя нарушать)

Следствия spec. Их нарушение = ошибка.

1. **`task_template` не объявляет структуру рассуждения**. Стадии — только в
   `methodology_pack` (TS9).
2. **`methodology_pack` и `domain_pack` ортогональны**. Конфликт по полю
   `reasoning_artifact` = ошибка валидации реестра (R10, DP8).
3. **На MVP — не более одной активной методологии на проект** (PS10).
4. **Каждая leaf-задача производит ровно три артефакта**: `primary`,
   `reasoning`, `trace` (EC8).
5. **Reasoning artifact валидируется по схеме активной методологии**, а не
   `artifact_contract` (EC9). Wrapper методологии не создаёт узлов
   графа задач (EC10).
6. **Quality gate с `human_approval`** блокирует завершение objective до
   `answered` + `selected_option_ids ⊇ {"approved"}` в `ClarificationRequest`
   с `source_type=quality_gate`, `source_id=gate.ref`.
7. **`pytest -q` зелёный** перед каждым коммитом.
8. **Не использовать `recipe` / `recipe_fragment`** — устаревшая терминология.

---

## 2. Команды самопроверки

```powershell
# Установка зависимостей
.\.venv\Scripts\python -m pip install -e .[dev]

# Регрессии
.\.venv\Scripts\python -m pytest -q

# Валидация реестра
.\.venv\Scripts\povgen registry validate

# UI build
cd ui\workspace; npm run build; cd ..\..

# API + UI на 127.0.0.1:8788
.\.venv\Scripts\povgen-api
```

---

## 3. Что закрыто (с коммитами)

| # | Что | Коммит |
|---|---|---|
| 9 | Phase 3/4 + Claude clients тесты | `172043a` |
| (W0.2) | starlette pin (G12) | `44d9651` |
| (W0.1) | `methodology_trace` honest data (G2) | `b830dc4` |
| (W0.3) | CE11 для claude_sdk / claude_subscription (G6) | `9758869` |
| 8 | Реальный `human_approval` gate `client.requirements_signoff` | `9d54f87` |
| (W1.2) | engagement-level ось: `decision_owner_role` | `b1f1ccb` |
| 7 | AST-эвалюатор `if`-выражений (DSL первого класса) | `b227bbc` |
| 1 | L1 Mission Control как landing | `57375af` |
| 2 + 3 | L2 MethodologyPage + L3 ReasoningPanel + role chips | `b5b58f7` |
| 4 | L4 ProvenanceViewer с execution summary | `2baf4f4` |
| 12a | `ClarificationOption` import lift | `35bcde5` |
| G8 | `framework_summary` → `summary` (R8/TS9) | `8af905a` |
| 10 | GitHub Actions CI | `0be293c` |
| 11 | `ARCHITECTURE.md` onboarding doc | `2954d2c` |

---

## 4. Что осталось открытым

### Roadmap волны 3 — «качество мышления» (heavy)

#### W3.1 — Per-stage CoT mode methodology [HIGH]

Сейчас `stage_execution_mode: single_call` — один LLM-вызов на всю
leaf-задачу с объединённой JSON-схемой `primary + reasoning`. Это нарушает
исходный посыл проекта «сильно декомпозировать и делегировать LLM мелкие
задачи». Поле зарезервировано в спеке как `per_stage_cot`.

**Что сделать:**
- Реализовать оркестратор стадий в `execution_service`: вызов LLM на каждую
  стадию с накопительным контекстом (предыдущие стадии → следующая стадия).
- Между стадиями применять правила (rules `if:`) этой стадии, чтобы
  следующая стадия видела `ClarificationCandidate` как сигнал.
- Сохранить совместимость с `single_call` для trivial задач.

**Acceptance:**
- Новый методологический пак с `stage_execution_mode: per_stage_cot`
  — рабочий end-to-end.
- Существующие тесты на `process.lean_jtbd@1.0.0` (single_call) — зелёные.
- Минимум один тест на «стадия N видит вывод стадии N-1».

#### W3.2 — Pre-selector сложности [MEDIUM]

Сейчас `task_template.complexity` статический. Pre-selector haiku должен
оценить сложность задачи в её контексте перед запуском и при необходимости
повысить/понизить уровень модели.

**Что сделать:**
- `complexity_selector_service` с lightweight haiku-вызовом.
- Принимает `task_template` + `ContextManifest` summary.
- Возвращает `trivial | standard | complex` + rationale.
- В `execution_service.execute_task` override `complexity` через результат
  селектора (если включено через env).

**Acceptance:**
- Тест: pre-selector возвращает `complex` для задачи с многими активными
  domain pack'ами.
- Тест: pre-selector не вызывается при `POV_COMPLEXITY_SELECTOR=off`.

#### W3.3 — Stub → examples фикстуры [MEDIUM]

`execution_service._execute_stub` — 800+ строк хардкоженных payload'ов
для каждого `artifact_role`. Это симптом нерасширяемости: добавить новый
шаблон задачи = дописать stub в Python.

**Что сделать:**
- Вынести stub'ы в `templates/tasks/<area>/<task>/examples/stub.json`.
- `_execute_stub` читает фикстуру по `artifact_role`, fallback'ит на
  «не реализовано» если файла нет.
- Удалить ~800 строк из execution_service.

**Acceptance:**
- Все существующие e2e тесты зелёные.
- Добавление нового task_template НЕ требует правки Python.

#### W3.4 — `povgen scaffold` CLI [LOW]

`povgen scaffold task --area X --name Y --primary-artifact Z` → генерирует
пакет директорию task pack + skeleton YAML + примеры в `examples/`.

Аналогично `povgen scaffold methodology --id X` и
`povgen scaffold domain --id X`.

**Acceptance:** smoke-тест на каждый из трёх scaffold-команд.

### Операционная зрелость (medium)

#### #5 — CLI команды для методологии [MEDIUM]

Зеркала к REST endpoint'ам:
- `povgen registry list-methodologies`
- `povgen problem methodology-show --workspace <path>`
- `povgen problem methodology-set --workspace <path> --pack-ref <ref>`

#### #6 — WebSocket events `overview_changed` / `methodology_changed` [MEDIUM]

После `run-next` / `set-methodology` / `answer-clarification` L1 Mission
Control не обновляется без F5. Нужно расширить `changed_projections` в
command_service и подписку UI на эти событийные имена.

### Микрофиксы (low)

- **12b** — `planning_service._objective_completed` для `human_approval`
  gate должен проверять «approve свежее последнего primary артефакта».
  Иначе старое approval остаётся валидным после повторной генерации спеки.
- **12c** — `claude_subscription_client` regex-парсинг JSON ненадёжен на
  verbose-ответах. Добавить retry с self-correction (один доп. вызов
  «переформатируй в чистый JSON»).

---

## 5. Что НЕ делать (вне MVP по `00_vision.md`)

Эти направления роадмапа отложены за пределы MVP. Контракты на них зарезервированы.

- Несколько активных методологий на проект (PS10 ограничивает MVP).
- Цепочки objective (ТЗ → архитектура → реализация).
- Cost tracking токенов и денег.
- Hybrid executor (LLM + tool + human в одной задаче).
- DAG-стадии методологии вместо линейной последовательности.

---

## 6. Открытые архитектурные вопросы

(не задачи, а решения, которые стоит обсудить когда наступит время)

- **Lockfile для Python deps** (`uv.lock` / `requirements.txt` через
  pip-tools / poetry). Сейчас транзитивные `mcp → starlette` могут
  ломать setup без явного pin. После W0.2 базовый случай зафиксирован,
  но lockfile был бы полнее. Выбор инструмента — отдельное решение.
- **Linter / formatter** — нет настроенного `ruff` / `black`. Стиль кода
  в текущем виде остаётся как есть.
- **`framework_summary` → `summary`** (G8) выполнен; стоит ли в спеке
  04_task_template_semantics.md тоже переименовать примеры с `framework:`
  на `summary:` — да, желательно.

---

## 7. Финальный sanity-check после задачи

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\povgen registry validate

# Если меняли UI
cd ui\workspace; npm run build; cd ..\..
```

Только после зелёного pytest коммитить.
