# 13. UI Realtime Integration and M10 Requirements

## Назначение

Этот документ описывает:

- как UI должен работать с уже реализованным `M9` backend;
- как использовать REST и WebSocket без giant payload;
- как организовать клиентское состояние;
- каких серверных расширений не хватает для целевого `M10` UX.

---

## Базовый принцип

UI работает не с внутренними таблицами ядра, а с независимыми read-models.

Следовательно:

- initial state загружается по REST;
- изменения приходят через WebSocket как сигналы инвалидации;
- UI перечитывает только изменившуюся проекцию.

Это принципиально важно для:

- realtime-поведения;
- высокой отзывчивости интерфейса;
- чистой архитектуры;
- предсказуемого клиентского кода.

---

## Доступные проекции M9

Сейчас backend отдаёт:

- `shell`
- `journey`
- `situation`
- `timeline`
- `artifacts`
- `review`
- `state`
- `debug`

### Их природа обновления

#### `shell`

Редко меняется.

Примеры:

- изменилось название;
- обновилась цель;
- подключился domain pack;
- изменился общий статус.

#### `journey`

Меняется умеренно.

Примеры:

- сменился текущий шаг;
- шаг завершён;
- шаг заблокирован;
- обновился composed recipe.

#### `situation`

Меняется часто.

Это главная оперативная проекция overview.

Примеры:

- изменился primary action;
- появилась блокировка;
- проект перешёл из “идёт выполнение” в “требуется внимание”.

#### `timeline`

Append-heavy проекция.

Меняется почти каждый раз, когда в проекте происходит смысловое событие.

#### `artifacts`

Меняется при появлении новых артефактов или обновлении их статусов.

#### `review`

Меняется при выполнении review и при будущих repair-циклах.

#### `state`

Меняется при патчах `ProblemState`.

#### `debug`

Тяжёлая operator-проекция.
Не должна обновляться слишком агрессивно на каждом экране.

---

## REST-модель клиента

### Overview page initial load

UI должен загружать параллельно:

- `shell`
- `journey`
- `situation`
- `timeline`
- `artifacts`
- `review`
- `state`

`debug` не должен грузиться на overview автоматически.

### Detail pages

#### Artifact page

- `artifacts`
- `artifact detail`

#### Review page

- `review`

#### State page

- `state`

#### Debug page

- `debug`

---

## WebSocket-модель

### Endpoint

`WS /ws/projects/{project_id}`

### Поведение клиента

1. Открыть соединение после initial REST load.
2. Сохранить `snapshot` signatures.
3. Слушать `projection_changed`.
4. По событию инвалидации перечитать только соответствующую REST-проекцию.

### Почему не слать весь payload через WS

Потому что:

- разные зоны экрана меняются с разной частотой;
- payload может быть тяжёлым;
- timeline и debug могут быстро расти;
- giant push state усложняет клиент и сервер.

Правильная роль WS:

- не быть транспортом всего состояния;
- быть **сигнальным каналом** об изменении проекций.

---

## Предлагаемая клиентская модель состояния

### Разделение store

Клиент должен делить состояние на:

- server state;
- UI state.

### Server state

Хранится в query layer.

Нужны отдельные query-ключи:

- `project-shell`
- `project-journey`
- `project-situation`
- `project-timeline`
- `project-artifacts`
- `project-review`
- `project-state`
- `project-debug`

### UI state

Локальное состояние интерфейса:

- какой drawer открыт;
- какая карточка timeline раскрыта;
- какой artifact preview активен;
- включён ли operator mode;
- открыт ли mobile sheet.

UI state не должен смешиваться с server state.

---

## Realtime UX-поведение

### При обновлении `situation`

- обновить Situation Panel;
- если изменилась блокировка или primary action, мягко подсветить блок;
- при необходимости обновить status в header.

### При обновлении `journey`

- обновить Journey Strip;
- мягко анимировать смену текущего шага.

### При обновлении `timeline`

- догрузить новые записи после последней sequence;
- добавить их сверху;
- подсветить новые элементы;
- при наличии скрытых старых записей показать мягкий индикатор “есть новые события”.

### При обновлении `artifacts`

- обновить только artifact rail и counters;
- если появился новый ключевой артефакт, разрешён мягкий toast.

### При обновлении `review`

- обновить review summary;
- если появились новые findings, это должно быть заметно на overview и в разделe `Замечания`.

### При обновлении `debug`

- не дёргать overview;
- обновлять только если открыт debug view.

---

## Политика частичной инвалидации

