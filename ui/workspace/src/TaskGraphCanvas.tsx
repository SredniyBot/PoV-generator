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

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
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

import type { FanOutMeta, TaskNodeView } from "./types";

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

interface FanOutCardData extends Record<string, unknown> {
  task: TaskNodeView;
  onToggleFanOut: (id: string) => void;
  isCollapsed: boolean;
}

type FlowNode = Node<TaskNodeCardData | FanOutCardData>;

// ── Helpers ────────────────────────────────────────────────────────────────

/** Flatten with collapse support — skips children of collapsed fan-out nodes. */
function flattenTree(nodes: TaskNodeView[], collapsed: Set<string>): TaskNodeView[] {
  const result: TaskNodeView[] = [];
  for (const node of nodes) {
    result.push(node);
    if (!collapsed.has(node.task_id) && node.children?.length) {
      result.push(...flattenTree(node.children, collapsed));
    }
  }
  return result;
}

interface BuildLayoutOptions {
  collapsed: Set<string>;
  onToggleFanOut: (id: string) => void;
}

function buildLayout(
  tasks: TaskNodeView[],
  options: BuildLayoutOptions,
): { nodes: FlowNode[]; edges: Edge[] } {
  const { collapsed, onToggleFanOut } = options;
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
  // Dashed data-dependency edges: producer → fan-out node
  for (const task of tasks) {
    if (task.template_type === "fan_out" && task.fan_out_meta?.producer_task_id) {
      const producerId = task.fan_out_meta.producer_task_id;
      if (tasks.some((t) => t.task_id === producerId)) {
        edges.push({
          id: `producer-${producerId}-${task.task_id}`,
          source: producerId,
          target: task.task_id,
          style: { stroke: "#8B5CF6", strokeDasharray: "6 3", strokeWidth: 2 },
          animated: false,
          type: "default",
        });
      }
    }
  }
  dagre.layout(graph);

  const nodes: FlowNode[] = tasks.map((task) => {
    const positioned = graph.node(task.task_id);
    const isFanOut = task.template_type === "fan_out";
    return {
      id: task.task_id,
      type: isFanOut ? "fanOutCard" : "taskCard",
      data: isFanOut
        ? ({ task, onToggleFanOut, isCollapsed: collapsed.has(task.task_id) } as FanOutCardData)
        : ({ task } as TaskNodeCardData),
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
    case "fan_out_instance":    return "fan-out";
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

function TaskCardNode({ data }: { data: TaskNodeCardData }) {
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

// ── Fan-out card node ──────────────────────────────────────────────────────

const STATUS_LABEL_FAN_OUT: Record<string, string> = {
  waiting_for_fan_out_source: "Ожидает данных",
  waiting_for_children: "В процессе",
  completed: "Завершено",
  failed: "Ошибка",
};

const STATUS_COLOR_FAN_OUT: Record<string, string> = {
  waiting_for_fan_out_source: "#94a3b8",
  waiting_for_children: "#3b82f6",
  completed: "#22c55e",
  failed: "#ef4444",
};

function FanOutCardNode({ data }: NodeProps<Node<FanOutCardData>>) {
  const { task, onToggleFanOut, isCollapsed } = data;
  const meta: FanOutMeta | null | undefined = task.fan_out_meta;
  const statusLabel = STATUS_LABEL_FAN_OUT[task.status] ?? task.status;
  const statusColor = STATUS_COLOR_FAN_OUT[task.status] ?? "#94a3b8";

  return (
    <div
      style={{
        background: "#fff",
        border: `2px dashed ${statusColor}`,
        borderRadius: 10,
        padding: "8px 12px",
        minWidth: 200,
        maxWidth: 260,
        fontSize: 12,
      }}
    >
      <Handle type="target" position={Position.Top} style={{ background: "#7c3aed" }} />
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
        <span style={{ fontWeight: 700, fontSize: 11, color: "#7c3aed" }}>⚡ fan-out</span>
        <span
          style={{
            background: statusColor,
            color: "#fff",
            borderRadius: 4,
            padding: "1px 6px",
            fontSize: 10,
          }}
        >
          {statusLabel}
        </span>
      </div>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>{task.title}</div>
      {task.status === "waiting_for_fan_out_source" && meta && (
        <div style={{ color: "#94a3b8", fontSize: 11 }}>
          Источник: {meta.source_artifact_role}
        </div>
      )}
      {task.status === "waiting_for_children" && meta && meta.total_instances > 0 && (
        <div style={{ marginTop: 4 }}>
          <div style={{ fontSize: 11, color: "#6b7280", marginBottom: 2 }}>
            {meta.completed_instances} / {meta.total_instances} завершено
          </div>
          <div style={{ height: 4, background: "#e5e7eb", borderRadius: 2 }}>
            <div
              style={{
                height: "100%",
                width: `${Math.round((meta.completed_instances / meta.total_instances) * 100)}%`,
                background: "#3b82f6",
                borderRadius: 2,
                transition: "width 0.3s",
              }}
            />
          </div>
        </div>
      )}
      {task.status === "failed" && task.error_message && (
        <div style={{ color: "#ef4444", fontSize: 11, marginTop: 4 }}>{task.error_message}</div>
      )}
      {meta != null && meta.total_instances > 4 && (
        <button
          onClick={(e) => { e.stopPropagation(); onToggleFanOut(task.task_id); }}
          style={{
            marginTop: 6,
            fontSize: 10,
            cursor: "pointer",
            background: "none",
            border: "1px solid #d1d5db",
            borderRadius: 4,
            padding: "2px 8px",
          }}
        >
          {isCollapsed
            ? `Показать все ${meta.total_instances}`
            : "Свернуть"}
        </button>
      )}
      <Handle type="source" position={Position.Bottom} style={{ background: "#7c3aed" }} />
    </div>
  );
}

const nodeTypes = { taskCard: TaskCardNode, fanOutCard: FanOutCardNode };

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
  // Дип-линк из статус-бара (?focus=<taskId>): центрировать граф на узле.
  focusTaskId?: string;
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
  focusTaskId,
  height = "70vh",
  completedLeafTasks = 0,
  totalLeafTasks = 0,
  onRetry,
  onOpenArtifacts,
  onGoToDecisions,
}: TaskGraphCanvasProps) {
  const [collapsedFanOuts, setCollapsedFanOuts] = useState<Set<string>>(new Set());

  // Auto-collapse fan-out nodes with > 4 instances on first mount / tree change.
  useEffect(() => {
    const toCollapse = new Set<string>();
    function walk(nodes: TaskNodeView[]) {
      for (const n of nodes) {
        if (n.template_type === "fan_out" && (n.fan_out_meta?.total_instances ?? 0) > 4) {
          toCollapse.add(n.task_id);
        }
        if (n.children?.length) walk(n.children);
      }
    }
    walk(tree);
    setCollapsedFanOuts((prev) => {
      const next = new Set(prev);
      toCollapse.forEach((id) => next.add(id));
      return next;
    });
  }, [tree]);

  const toggleFanOut = useCallback(
    (id: string) =>
      setCollapsedFanOuts((prev) => {
        const next = new Set(prev);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        return next;
      }),
    [],
  );

  const tasks = useMemo(
    () => flattenTree(tree, collapsedFanOuts),
    [tree, collapsedFanOuts],
  );
  const layout = useMemo(
    () => buildLayout(tasks, { collapsed: collapsedFanOuts, onToggleFanOut: toggleFanOut }),
    [tasks, collapsedFanOuts, toggleFanOut],
  );
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

  // Центрирование на focusTaskId (дип-линк из статус-бара): зависит от nodes,
  // поэтому перезапускается после лэйаута/WS-рефреша; на ещё не появившийся
  // узел — no-op до его появления.
  useEffect(() => {
    if (!focusTaskId) return;
    const node = nodes.find((n) => n.id === focusTaskId);
    if (!node) return;
    setCenter(node.position.x + NODE_WIDTH / 2, node.position.y + NODE_HEIGHT / 2, {
      zoom: 1.2,
      duration: 600,
    });
  }, [focusTaskId, nodes, setCenter]);

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
            if (onSelectNode) onSelectNode((node.data as { task: TaskNodeView }).task);
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
              const status = (node.data as { task?: TaskNodeView })?.task?.status ?? "candidate";
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
