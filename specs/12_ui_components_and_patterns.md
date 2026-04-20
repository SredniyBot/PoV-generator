# 12. UI Components and Interaction Patterns

## Назначение

Этот документ описывает:

- какие UI-компоненты нужны системе;
- как они ведут себя в основных сценариях;
- что открывается inline, в drawer или на отдельном экране;
- как выглядят empty/loading/error/live states;
- как компоненты должны работать вместе.

---

## Компонентные уровни

Компоненты делятся на 4 уровня:

1. `Foundation`
2. `Layout`
3. `Domain UI`
4. `Operator UI`

---

## 1. Foundation

Базовые атомы интерфейса.

### Button

Варианты:

- `primary`
- `secondary`
- `ghost`
- `danger`
- `inline action`

Правила:

- на экране один визуально доминирующий `primary`;
- secondary actions менее контрастные;
- destructive actions всегда с confirm.

### Input

Варианты:

- text input;
- textarea;
- search field;
- inline editable field.

### Status Chip

Используется для:

- статуса проекта;
- статуса шага;
- severity замечания;
- live-статуса действия.

Обязательные semantic варианты:

- neutral
- active
- success
- warning
- danger
- muted

### Badge

Только для коротких количественных маркеров:

- число замечаний;
- число новых событий;
- число незакрытых вопросов.

### Tabs

Используются для secondary navigation внутри проекта.

Нельзя использовать tabs внутри tabs.

### Skeleton

Обязателен для:

- project list;
- overview blocks;
- timeline;
- artifact reader.

---

## 2. Layout components

### App Shell

Содержит:

- левую rail навигации;
- main content;
- optional contextual rail.

### Project Rail

Показывает:

- список проектов;
- индикатор активного проекта;
- признак блокировки;
- краткий статус.

На узких экранах превращается в drawer.

### Project Header

Содержит:

- название;
- короткое описание;
- status chip;
- updated timestamp;
- domain pack chips;
- quick actions.

### Section Card

Базовый контейнер для смысловых блоков.

Варианты:

- normal
- highlighted
- warning
- danger
- elevated

### Side Rail

Контекстная боковая колонка.

Используется для:

- key artifacts;
- summary cards;
- compact review panel.

На tablet/mobile складывается вниз или в drawer.

---

## 3. Domain UI components

### Journey Strip

Горизонтальный маршрут проекта.

Содержит шаги:

- completed;
- current;
- pending;
- blocked.

Обязательные элементы шага:

- title;
- status;
- optional source marker:
  - base;
  - fragment;
  - domain pack.

При клике открывает step detail.

### Situation Panel

Главная карточка состояния проекта.

Содержит:

- headline;
- summary;
- primary action;
- optional secondary actions;
- blocker preview.

Это самая важная карточка overview.

### Timeline Feed

Вертикальная лента ключевых событий.

Формат элемента:

- заголовок;
- короткое описание;
- статус;
- timestamp;
- link to detail;
- optional quick action.

#### Варианты записи

- info
- progress
- artifact_created
- review_warning
- blocked
- user_action
- system_action

#### Поведение

- новые элементы появляются сверху;
- после realtime-обновления элемент кратко подсвечивается;
- длинные серии событий допускается группировать.

### Timeline Group

Группа событий за одну “сессию работы”.

Примеры:

- “Автоматический прогон требований”
- “Ревью и остановка на замечаниях”

### Artifact Card

Содержит:

- роль артефакта;
- название;
- created_at;
- validation badge;
- open action.

Если артефакт ключевой, карточка визуально крупнее.

### Artifact Reader

Главный компонент для чтения документа.

Режимы:

- `Документ`
- `Структура`
- `Проверки`
- `Происхождение`

По умолчанию открывается `Документ`.

### Review Summary Card

Содержит:

- итоговый статус ревью;
- число замечаний;
- краткий summary;
- основное действие.

### Review Issue List

Список findings.

Элемент списка:

- severity;
- текст замечания;
- связь с артефактом/разделом;
- действия.

### State Snapshot Card

Компактная выжимка `ProblemState`.

Содержит:

- goal;
- топ-3 активных gaps;
- readiness summary;
- active domain packs.

### State Detail Sections

Внутри `Состояния` должны быть отдельные секции:

- Цель
- Пробелы
- Readiness
- Допущения
- Facts
- Состав recipe

---

## 4. Operator UI components

### Debug Panel

Секция технических деталей.

Компоненты внутри:

- task table;
- task events stream;
- planning decisions list;
- execution trace list;
- context manifest list;
- validation runs;
- escalations.

По умолчанию этот слой скрыт глубже.

### Context Manifest Card

Показывает:

- task_id;
- template_ref;
- сколько элементов вошло в контекст;
- budget usage.

### Execution Trace Viewer

Показывает:

