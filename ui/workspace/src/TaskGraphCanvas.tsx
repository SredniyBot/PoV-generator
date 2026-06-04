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
    case "objective_root":      return "корень цели";
    case "base_child":          return "базовая";
    case "domain_contribution": return "domain pack";
    case "repair":              return "исправление";
    case "user_request":        return "ручная";
    case "system":              return "система";
    default:                    return origin;
  }
}

/** Условия рендеринга кнопок действий вынесены из JSX. */
function resolveActions(task: TaskNodeView): {
  showRetry: boolean;
  showArtifacts: boolean;
  showDecisions: boolean;
} {
  return {
    showRetry:     task.retryable === true && task.status === "failed",
    showArtifacts: task.status === "completed",
    showDecisions: (task.blocking_clarification_count ?? 0) > 0,
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
        {(task.blocking_clarification_count ?? 0) > 0 ? (
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
