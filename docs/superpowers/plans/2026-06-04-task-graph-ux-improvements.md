# Task Graph UX Improvements — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить визуальный фокус на активной задаче, контекстные действия на нодах и отображение ошибок прямо на карточке.

**Architecture:** Все изменения — в двух файлах: `TaskGraphCanvas.tsx` и `styles.css`. Callbacks из `TaskGraphPage` передаются в граф через новые props `TaskGraphCanvasProps`. Внутри компонентного дерева callbacks распределяются через React context `TaskGraphActionsCtx`, чтобы `TaskCardNode` (который объявлен вне компонента) мог их читать без изменения схемы данных нод. `useReactFlow()` используется для авто-центрирования на активной задаче.

**Tech Stack:** React 18, `@xyflow/react` v12.10.2, `@dagrejs/dagre` v3.0.0, React Query v5, TypeScript 5.8, CSS custom properties (тёмная тема проекта)

**Note:** Backend endpoint для `skip_task` не существует — кнопка Skip не реализуется. Реализуются: Retry, Open Artifacts (навигация), Go to Decisions (навигация).

---

## File Map

| Файл | Изменение |
|------|-----------|
| `ui/workspace/src/styles.css` | Добавить: `@keyframes tg-pulse`, стили баннера `.tg-banner`, меню действий `.tg-node__actions`, строки ошибки `.tg-node__error`, усиленный `.tg-node--current` |
| `ui/workspace/src/TaskGraphCanvas.tsx` | Добавить: `TaskGraphActionsCtx`, `ProgressBanner`, авто-пан в `useEffect`, props для callbacks, расширить `TaskCardNode` |
| `ui/workspace/src/App.tsx` | В `TaskGraphPage`: добавить `useNavigate`, создать callbacks, передать в `TaskGraphCanvas` |

---

## Task 1: CSS — новые стили

**Files:**
- Modify: `ui/workspace/src/styles.css:3436-3439` (`.tg-node--current`) и конец секции (после строки 3515)

- [ ] **Step 1: Заменить статичный `.tg-node--current` на анимированный**

Найти в `styles.css` (строки 3436–3439):
```css
.tg-node--current {
  outline: 2px solid rgba(120, 184, 201, 0.55);
  outline-offset: 1px;
}
```

Заменить на:
```css
.tg-node--current {
  outline: 2px solid rgba(120, 184, 201, 0.75);
  outline-offset: 2px;
  animation: tg-pulse 2s ease-in-out infinite;
}

@keyframes tg-pulse {
  0%, 100% { outline-color: rgba(120, 184, 201, 0.75); box-shadow: 0 0 0 0 rgba(120, 184, 201, 0.25); }
  50%       { outline-color: rgba(120, 184, 201, 0.35); box-shadow: 0 0 8px 3px rgba(120, 184, 201, 0.15); }
}
```

- [ ] **Step 2: Добавить стили прогресс-баннера после `.tg-handle` (строка 3515)**

```css
/* Progress banner */
.tg-banner {
  position: absolute;
  top: 0.6rem;
  left: 0.6rem;
  z-index: 10;
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.35rem 0.65rem;
  background: var(--surface-primary, rgba(28, 32, 38, 0.95));
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 0.4rem;
  font-size: 0.75rem;
  color: var(--text-primary, #e8ecf3);
  pointer-events: none;
  backdrop-filter: blur(4px);
}

.tg-banner__dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: rgba(150, 160, 180, 0.5);
  flex-shrink: 0;
}

.tg-banner__dot--active {
  background: rgba(120, 184, 201, 0.9);
  animation: tg-pulse-dot 1.6s ease-in-out infinite;
}

@keyframes tg-pulse-dot {
  0%, 100% { opacity: 1; }
  50%       { opacity: 0.4; }
}

.tg-banner__title {
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary, #9aa4b2);
}

.tg-banner__progress {
  font-weight: 600;
  color: var(--text-primary, #e8ecf3);
  white-space: nowrap;
}

/* Action menu on node hover */
.tg-node__actions {
  display: flex;
  gap: 0.3rem;
  opacity: 0;
  transition: opacity 0.15s ease;
  position: absolute;
  bottom: -2rem;
  left: 0;
  right: 0;
  justify-content: center;
  z-index: 5;
}

.tg-node:hover .tg-node__actions {
  opacity: 1;
}

.tg-action-btn {
  padding: 0.2rem 0.5rem;
  font-size: 0.68rem;
  border-radius: 0.3rem;
  border: 1px solid rgba(255, 255, 255, 0.1);
  background: var(--surface-primary, rgba(28, 32, 38, 0.95));
  color: var(--text-secondary, #9aa4b2);
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.1s ease, color 0.1s ease;
}

.tg-action-btn:hover {
  background: rgba(120, 184, 201, 0.15);
  color: var(--text-primary, #e8ecf3);
}

.tg-action-btn--danger:hover {
  background: rgba(215, 131, 131, 0.18);
  color: #e3a3a3;
}

/* Status summary on failed node */
.tg-node__error {
  font-size: 0.68rem;
  color: rgba(215, 131, 131, 0.85);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-top: -0.15rem;
}
```