- вход;
- метаданные запуска;
- модель/провайдера;
- статус;
- ответ;
- ошибки.

### Planning Decision Card

Показывает:

- выбранный шаг;
- причины;
- blockers;
- admission result.

---

## Паттерны взаимодействия

## 1. One primary action

Для каждого workspace-state:

- есть один главный action;
- он расположен в Situation Panel;
- его label всегда должен быть деловым глаголом.

Примеры:

- `Открыть замечания`
- `Ответить на уточнения`
- `Открыть ТЗ`
- `Продолжить выполнение`

---

## 2. Progressive disclosure

UI раскрывается по уровням:

1. Обзор
2. Деталь сущности
3. Техническая глубина

Пользователь не должен видеть глубинный слой, пока он ему не нужен.

---

## 3. Где открывать детали

### Inline expand

Только для коротких пояснений и превью.

### Drawer

Использовать для:

- карточки timeline event;
- карточки шага;
- короткого state detail;
- технической справки.

### Full view

Использовать для:

- artifact reader;
- полноценный review;
- full journey;
- debug page.

---

## 4. Loading patterns

### Initial page load

Использовать structured skeleton, повторяющий финальную композицию.

### Partial reload

При обновлении одной проекции:

- не блокировать весь экран;
- показывать локальный loading state только в изменяемом блоке;
- использовать subtle state change indicator.

### Realtime update

При приходе нового `projection_changed`:

- обновлять только соответствующий блок;
- если обновление важно, подсвечивать его локально;
- не показывать навязчивый toast без необходимости.

---

## 5. Empty states

Каждый раздел должен иметь clean empty state.

Примеры:

- нет артефактов;
- ревью ещё не выполнено;
- нет debug data;
- нет domain pack расширений.

Empty state должен:

- объяснять, почему данных нет;
- говорить, появятся ли они позже;
- не выглядеть как ошибка.

---

## 6. Error states

Нужно различать:

- пользовательскую блокировку;
- системную ошибку;
- отсутствие данных;
- временную проблему realtime.

### Пользовательская блокировка

Показывается деловым языком:

- “Процесс остановлен: есть замечания”

### Системная ошибка

Показывается с recovery action:

- “Не удалось обновить данные”
- `Повторить`

### Потеря realtime-соединения

Нужен мягкий баннер:

- “Обновления в реальном времени временно недоступны. Данные можно обновить вручную.”

---

## 7. Notifications

Уведомления должны быть редкими.

Разрешены:

- успешно выполнено действие;
- открыто новое уточнение;
- появился новый артефакт;
- завершился review;
- восстановлено realtime-соединение.

Не использовать toast для каждого системного шага.

---

## Паттерны M10

Ниже зафиксированы будущие UI-паттерны, которые нужно заложить уже сейчас.

### Interaction Request Card

Используется для:

- уточняющих вопросов;
- approval;
- required decision.

Содержит:

- title;
- summary;
- список вопросов;
- severity/blocking;
- форму ответа;
- deadline или urgency при необходимости.

### Repair Cycle Panel

Используется для M10 review/repair loop.

Содержит:

- текущий статус цикла;
- unresolved findings;
- history итераций;
- next recommended action.

### Accepted Risk Pattern

Отдельный вид подтверждения для случаев, где замечание не исправляется, а принимается как риск.

Нужен:

- explainable summary;
- explicit confirm;
- запись в timeline.

---

## Микровзаимодействия

### Hover

Очень мягкий.

Не использовать сильное масштабирование.

### Focus

Обязательный видимый focus ring.

### Press

Короткое затемнение или уплотнение.

### Live update highlight

Краткое accent-highlight состояние после realtime change:

- timeline item;
- artifact card;
- review summary;
- status chip.

---

## Правила текста и меток

### Заголовки карточек

Короткие и предметные:

- `Текущая ситуация`
- `Ключевые артефакты`
- `Замечания ревью`
- `Состояние проекта`

### Метки статусов

Не использовать сырые значения бэкенда:

- не `needs_changes`
- а `Есть замечания`

- не `queued`
- а `Ожидает выполнения`

### Технические id

На business layer не показываются.
Допускаются только в debug view.

---

## Контрольный список перед реализацией UI

Перед реализацией каждого нового экрана нужно проверить:

1. Где находится один главный action?
2. Что пользователь должен понять за первые 10 секунд?
3. Нет ли на экране лишней технической информации?
4. Какая сущность открывается в detail?
5. Как выглядит loading/empty/error/live state?
6. Что происходит на mobile?
7. Не дублируется ли смысл между overview и detail?

---

## Итог

Компонентная система должна поддерживать один базовый сценарий:

- пользователь понимает проект на overview;
- проваливается в детали только по необходимости;
- всегда видит, где произошло важное изменение;
- всегда понимает, что делать дальше.
