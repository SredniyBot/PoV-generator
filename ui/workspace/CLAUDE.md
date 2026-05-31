# ui/workspace/ — React+Vite SPA

Web-клиент PoV-generator: панель проекта (mission control), task graph, артефакты,
clarifications, журнал решений, настройки LLM. React 18 + Vite 6 + TypeScript (strict).
Стейт сервера — через TanStack Query; realtime — через WebSocket к backend.

## Команды

- `npm run dev` — Vite dev-сервер на порту `5173`.
- `npm run build` — сначала `tsc --noEmit -p tsconfig.app.json` (typecheck, без эмита),
  затем `vite build` → `dist/`. Type-ошибка валит билд.
- `npm run preview` — предпросмотр собранного `dist/`.

Backend ожидается на `127.0.0.1:8788`. Vite proxy (`vite.config.ts:8-17`):
`/api` → `http://127.0.0.1:8788`, `/ws` → `ws://127.0.0.1:8788` (ws: true).
В проде пути относительные (`API_BASE = ""`), переопределяются env
`VITE_API_BASE_URL` / `VITE_WS_BASE_URL` (`api.ts:25`, `api.ts:309`).

## Структура src/

CSS подключается только через `main.tsx:6` → `import "./styles.css"`. Сам `styles.css`
в начале файла `@import`-ит остальное в фиксированном порядке (`styles.css:1-7`):
Google Fonts → `tokens.css` → `overview-v2.css` → `home-dash.css` → `create-form.css`
→ `decision-log.css` → `settings-page.css`, дальше идут собственные правила styles.css.
Отдельно `TaskGraphCanvas.tsx:40` импортит css ReactFlow.

- `main.tsx` — bootstrap: `QueryClientProvider` (staleTime 15s, refetchOnWindowFocus off,
  retry 1) → `<App/>` в `StrictMode`; единственная точка импорта `styles.css`.
- `App.tsx` — ~4215 строк, монолит: роутинг + большинство страниц (Mission Control,
  Activity, Clarifications, Methodology, Reasoning, Artifacts, TaskGraph, State, Review,
  Debug) определены здесь как локальные компоненты. Точки входа:
  - роутинг: `App.tsx:225` (`<BrowserRouter>`), верхний уровень `:278-301`,
    вложенный `WorkspaceRoute` `:497-557`.
  - realtime-подписка: `App.tsx:435` (`useProjectRealtime`), список проекций
    `REALTIME_PROJECTIONS` `:85-98`, ключ инвалидации `projectionKey()` `:132`.
- `api.ts` — типизированный REST-клиент (объект `api`, `:42-303`) + `createProjectSocket`
  (`:305`) для WS. Все типы импортируются из `types.ts`.
- `types.ts` — DTO/view-типы backend-проекций (`*View`), `ProjectionName`, `WsMessage`.
- `ui.tsx` — переиспользуемые UI-примитивы: `cx`, `formatDateTime`, `prettyLabel`,
  `StatusPill`, `Button`, `IconButton`, `SectionCard`, `EmptyState`, `LoadingPanel`,
  `Modal` (`:227`), `Drawer` (`:257`), `ProjectRail`, `WorkspaceTabs` (`:354`),
  `ConnectionBadge`, `WorkspaceHeader`, `CommandBar` (`:510`), `TaskGraphTree`,
  `SituationPanel`, `TimelineFeed`, `ArtifactRail`. `WorkspaceTabs` фиксирует видимые вкладки проекта:
  Обзор / Вопросы / Задачи / Артефакты / Журнал решений / Методология (`ui.tsx:360-366`)
  — остальные роуты (state/review/debug) доступны только по прямому URL.
- `useProjectRealtime.ts` — hook WS-подписки. Открывает сокет на `projectId`, на
  `projection_changed` дёргает `onProjectionChanged`; авто-reconnect через 1800ms
  (`:77`); статусы `idle|connecting|connected|degraded`.
- `ProjectOverviewV2.tsx` — экран `overview` (goal-anchored mission control, L6-1).
  Экспорт `ProjectOverviewV2`.
- `ProjectsHomeDashboard.tsx` — корневой экран `/`: проекты сгруппированы по статусу
  на клиенте (attention/running/ready/other), сигнал `has_blockers` + `status_label`.
- `TaskGraphCanvas.tsx` — визуализация task graph через `@xyflow/react` + `@dagrejs/dagre`
  (layout TB). Кастомный нод `taskCard`, цвет ребра по `origin_kind`, клик → `onSelectNode`.