- [ ] **Step 3: Убедиться, что `.tg-node` имеет `position: relative`** (нужно для абсолютного позиционирования `.tg-node__actions`).

Найти в `styles.css` (строки 3417–3429):
```css
.tg-node {
  width: 240px;
  min-height: 96px;
  background: var(--surface-primary, rgba(28, 32, 38, 0.95));
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 0.5rem;
  padding: 0.5rem 0.6rem;
  display: grid;
  gap: 0.3rem;
  cursor: pointer;
  transition: transform 0.1s ease, border-color 0.2s ease;
  font-size: 0.8rem;
}
```

Добавить `position: relative;` в блок `.tg-node`:
```css
.tg-node {
  width: 240px;
  min-height: 96px;
  background: var(--surface-primary, rgba(28, 32, 38, 0.95));
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 0.5rem;
  padding: 0.5rem 0.6rem;
  display: grid;
  gap: 0.3rem;
  cursor: pointer;
  position: relative;
  transition: transform 0.1s ease, border-color 0.2s ease;
  font-size: 0.8rem;
}
```

- [ ] **Step 4: Проверить build**

```bash
cd ui/workspace && npm run build 2>&1 | tail -20
```

Ожидаемый результат: `✓ built in` без ошибок TypeScript.

- [ ] **Step 5: Commit**

```bash
git add ui/workspace/src/styles.css
git commit -m "style(task-graph): pulse animation, banner, action menu, error line"
```

---

## Task 2: TaskGraphCanvas — context, banner, auto-pan, actions, error line

**Files:**
- Modify: `ui/workspace/src/TaskGraphCanvas.tsx` (полная замена содержимого)

- [ ] **Step 1: Заменить содержимое `TaskGraphCanvas.tsx` на новую версию**

