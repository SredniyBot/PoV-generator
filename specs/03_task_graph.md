# Граф задач

> **Статус:** v2.0 · черновик · 2026-05-01

Хранилище графа задач содержит дерево задач проекта во время выполнения. Это источник истины о том, какие задачи существуют, как они связаны и в каком состоянии находятся.

---

## Узел задачи

```python
class TaskNode(BaseModel):
    task_id: UUID
    project_id: UUID
    objective_ref: str
    parent_task_id: UUID | None
    template_ref: str
    template_type: Literal["composite", "leaf"]
    title: str
    status: TaskStatus
    depth: int
    origin: TaskOrigin
    stable_key: str
    attempt: int = 0
    error: TaskError | None = None
```

`origin.kind`:

- `objective_root`;
- `base_child`;
- `domain_contribution`;
- `repair`;
- `user_request`;
- `system`.

`stable_key` нужен для идемпотентности создания задач.

---

## Статусы

```text
candidate
ready
blocked
in_progress
waiting_for_children
completed
failed
skipped
obsolete
```

Смысл:

- `candidate` — задача создана, допуск к запуску еще не пересчитан;
- `ready` — листовая задача прошла допуск к запуску;
- `blocked` — запуск невозможен, причина должна быть видна;
- `in_progress` — задача выполняется;
- `waiting_for_children` — composite раскрыт и ждет подзадачи;
- `completed` — результат принят валидацией;
- `failed` — исполнение или валидация завершились ошибкой;
- `skipped` — задача пропущена с явной причиной;
- `obsolete` — задача инвалидирована.

Композитная задача обычно переходит из `candidate` в `waiting_for_children`, а не в `in_progress`.

---

## Связи

```python
class TaskEdge(BaseModel):
    from_task_id: UUID
    to_task_id: UUID
    kind: Literal["parent_child", "depends_on", "blocks", "supersedes"]
    strength: Literal["hard", "soft", "semantic"]
    reason: str
```

Разделение важно:

- `parent_child` — смысловая декомпозиция;
- `depends_on` — поток данных или обязательная предпосылка;
- `blocks` — объяснение блокировки;
- `supersedes` — перепланирование или инвалидирование.

---

## События

Минимальный журнал событий:

```text
task_created
task_expanded
domain_contribution_applied
admission_updated
task_selected
task_started
artifact_attached
validation_passed
validation_failed
task_completed
task_failed
task_skipped
task_obsoleted
task_retried
edge_created
clarification_blocked
clarification_resolved
```

События должны быть достаточно подробными для UI и технической диагностики.

---

## Раскрытие графа

Раскрытие композитной задачи:

1. Загрузить шаблон задачи.
2. Создать базовые дочерние задачи.
3. Создать связи `parent_child`.
4. Зарегистрировать slots.
5. Перевести composite в `waiting_for_children`.

Применение доменных расширений:

1. Найти активные доменные пакеты.
2. Найти слоты в раскрытом графе.
3. Создать доменные задачи и проверки качества по стабильному ключу.
4. Не создавать дубликаты при повторном пересчете.

---

## Проекции

Серверные проекции:

- `task_tree`;
- `ready_tasks`;
- `blocked_tasks`;
- `active_task`;
- `task_errors`;
- `task_artifacts`;
- `blocking_clarifications`;
- `domain_contributions`.

UI не должен строить дерево из сырых событий.

---

## Инварианты

| ID | Правило |
|---|---|
| TG1 | У проекта и цели есть одна корневая задача. |
| TG2 | Граф `parent_child` не содержит циклов. |
| TG3 | `stable_key` уникален в рамках проекта. |
| TG4 | Задача в ошибке имеет видимое пользователю описание проблемы. |
| TG5 | Повторное применение доменного расширения не создает дубль. |
| TG6 | Листовая задача не становится `completed` без валидации. |
| TG7 | Task Store не хранит порядок выполнения как маршрут. |
| TG8 | Уточнение не является задачей по умолчанию, но может быть причиной блокировки задачи. |