- `DecisionLogPage.tsx` — экран `decisions` (журнал решений, L6-5), фильтры
  all/answered/assumed/auto.
- `LlmSettingsPage.tsx` — `/settings`: провайдеры / модели / assignments по purpose +
  диагностика резолва.
- CSS: `tokens.css` (CSS-переменные — палитра, см. ниже), `styles.css` (~94KB, основной),
  `overview-v2.css`, `decision-log.css`, `create-form.css`, `settings-page.css`,
  `home-dash.css`.

## Связь с backend

REST через `api.*` (все пути с префиксом `/api`). Группы эндпоинтов:
- проекты/реестр: `/api/projects`, `/api/registry/{objectives,domain-packs,methodology-packs}`.
- проекции проекта (read): `shell`, `task-graph`, `situation`, `timeline`,
  `clarifications`, `artifacts`, `review`, `state`, `debug`, `overview`.
- команды (POST `/api/projects/:id/commands/*`): `run-next`, `run-until-blocked` (async,
  возвращает `WorkflowRunView`), `cancel-workflow`, `retry-task`, `set-goal`, `close-gap`,
  `set-readiness`, `enable-domain-pack`, `answer-clarification`, `accept-assumption`,
  `set-clarification-mode`, `defer/reopen-clarification`, `set-methodology`.
- workflow runs: `/workflow-runs/active`, `/workflow-runs/:runId`, `/workflow-runs?limit=`.
- L6 design (06/10/11 specs): `methodology-trace`, `artifacts/:id/skeleton`, `decisions`,
  `artifact-versions`, `failure-pins`; PDF — `artifacts/:id/download.pdf` (`api.artifactPdfUrl`).
- settings: `/api/settings/{purposes,providers,models,routings,assignments,diagnostics}`.

Realtime: `createProjectSocket(projectId, projections)` (`api.ts:305`) открывает
`/ws/projects/:projectId?projections=...`. Сервер шлёт `{type:"projection_changed",
projection}`; `useProjectRealtime` транслирует в инвалидацию React Query —
тяжёлый polling не используется.

## Пирамида L1→L4 (по маркерам в коде)

Уровни помечены комментариями в `App.tsx` / `ProjectOverviewV2.tsx`:
- L1 Mission Control — `ProjectOverviewV2.tsx` (route `overview`, активный) + legacy
  `MissionControlPage` (`App.tsx:836`, route `mission`). Доминирующая проекция `overview`.
- L2 Methodology / Clarifications — `MethodologyPage` (`App.tsx:1489`, route `methodology`)
  и `ClarificationsPage` (`App.tsx:1009`).
- L2/L3 Activity — route `activity` рендерит `OverviewPage` (legacy/fallback, `App.tsx:513-534`).
- L3 Reasoning — `ReasoningPanel` (`App.tsx:1639`): показывает `reasoning_artifact` задачи
  через `getMethodologyTrace`.
- L4 Provenance — `ProvenanceViewer` (`App.tsx:1710`), открывается кнопкой из ReasoningPanel
  внутри `Drawer` "Provenance / откуда это" (`App.tsx:1682,1698-1702`). Нижний уровень —
  источники/провенанс (methodology pack, стадии, правила).

Маппинг route↔уровень подтверждён по комментариям-маркерам и роутингу; точные
строки секций внутри `ProjectOverviewV2.tsx` могут смещаться — доверяй коду.

## Gotchas

- `App.tsx` — гигант (~4215 строк); большинство «страниц» живут внутри него, не отдельными
  файлами. Перед добавлением — грепай по имени компонента.
- Цвета/spacing не хардкодить — только токены из `tokens.css` (любой px/rem/hex в компонентах
  считается багом, `tokens.css:7-9`).
- `tokens.css` подгружается ПЕРВЫМ (`styles.css:2`), правила самого `styles.css` идут после
  всех `@import` и переопределяют feature-css.
- `run-until-blocked` асинхронный: `maxSteps=1000` это намеренный «sanity ceiling», не лимит
  (`api.ts:80-95`); прогресс читается через `getActiveWorkflowRun`.
- Пустые `provider`/`model` в командах отправляются как `undefined`, чтобы backend пошёл
  через settings-store resolve, а не legacy env-path (`api.ts:76-78`).
- Проектные мутации шлют команды и полагаются на WS-инвалидацию; локально стейт не
  оптимистично-обновляется.
- `ProjectsHomeDashboard` группирует проекты эвристикой по `status_label` (regex ru/en) —
  это клиентская логика, не backend-контракт.