```tsx
/**
 * ReactFlow + dagre canvas для визуализации task graph проекта (W4.2 / G1).
 *
 * Прошлый `TaskGraphTree` рендерил задачи как вложенный `<ul>` с отступами
 * — это даёт ноль визуальной информации о структуре графа и блокировках.
 * Этот компонент:
 *
 * - кладёт каждую задачу как узел-карточку (статус + название + источник);
 * - рёбра `parent_child` рисует автоматически из `TaskNodeView.parent_task_id`;
 * - layout считает dagre top-down — корневая задача наверху, leaf'ы внизу;
 * - даёт zoom/pan/MiniMap из коробки;
 * - на клик узла вызывает `onSelectNode(task)` — это открывает существующий
 *   `TaskNodeDetail` drawer (с панелью "Рассуждение" и Provenance в L4);
 * - авто-центрируется на активной задаче при каждом изменении `is_current`;
 * - показывает прогресс-баннер и меню действий на нодах.
 */

import { createContext, useContext, useEffect, useMemo } from "react";
import {
  Background,
  Controls,
  type Edge,
  Handle,
  MiniMap,
  type Node,
  type NodeProps,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
} from "@xyflow/react";
import dagre from "@dagrejs/dagre";

import "@xyflow/react/dist/style.css";

import type { TaskNodeView } from "./types";

const NODE_WIDTH = 240;
const NODE_HEIGHT = 96;

// ── Callbacks context ──────────────────────────────────────────────────────

interface TaskGraphActions {
  onRetry: (taskId: string) => void;
  onOpenArtifacts: () => void;
  onGoToDecisions: () => void;
}

const TaskGraphActionsCtx = createContext<TaskGraphActions | null>(null);

// ── Data types ─────────────────────────────────────────────────────────────

interface TaskNodeCardData extends Record<string, unknown> {
  task: TaskNodeView;
}

type FlowNode = Node<TaskNodeCardData>;

// ── Helpers ────────────────────────────────────────────────────────────────

/** flatten рекурсивного дерева TaskNodeView[] в плоский массив (включая корни). */
function flatten(tree: TaskNodeView[], acc: TaskNodeView[] = []): TaskNodeView[] {
  for (const node of tree) {
    acc.push(node);
    if (node.children?.length) flatten(node.children, acc);
  }
  return acc;
}

function buildLayout(tasks: TaskNodeView[]): { nodes: FlowNode[]; edges: Edge[] } {
  const graph = new dagre.graphlib.Graph();
  graph.setDefaultEdgeLabel(() => ({}));
  graph.setGraph({ rankdir: "TB", nodesep: 40, ranksep: 60, marginx: 20, marginy: 20 });

  for (const task of tasks) {
    graph.setNode(task.task_id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  const edges: Edge[] = [];
  for (const task of tasks) {
    if (task.parent_task_id && tasks.some((t) => t.task_id === task.parent_task_id)) {
      graph.setEdge(task.parent_task_id, task.task_id);
      edges.push({
        id: `e-${task.parent_task_id}-${task.task_id}`,
        source: task.parent_task_id,
        target: task.task_id,
        type: "smoothstep",
        animated: task.is_current,
        style: { stroke: edgeColorForOrigin(task.origin_kind) },
      });
    }
  }
  dagre.layout(graph);

  const nodes: FlowNode[] = tasks.map((task) => {
    const positioned = graph.node(task.task_id);
    return {
      id: task.task_id,
      type: "taskCard",
      data: { task },
      position: {
        x: (positioned?.x ?? 0) - NODE_WIDTH / 2,
        y: (positioned?.y ?? 0) - NODE_HEIGHT / 2,
      },
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
      draggable: false,
    };
  });
  return { nodes, edges };
}

function edgeColorForOrigin(origin: string): string {
  switch (origin) {
    case "domain_contribution":
      return "rgba(214, 173, 89, 0.5)";
    case "repair":
      return "rgba(215, 131, 131, 0.6)";
    case "user_request":
      return "rgba(168, 132, 198, 0.6)";
    default:
      return "rgba(255, 255, 255, 0.15)";
  }
}

function labelForOrigin(origin: string): string {
  switch (origin) {
    case "objective_root":   return "корень цели";
    case "base_child":       return "базовая";
    case "domain_contribution": return "domain pack";
    case "repair":           return "исправление";
    case "user_request":     return "ручная";
    case "system":           return "система";
    default:                 return origin;
  }
}

/** Кнопки действий для ноды — условия рендеринга вынесены из JSX. */
function resolveActions(task: TaskNodeView): {
  showRetry: boolean;
  showArtifacts: boolean;
  showDecisions: boolean;
} {
  return {
    showRetry:     task.retryable === true && task.status === "failed",
    showArtifacts: task.status === "completed",
    showDecisions: task.blocking_clarification_count > 0,
  };
}

// ── Custom node ────────────────────────────────────────────────────────────

function TaskCardNode({ data }: NodeProps<FlowNode>) {
  const task = data.task;
  const actions = useContext(TaskGraphActionsCtx);
  const { showRetry, showArtifacts, showDecisions } = resolveActions(task);
  const hasActions = showRetry || showArtifacts || showDecisions;

  return (
    <div
      className={`tg-node tg-node--${task.status}${task.is_current ? " tg-node--current" : ""}`}
    >
      <Handle type="target" position={Position.Top} className="tg-handle" />
      <div className="tg-node__header">
        <span className={`tg-pill tg-pill--${task.status}`}>{task.status}</span>
        <span className="tg-node__origin" title={task.origin_ref}>
          {labelForOrigin(task.origin_kind)}
        </span>
      </div>
      <div className="tg-node__title">{task.title}</div>
      {task.status === "failed" && task.status_summary ? (
        <div className="tg-node__error" title={task.status_summary}>
          {task.status_summary}
        </div>
      ) : null}
      <div className="tg-node__meta">
        <span>{task.template_type === "composite" ? "композит" : "задача"}</span>
        {task.blocking_clarification_count > 0 ? (
          <span className="tg-node__warn">⚠ {task.blocking_clarification_count}</span>
        ) : null}
      </div>
      {hasActions && actions ? (
        <div className="tg-node__actions">
          {showRetry && (
            <button
              className="tg-action-btn tg-action-btn--danger"
              onClick={(e) => { e.stopPropagation(); actions.onRetry(task.task_id); }}
            >
              Retry
            </button>
          )}
          {showArtifacts && (
            <button
              className="tg-action-btn"
              onClick={(e) => { e.stopPropagation(); actions.onOpenArtifacts(); }}
            >
              Артефакт
            </button>
          )}
          {showDecisions && (
            <button
              className="tg-action-btn"
              onClick={(e) => { e.stopPropagation(); actions.onGoToDecisions(); }}
            >
              ⚠ Решения
            </button>
          )}
        </div>
      ) : null}
      <Handle type="source" position={Position.Bottom} className="tg-handle" />
    </div>
  );
}

const nodeTypes = { taskCard: TaskCardNode };

// ── Progress banner ────────────────────────────────────────────────────────

interface ProgressBannerProps {
  tasks: TaskNodeView[];
  completedLeafTasks: number;
  totalLeafTasks: number;
}

function ProgressBanner({ tasks, completedLeafTasks, totalLeafTasks }: ProgressBannerProps) {
  if (totalLeafTasks === 0) return null;

  const currentTask = tasks.find((t) => t.is_current) ?? null;
  const isActive = currentTask !== null;

  return (
    <div className="tg-banner">
      <span className={`tg-banner__dot${isActive ? " tg-banner__dot--active" : ""}`} />
      {currentTask ? (
        <span className="tg-banner__title" title={currentTask.title}>
          {currentTask.title}
        </span>
      ) : (
        <span className="tg-banner__title">Нет активных задач</span>
      )}
      <span className="tg-banner__progress">
        {completedLeafTasks} / {totalLeafTasks}
      </span>
    </div>
  );
}

// ── Canvas props ───────────────────────────────────────────────────────────

export interface TaskGraphCanvasProps {
  tree: TaskNodeView[];
  onSelectNode?: (task: TaskNodeView) => void;
  height?: string | number;
  completedLeafTasks?: number;
  totalLeafTasks?: number;
  onRetry?: (taskId: string) => void;
  onOpenArtifacts?: () => void;
  onGoToDecisions?: () => void;
}

// ── Inner canvas (needs ReactFlowProvider ancestor) ────────────────────────

function TaskGraphCanvasInner({
  tree,
  onSelectNode,
  height = "70vh",
  completedLeafTasks = 0,
  totalLeafTasks = 0,
  onRetry,
  onOpenArtifacts,
  onGoToDecisions,
}: TaskGraphCanvasProps) {
  const tasks = useMemo(() => flatten(tree), [tree]);
  const layout = useMemo(() => buildLayout(tasks), [tasks]);
  const [nodes, setNodes, onNodesChange] = useNodesState<FlowNode>(layout.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(layout.edges);
  const { setCenter } = useReactFlow();

  const currentTaskId = useMemo(
    () => tasks.find((t) => t.is_current)?.task_id ?? null,
    [tasks],
  );

  // Sync layout → ReactFlow state when tree changes.
  useEffect(() => {
    setNodes(layout.nodes);
    setEdges(layout.edges);
  }, [layout, setNodes, setEdges]);

  // Auto-pan to the active node whenever it changes.
  useEffect(() => {
    if (!currentTaskId) return;
    const rfNode = layout.nodes.find((n) => n.id === currentTaskId);
    if (!rfNode?.position) return;
    setCenter(
      rfNode.position.x + NODE_WIDTH / 2,
      rfNode.position.y + NODE_HEIGHT / 2,
      { duration: 600, zoom: 1.2 },
    );
  }, [currentTaskId, layout.nodes, setCenter]);

  const actions: TaskGraphActions = useMemo(
    () => ({
      onRetry:         (id) => onRetry?.(id),
      onOpenArtifacts: () => onOpenArtifacts?.(),
      onGoToDecisions: () => onGoToDecisions?.(),
    }),
    [onRetry, onOpenArtifacts, onGoToDecisions],
  );

  return (
    <TaskGraphActionsCtx.Provider value={actions}>
      <div className="tg-canvas" style={{ height }}>
        <ProgressBanner
          tasks={tasks}
          completedLeafTasks={completedLeafTasks}
          totalLeafTasks={totalLeafTasks}
        />
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onNodeClick={(_, node) => {
            if (onSelectNode) onSelectNode((node.data as TaskNodeCardData).task);
          }}
          fitView
          fitViewOptions={{ padding: 0.2 }}
          proOptions={{ hideAttribution: true }}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable
        >
          <Background gap={20} size={1} color="rgba(255, 255, 255, 0.04)" />
          <Controls showInteractive={false} />
          <MiniMap
            pannable
            zoomable
            maskColor="rgba(0, 0, 0, 0.6)"
            nodeColor={(node) => {
              const status = (node.data as TaskNodeCardData)?.task?.status ?? "candidate";
              return statusFillColor(status);
            }}
          />
        </ReactFlow>
      </div>
    </TaskGraphActionsCtx.Provider>
  );
}

// ── Public export (wraps with ReactFlowProvider) ───────────────────────────

export function TaskGraphCanvas(props: TaskGraphCanvasProps) {
  return (
    <ReactFlowProvider>
      <TaskGraphCanvasInner {...props} />
    </ReactFlowProvider>
  );
}

// ── MiniMap fill colors ────────────────────────────────────────────────────

function statusFillColor(status: string): string {
  switch (status) {
    case "completed":
      return "rgba(140, 196, 153, 0.7)";
    case "in_progress":
    case "ready":
      return "rgba(120, 184, 201, 0.7)";
    case "failed":
      return "rgba(215, 131, 131, 0.85)";
    case "blocked":
      return "rgba(215, 131, 131, 0.5)";
    case "waiting_for_children":
      return "rgba(214, 173, 89, 0.6)";
    case "skipped":
    case "obsolete":
      return "rgba(150, 150, 150, 0.4)";
    default:
      return "rgba(150, 160, 180, 0.5)";
  }
}
```

