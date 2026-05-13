# UI API и синхронизация в реальном времени

> Статус: целевая спецификация v2.1 · 2026-05-09. Документ описывает серверный контракт, который позволяет UI показывать живое состояние проекта без сборки одного большого DTO.

## 1. Принцип

UI получает состояние проекта через несколько независимых проекций. Каждая проекция отвечает за свой характер данных и обновляется отдельно.

Это важно, потому что разные части экрана меняются с разной частотой:

- дерево задач меняется при расширении графа, смене статусов и ретраях;
- лента событий пополняется постоянно;
- артефакты меняются реже, но могут быть большими;
- панель ситуации обновляется после изменений `ProjectKnowledge` или `ProcessState`;
- данные технической диагностики нужны только при открытии деталей.

Один агрегирующий объект для всего экрана запрещен как основная модель. Он быстро становится тяжелым, плохо кэшируется и смешивает данные с разной динамикой.

## 2. Проекции

| Проекция | Назначение | Частота обновления | UI-уровень |
| --- | --- | --- | --- |
| `project_shell` | базовая информация о проекте и статус соединения | редко | L1 шапка |
| `project_overview` | агрегированный mission control: активность, прогресс, критические вопросы и эскалации, ключевые артефакты, активная методология | средне | L1 |
| `task_graph` | дерево задач, статусы, блокировки, исполнители | средне | L2 |
| `timeline` | очищенная история событий | часто | L2 |
| `situation` | сжатое состояние проекта (полное, не overview) | средне | L2 |
| `clarifications` | открытые вопросы, допущения и ответы | средне | L2 |
| `artifacts_index` | список артефактов и версий | средне | L2 |
| `methodology` | активный methodology_pack: стадии, правила, статистика применения | редко | L2 |
| `task_details` | детали выбранной задачи (включая reasoning_artifact) | по запросу | L3 |
| `artifact_content` | содержимое выбранного артефакта | по запросу | L3 |
| `methodology_trace` | трасса исполнения задачи через стадии методологии | по запросу | L4 |
| `debug_trace` | данные технической диагностики планирования, допуска и выполнения | по запросу | L4 |

Проекции не должны дублировать большие данные друг друга. Они могут ссылаться на общие идентификаторы задач, событий, артефактов и проверок.

## 3. REST API

REST используется для начальной загрузки, команд и точечного получения деталей.

Минимальные эндпоинты:

| Метод | Путь | Назначение |
| --- | --- | --- |
| `GET` | `/api/projects` | список проектов |
| `POST` | `/api/projects` | создать проект |
| `GET` | `/api/projects/{project_id}/shell` | базовая информация проекта |
| `GET` | `/api/projects/{project_id}/overview` | агрегированный L1 mission control |
| `GET` | `/api/projects/{project_id}/task-graph` | дерево задач |
| `GET` | `/api/projects/{project_id}/timeline` | события проекта |
| `GET` | `/api/projects/{project_id}/situation` | полная панель ситуации |
| `GET` | `/api/projects/{project_id}/clarifications` | открытые и недавние уточнения |
| `GET` | `/api/projects/{project_id}/clarifications/{clarification_id}` | детали уточнения |
| `GET` | `/api/projects/{project_id}/artifacts` | список артефактов |
| `GET` | `/api/projects/{project_id}/artifacts/{artifact_id}` | содержимое артефакта |
| `GET` | `/api/projects/{project_id}/methodology` | активный methodology_pack и статистика применения |
| `GET` | `/api/projects/{project_id}/tasks/{task_id}` | детали задачи (включая reasoning_artifact) |
| `GET` | `/api/projects/{project_id}/tasks/{task_id}/methodology-trace` | трасса методологии для исполнения задачи |
| `GET` | `/api/projects/{project_id}/tasks/{task_id}/debug` | техническая диагностика задачи |
| `POST` | `/api/projects/{project_id}/commands/run-next` | запустить следующий доступный шаг |
| `POST` | `/api/projects/{project_id}/commands/retry-task` | повторить задачу в ошибке |
| `POST` | `/api/projects/{project_id}/commands/answer-clarification` | ответить на уточнение |
| `POST` | `/api/projects/{project_id}/commands/accept-assumption` | принять предложенное допущение |
| `POST` | `/api/projects/{project_id}/commands/set-clarification-mode` | изменить режим участия пользователя |
| `POST` | `/api/projects/{project_id}/commands/domain-packs` | изменить активные доменные пакеты |
| `POST` | `/api/projects/{project_id}/commands/set-methodology` | сменить активный methodology_pack |