UI не должен автоматически перечитывать всё подряд.

### Правила

- `shell` может быть инвалидирован вместе с `situation`, но не обязан;
- `timeline` обновляется отдельно;
- `debug` лениво;
- `artifacts/{id}` инвалидация зависит от списка `artifacts`;
- если action изменил только `state`, не нужно повторно тянуть `debug`.

---

## Командная модель UI

Команды не должны вызываться “втихую”.

Каждая серверная команда должна иметь в UI:

- источник вызова;
- понятную формулировку;
- подтверждение там, где действие рискованно;
- локальный pending state;
- обработку success/error;
- список изменившихся проекций.

### Уже существующие команды M9

- `run-next`
- `run-until-blocked`
- `retry-task`
- `set-goal`
- `close-gap`
- `set-readiness`
- `enable-domain-pack`

### UI-правило

После успешной команды UI должен:

1. локально зафиксировать pending -> success;
2. инвалидировать только те проекции, которые сервер вернул в `changed_projections`;
3. дождаться realtime или сделать точечный refetch.

---

## Что не хватает для M10

Чтобы UX соответствовал проектной модели, backend на `M10` должен добавить новые проекции и команды.

### 1. Interactions projection

Нужна новая проекция:

- `interactions`

Она должна содержать:

- открытые запросы на уточнение;
- approvals;
- decision requests;
- статус ответа;
- blocking flag;
- очередность.

### 2. Interaction detail

Нужен detail endpoint:

- вопросы;
- контекст;
- рекомендуемый ответный формат;
- связь с этапом проекта.

### 3. Interaction commands

Нужны команды:

- `answer-interaction`
- `approve`
- `decline`
- `request-human-resolution`

### 4. Repair cycle projection

Нужна отдельная проекция:

- `repair`

Она должна показывать:

- активный repair cycle;
- unresolved findings;
- номер итерации;
- статус re-review;
- accepted risks.

### 5. Repair commands

Нужны команды:

- `start-repair`
- `re-run-review`
- `accept-risk`
- `cancel-repair`

### 6. Новые timeline events

Для целевого UX нужны осмысленные события:

- clarification requested;
- clarification answered;
- approval requested;
- approval granted;
- repair started;
- repair completed;
- risk accepted;
- interaction expired or cancelled.

### 7. Расширение `situation`

Проекция `situation` должна уметь выражать:

- ожидание ответа пользователя;
- ожидание approval;
- активный repair cycle;
- accepted risk path;
- отдельные причины блокировки по interaction/repair.

---

## Серверные требования к формулировкам

Чтобы UI оставался чистым, backend должен продолжать отдавать человекочитаемые поля:

- `headline`
- `summary`
- `title`
- `label`
- `detail_view`

Frontend не должен преобразовывать:

- `needs_changes` -> “Есть замечания”
- `validation_failed` -> “Требуется внимание”

Эти преобразования должны жить серверно.

---

## Технические требования к realtime

### Надёжность

UI должен переживать:

- временную потерю WS;
- повторное подключение;
- пропуск одного или нескольких событий;
- ситуацию, когда REST уже обновился, а WS ещё нет;
- ситуацию, когда WS пришёл, а REST временно недоступен.

### Политика recovery

При reconnect клиент должен:

1. переподключиться;
2. получить новый `snapshot`;
3. сравнить signatures;
4. выполнить selective refetch.

### Таймауты и деградация

Если WS недоступен:

- интерфейс остаётся рабочим;
- пользователь видит мягкий статус “обновления в реальном времени недоступны”;
- ручное обновление должно оставаться доступным.

---

## Рекомендации по frontend stack

Для будущей реализации рекомендуется:

- `React + TypeScript`
- query layer уровня `TanStack Query`
- отдельный лёгкий UI store для локального состояния
- headless primitives для сложных контролов
- motion layer только для локальных важных переходов
- CSS variables как основа design tokens

Требование к frontend-архитектуре:

- не смешивать REST-клиент, WS-клиент и UI-компоненты в один слой;
- иметь отдельный projection client;
- иметь отдельный realtime adapter;
- хранить mapping `projection -> query invalidation` централизованно.

---

## Итог

Для корректного UI M9 уже даёт хороший фундамент:

- раздельные проекции;
- командный API;
- realtime-сигналы.

Для M10 нужно добавить:

- interactions;
- repair cycle;
- новые команды;
- новые смысловые timeline events;
- более богатую `situation`.

Это даст интерфейсу не только наблюдаемость, но и полноценный управляемый пользовательский процесс.