- [ ] **Step 2: Проверить build**

```bash
cd ui/workspace && npm run build 2>&1 | tail -20
```

Ожидаемый результат: `✓ built in` без TypeScript-ошибок.

- [ ] **Step 3: Commit**

```bash
git add ui/workspace/src/TaskGraphCanvas.tsx
git commit -m "feat(task-graph): auto-pan, progress banner, action menu, error line"
```

---

## Task 3: App.tsx — прокинуть callbacks из TaskGraphPage

**Files:**
- Modify: `ui/workspace/src/App.tsx:2404-2446` (функция `TaskGraphPage`)

- [ ] **Step 1: Убедиться, что `useNavigate` уже импортирован в App.tsx**

```bash
grep -n "useNavigate" ui/workspace/src/App.tsx | head -5
```

Ожидаемый результат: строка вроде `import { ..., useNavigate, ... } from "react-router-dom";`. Если нет — добавить в импорт.

- [ ] **Step 2: Заменить функцию `TaskGraphPage`**

Найти в `App.tsx` (строки 2404–2446):
```tsx
function TaskGraphPage({ projectId }: { projectId: string }) {
  // W4.2 (G1): canvas-based task graph через ReactFlow + dagre.
  // Кликнул на узел → открывается drawer с тем же TaskNodeDetail,
  // что и на L2 Activity, плюс панель «Рассуждение» внутри.
  // Ни provider, ни model из UI не передаются — см. WorkspaceRoute.
  const provider = "";
  const model = "";
  const [selectedTask, setSelectedTask] = useState<TaskNodeView | null>(null);
  const taskGraphQuery = useQuery({
    queryKey: projectionKey(projectId, "task_graph"),
    queryFn: () => api.getTaskGraph(projectId),
  });
  const retryMutation = useMutation({
    mutationFn: (taskId: string) => api.retryTask(projectId, taskId, provider, model),
  });

  if (taskGraphQuery.isLoading || !taskGraphQuery.data) {
    return <LoadingPanel title="Загрузка графа задач…" />;
  }

  const data = taskGraphQuery.data;
  return (
    <SectionCard
      title="Граф задач"
      subtitle={`Завершено ${data.completed_leaf_tasks} из ${data.total_leaf_tasks} листовых задач`}
    >
      <TaskGraphCanvas tree={data.nodes} onSelectNode={setSelectedTask} />
      <Drawer
        open={Boolean(selectedTask)}
        title={selectedTask?.title ?? "Задача"}
        onClose={() => setSelectedTask(null)}
      >
        {selectedTask ? (
          <TaskNodeDetail
            task={selectedTask}
            projectId={projectId}
            onRetryTask={(taskId) => retryMutation.mutate(taskId)}
          />
        ) : null}
      </Drawer>
    </SectionCard>
  );
}
```