Командные эндпоинты возвращают не «успешно» по факту принятия HTTP-запроса, а бизнес-результат команды:

- `accepted` - команда принята и запущена;
- `completed` - команда сразу завершилась;
- `rejected` - команда не может быть выполнена;
- `warning` - команда обработана, но требует внимания;
- `failed` - команда завершилась ошибкой.

Если доступных задач нет, `run-next` должен вернуть `warning` или `rejected` с объяснением, а не успешное уведомление.

## 4. WebSocket

WebSocket используется для доставки изменений проекта в реальном времени.

Канал:

```text
/api/projects/{project_id}/events
```

Минимальные типы сообщений:

| Тип | Назначение |
| --- | --- |
| `snapshot_ready` | сервер сообщает, какие проекции доступны для первичной загрузки |
| `projection_changed` | изменилась одна из проекций |
| `event_appended` | в ленту добавлено новое событие |
| `task_changed` | изменился статус или данные задачи |
| `artifact_changed` | создан или обновлен артефакт |
| `situation_changed` | изменилось состояние проекта |
| `clarification_changed` | создан, изменен или закрыт вопрос пользователя |
| `methodology_changed` | сменён активный methodology_pack |
| `overview_changed` | пересчитана агрегация project_overview |
| `command_result` | появился результат пользовательской команды |
| `connection_notice` | служебное сообщение о соединении |

Сообщения должны быть короткими. Большие данные не передаются через WebSocket, а запрашиваются через REST по ссылке на проекцию или объект.

Пример:

```json
{
  "type": "task_changed",
  "project_id": "project-1",
  "task_id": "clarify-goal",
  "status": "completed",
  "projection_refs": ["task_graph", "timeline", "situation"],
  "event_id": "event-42",
  "occurred_at": "2026-05-01T12:00:00Z"
}
```

## 5. Версионирование проекций

Каждая проекция имеет версию.

UI хранит последнюю полученную версию и перезагружает только изменившиеся проекции.

Минимальные поля ответа проекции:

```json
{
  "project_id": "project-1",
  "projection": "task_graph",
  "version": 17,
  "generated_at": "2026-05-01T12:00:00Z",
  "data": {}
}
```

Если клиент пропустил события или восстановил соединение, он запрашивает актуальные версии проекций через REST.

## 6. События ленты

Лента использует отдельную человеко-читаемую модель событий.

Минимальные поля:

```json
{
  "event_id": "event-42",
  "project_id": "project-1",
  "severity": "info",
  "title": "Задача завершена",
  "message": "Система уточнила цель проекта и обновила состояние проекта.",
  "subject_type": "task",
  "subject_id": "clarify-goal",
  "actions": [
    {
      "type": "open_task",
      "label": "Открыть задачу",
      "target_id": "clarify-goal"
    }
  ],
  "occurred_at": "2026-05-01T12:00:00Z"
}
```

Допустимые уровни важности:

- `info`;
- `success`;
- `warning`;
- `error`.

События уточнений должны иметь действия:

- открыть вопрос;
- ответить;
- принять допущение;
- открыть затронутую задачу;
- открыть затронутый артефакт.

## 7. Проекция уточнений

`clarifications` содержит короткий список вопросов и допущений, нужный для панели ситуации и центра уточнений.

Минимальная структура элемента:

```json
{
  "clarification_id": "clarification-1",
  "status": "open",
  "priority": "high",
  "title": "Уточнить критерий успеха",
  "question": "Какой измеримый результат должен подтвердить успех пилота?",
  "reason": "Без этого нельзя проверить итоговое ТЗ и критерии приемки.",
  "impact": "Ответ повлияет на KPI, границы PoC/PoV и приемочные критерии.",
  "answer_mode": "single",
  "options": [],
  "recommended_option_id": null,
  "default_assumption": null,
  "blocking_scope": "subtree",
  "affected_task_ids": ["task-1"]
}
```

Большие объяснения и трассы кандидатов загружаются через детальный REST-эндпоинт.

## 7a. Проекция project_overview

Агрегированный L1-mission control. Минимальная структура:

```json
{
  "project_id": "project-1",
  "stage_summary": "Сбор требований, ждём ответа клиента",
  "current_activity": {
    "kind": "waiting_for_user",
    "message": "Ждём ответа на уточнение по DPO-согласованию",
    "since": "2026-05-09T11:30:00Z",
    "related_id": "clarification-3"
  },
  "objective_progress": {
    "artifacts_required": 5,
    "artifacts_ready": 3,
    "gates_required": 1,
    "gates_passed": 0
  },
  "critical_clarifications": [
    {
      "clarification_id": "clarification-3",
      "title": "DPO-согласование",
      "priority": "high",
      "blocking_scope": "subtree"
    }
  ],
  "active_escalations": [],
  "key_artifacts": [
    {
      "artifact_id": "artifact-12",
      "contract_ref": "common.data_sources@1.0.0",
      "title": "Источники данных",
      "status": "approved"
    }
  ],
  "active_methodology": {
    "ref": "process.lean_jtbd@1.0.0",
    "title": "JTBD-driven decision making"
  }
}
```

Пустые блоки опускаются. Правило попадания объекта в overview см. `10_ui_workspace.md` § 3.2.

## 7b. Проекция methodology

Карта активного методологического пакета для L2-вью.

```json
{
  "project_id": "project-1",
  "active_pack": {
    "ref": "process.lean_jtbd@1.0.0",
    "title": "JTBD-driven decision making",
    "status": "active",
    "stage_execution_mode": "single_call"
  },
  "stages": [
    {
      "id": "goal_framing",
      "title": "Сформулировать цель задачи",
      "produces_fields": ["declared_goal"],
      "rules_count": 1,
      "applications_count": 12,
      "candidates_emitted": 0
    }
  ],
  "rules": [
    {
      "id": "ambiguous_choice",
      "stage_id": "decision",
      "applications_count": 7,
      "fired_count": 2,
      "candidate_refs": ["clarification-3", "clarification-7"]
    }
  ]
}
```

Детали отдельной стадии или правила (включая список конкретных задач, где они сработали) загружаются через `/api/projects/{project_id}/methodology` с параметрами фильтрации.

## 8. Ошибки команд

Ошибки команд должны быть пригодны для UI.

Минимальная структура:

```json
{
  "status": "failed",
  "code": "unknown_artifact_contract",
  "title": "Неизвестный контракт артефакта",
  "message": "Задача вернула артефакт с контрактом, которого нет в реестре.",
  "task_id": "task-1",
  "retry_allowed": true,
  "user_action_required": false
}
```

Технический traceback не является основным сообщением для пользователя. Он доступен только в деталях диагностики.

## 9. Клиентская модель состояния

Клиент хранит:

- выбранный проект;
- версии загруженных проекций;
- выбранную задачу, событие или артефакт;
- открытое уточнение;
- состояние WebSocket;
- локальное состояние открытых панелей и фильтров.

Клиент не вычисляет бизнес-статусы самостоятельно. Он может только отображать статусы, полученные от сервера.

## 10. Инварианты

- Real-time сообщения не передают большие артефакты.
- REST остается источником актуального состояния после потери соединения.
- Команды возвращают бизнес-результат, а не только HTTP-статус.
- UI не собирается из одного монолитного DTO.
- Каждая проекция имеет версию.
- Каждое событие ленты связано с задачей, артефактом, проверкой, командой или системным состоянием.
- Ошибка исполнения должна быть видна пользователю и доступна для детального просмотра.
- Уточнения имеют отдельную проекцию и не смешиваются с технической диагностикой.