Заменить на:
```tsx
function TaskGraphPage({ projectId }: { projectId: string }) {
  // W4.2 (G1): canvas-based task graph через ReactFlow + dagre.
  // Кликнул на узел → открывается drawer с тем же TaskNodeDetail,
  // что и на L2 Activity, плюс панель «Рассуждение» внутри.
  // Ни provider, ни model из UI не передаются — см. WorkspaceRoute.
  const provider = "";
  const model = "";
  const navigate = useNavigate();
  const [selectedTask, setSelectedTask] = useState<TaskNodeView | null>(null);
  const taskGraphQuery = useQuery({
    queryKey: projectionKey(projectId, "task_graph"),
    queryFn: () => api.getTaskGraph(projectId),
  });
  const retryMutation = useMutation({
    mutationFn: (taskId: string) => api.retryTask(projectId, taskId, provider, model),
  });

  if (taskGraphQuery.isLoading || !taskGraphQuery.data) {
    return <LoadingPanel title="Загрузка графа задач…" />;
  }

  const data = taskGraphQuery.data;
  return (
    <SectionCard
      title="Граф задач"
      subtitle={`Завершено ${data.completed_leaf_tasks} из ${data.total_leaf_tasks} листовых задач`}
    >
      <TaskGraphCanvas
        tree={data.nodes}
        onSelectNode={setSelectedTask}
        completedLeafTasks={data.completed_leaf_tasks}
        totalLeafTasks={data.total_leaf_tasks}
        onRetry={(taskId) => retryMutation.mutate(taskId)}
        onOpenArtifacts={() => navigate(`/projects/${projectId}/artifacts`)}
        onGoToDecisions={() => navigate(`/projects/${projectId}/decisions/pending`)}
      />
      <Drawer
        open={Boolean(selectedTask)}
        title={selectedTask?.title ?? "Задача"}
        onClose={() => setSelectedTask(null)}
      >
        {selectedTask ? (
          <TaskNodeDetail
            task={selectedTask}
            projectId={projectId}
            onRetryTask={(taskId) => retryMutation.mutate(taskId)}
          />
        ) : null}
      </Drawer>
    </SectionCard>
  );
}
```

- [ ] **Step 3: Проверить build**

```bash
cd ui/workspace && npm run build 2>&1 | tail -20
```

Ожидаемый результат: `✓ built in` без ошибок.

- [ ] **Step 4: Commit**

```bash
git add ui/workspace/src/App.tsx
git commit -m "feat(task-graph): wire retry/artifacts/decisions callbacks in TaskGraphPage"
```

---

## Task 4: Ручная проверка

- [ ] **Step 1: Запустить dev сервер**

```bash
cd ui/workspace && npm run dev
```

Открыть `http://localhost:5173`, перейти к любому проекту → вкладка «Граф задач».

- [ ] **Step 2: Проверить прогресс-баннер**

Ожидаемое: в левом верхнем углу графа виден баннер вида `● Название задачи  5 / 12`. Если задач нет — баннер не отображается.

- [ ] **Step 3: Проверить пульсирующую рамку**

Ожидаемое: нода с `is_current=true` имеет мигающий cyan outline. Остальные ноды — статичная рамка.

- [ ] **Step 4: Проверить авто-центрирование**

Ожидаемое: при открытии страницы граф плавно (600ms) центрируется на активной ноде. При изменении статуса через WebSocket — граф перецентрируется.

- [ ] **Step 5: Проверить action меню**

Навести на failed-ретраяемую ноду: должна появиться кнопка `Retry`. Навести на completed ноду: кнопка `Артефакт`. Навести на ноду с `blocking_clarification_count > 0`: кнопка `⚠ Решения`.

- [ ] **Step 6: Проверить строку ошибки**

На ноде с `status=failed` и непустым `status_summary` — под заголовком должна быть красная строка с текстом ошибки.

- [ ] **Step 7: Проверить существующие сценарии**

- Граф без задач (`total_leaf_tasks=0`) — баннер не отображается, граф пустой.
- Все задачи completed — баннер показывает `Нет активных задач` + серую точку.
- Клик на ноду → drawer открывается как раньше.
- MiniMap: `failed` теперь ярче `blocked` (разные opacity в `statusFillColor`).
