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
import { AlertTriangle, ChevronDown, ChevronRight, FileText, Layers, Split } from "lucide-react";

import "@xyflow/react/dist/style.css";

import type { FanOutMeta, TaskNodeView } from "./types";

const NODE_WIDTH = 180;
const NODE_HEIGHT = 76;

// ── Callbacks context ──────────────────────────────────────────────────────

interface TaskGraphActions {
  onRetry: (taskId: string) => void;
  onOpenArtifacts: (task: TaskNodeView) => void;
  onGoToDecisions: () => void;
}

const TaskGraphActionsCtx = createContext<TaskGraphActions | null>(null);

// ── Data types ─────────────────────────────────────────────────────────────

interface TaskNodeCardData extends Record<string, unknown> {
  task: TaskNodeView;
  // Композит с детьми можно свернуть (прячет поддерево).
  onToggle?: (id: string) => void;
  isCollapsed?: boolean;
  childCount?: number;
}

interface FanOutCardData extends Record<string, unknown> {
  task: TaskNodeView;
  onToggle: (id: string) => void;
  isCollapsed: boolean;
}

type FlowNode = Node<TaskNodeCardData | FanOutCardData>;

// ── Helpers ────────────────────────────────────────────────────────────────

/** Flatten with collapse support — skips children of any collapsed node
 *  (composite или fan-out). */
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
  onToggle: (id: string) => void;
}

function buildLayout(
  tasks: TaskNodeView[],
  options: BuildLayoutOptions,
): { nodes: FlowNode[]; edges: Edge[] } {
  const { collapsed, onToggle } = options;
  const graph = new dagre.graphlib.Graph();
  graph.setDefaultEdgeLabel(() => ({}));
  // TB (сверху вниз) — горизонтальная раскладка по запросу. Компактность даём
  // узкими узлами + плотным nodesep; широкие ветки можно свернуть на композите.
  graph.setGraph({ rankdir: "TB", nodesep: 20, ranksep: 70, marginx: 20, marginy: 20 });

  for (const task of tasks) {
    graph.setNode(task.task_id, { width: NODE_WIDTH, height: nodeHeight(task) });
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
    const h = nodeHeight(task);
    return {
      id: task.task_id,
      type: isFanOut ? "fanOutCard" : "taskCard",
      data: isFanOut
        ? ({ task, onToggle, isCollapsed: collapsed.has(task.task_id) } as FanOutCardData)
        : ({
            task,
            onToggle,
            isCollapsed: collapsed.has(task.task_id),
            childCount: task.children?.length ?? 0,
          } as TaskNodeCardData),
      position: {
        x: (positioned?.x ?? 0) - NODE_WIDTH / 2,
        y: (positioned?.y ?? 0) - h / 2,
      },
      width: NODE_WIDTH,
      height: h,
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

// Единый источник «статус → подпись + цвет». Им пользуются и обычные узлы, и
// fan-out, и мини-карта — одна палитра вместо четырёх разрозненных.
const STATUS_META: Record<string, { label: string; color: string }> = {
  completed:                  { label: "Готово",        color: "rgba(140, 196, 153, 0.9)" },
  in_progress:                { label: "В работе",      color: "rgba(120, 184, 201, 0.95)" },
  ready:                      { label: "Готова",        color: "rgba(120, 184, 201, 0.95)" },
  waiting_for_children:       { label: "В процессе",    color: "rgba(214, 173, 89, 0.9)" },
  waiting_for_fan_out_source: { label: "Ждёт данные",   color: "rgba(214, 173, 89, 0.9)" },
  failed:                     { label: "Ошибка",        color: "rgba(215, 131, 131, 0.95)" },
  blocked:                    { label: "Заблокирована", color: "rgba(215, 131, 131, 0.7)" },
  skipped:                    { label: "Пропущена",     color: "rgba(150, 150, 150, 0.55)" },
  obsolete:                   { label: "Устарела",      color: "rgba(150, 150, 150, 0.55)" },
  candidate:                  { label: "Запланирована", color: "rgba(150, 160, 180, 0.6)" },
};

function statusMeta(status: string): { label: string; color: string } {
  return STATUS_META[status] ?? { label: status, color: "rgba(150, 160, 180, 0.6)" };
}

// fan-out-узлы выше (прогресс + переключатель), остальные — компактные.
function nodeHeight(task: TaskNodeView): number {
  return task.template_type === "fan_out" ? 124 : NODE_HEIGHT;
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
  const meta = statusMeta(task.status);
  const warnCount = task.blocking_clarification_count ?? 0;
  const isComposite = task.template_type === "composite";
  const childCount = data.childCount ?? 0;
  const collapsible = isComposite && childCount > 0 && Boolean(data.onToggle);

  return (
    <div
      className={`tg-node${task.is_current ? " tg-node--current" : ""}`}
      style={{ borderLeftColor: meta.color }}
    >
      <Handle type="target" position={Position.Top} className="tg-handle" />
      <div className="tg-node__status">
        {collapsible ? (
          <button
            type="button"
            className="tg-node__collapse"
            title={data.isCollapsed ? "Развернуть подзадачи" : "Свернуть подзадачи"}
            onClick={(e) => { e.stopPropagation(); data.onToggle?.(task.task_id); }}
          >
            {data.isCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
          </button>
        ) : null}
        <span className="tg-dot" style={{ background: meta.color }} />
        <span className="tg-node__status-label">{meta.label}</span>
        {warnCount > 0 ? (
          <span className="tg-node__warn" title={`Ждут решения: ${warnCount}`}>
            <AlertTriangle size={12} /> {warnCount}
          </span>
        ) : null}
        {collapsible && data.isCollapsed ? (
          <span className="tg-node__hidden" title="скрытых подзадач">{childCount}</span>
        ) : null}
        {isComposite ? (
          <Layers size={13} className="tg-node__type-icon" aria-label="композит" />
        ) : (
          <FileText size={13} className="tg-node__type-icon" aria-label="задача" />
        )}
      </div>
      <div className="tg-node__title" title={task.title}>{task.title}</div>
      {task.status === "failed" && task.status_summary ? (
        <div className="tg-node__error" title={task.status_summary}>
          {task.status_summary}
        </div>
      ) : null}
      {hasActions && actions ? (
        <div className="tg-node__actions">
          {showRetry && (
            <button
              className="tg-action-btn tg-action-btn--danger"
              onClick={(e) => { e.stopPropagation(); actions.onRetry(task.task_id); }}
            >
              Повторить
            </button>
          )}
          {showArtifacts && (
            <button
              className="tg-action-btn"
              onClick={(e) => { e.stopPropagation(); actions.onOpenArtifacts(task); }}
            >
              Артефакт
            </button>
          )}
          {showDecisions && (
            <button
              className="tg-action-btn"
              onClick={(e) => { e.stopPropagation(); actions.onGoToDecisions(); }}
            >
              Решения
            </button>
          )}
        </div>
      ) : null}
      <Handle type="source" position={Position.Bottom} className="tg-handle" />
    </div>
  );
}

// ── Fan-out card node ──────────────────────────────────────────────────────

function FanOutCardNode({ data }: NodeProps<Node<FanOutCardData>>) {
  const { task, onToggle, isCollapsed } = data;
  const meta: FanOutMeta | null | undefined = task.fan_out_meta;
  const status = statusMeta(task.status);
  const pct =
    meta && meta.total_instances > 0
      ? Math.round((meta.completed_instances / meta.total_instances) * 100)
      : 0;

  return (
    <div className="tg-node tg-node--fanout" style={{ borderLeftColor: status.color }}>
      <Handle type="target" position={Position.Top} className="tg-handle" />
      <div className="tg-node__status">
        <span className="tg-dot" style={{ background: status.color }} />
        <span className="tg-node__status-label">{status.label}</span>
        <Split size={13} className="tg-node__type-icon" aria-label="fan-out" />
      </div>
      <div className="tg-node__title" title={task.title}>{task.title}</div>
      {task.status === "waiting_for_fan_out_source" && meta ? (
        <div className="tg-node__sub">Источник: {meta.source_artifact_role}</div>
      ) : null}
      {task.status === "waiting_for_children" && meta && meta.total_instances > 0 ? (
        <div className="tg-fanout">
          <div className="tg-fanout__count">
            {meta.completed_instances} / {meta.total_instances} готово
          </div>
          <div className="tg-fanout__bar">
            <div className="tg-fanout__bar-fill" style={{ width: `${pct}%` }} />
          </div>
        </div>
      ) : null}
      {task.status === "failed" && task.error_message ? (
        <div className="tg-node__error" title={task.error_message}>{task.error_message}</div>
      ) : null}
      {meta != null && meta.total_instances > 4 ? (
        <button
          className="tg-action-btn tg-fanout__toggle"
          onClick={(e) => { e.stopPropagation(); onToggle(task.task_id); }}
        >
          {isCollapsed ? `Показать все ${meta.total_instances}` : "Свернуть"}
        </button>
      ) : null}
      <Handle type="source" position={Position.Bottom} className="tg-handle" />
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
  onOpenArtifacts?: (task: TaskNodeView) => void;
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
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  // Auto-collapse fan-out nodes with > 4 instances on first mount / tree change.
  // Композиты по умолчанию развёрнуты — пользователь сам решает, что свернуть.
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
    setCollapsed((prev) => {
      const next = new Set(prev);
      toCollapse.forEach((id) => next.add(id));
      return next;
    });
  }, [tree]);

  const toggleCollapse = useCallback(
    (id: string) =>
      setCollapsed((prev) => {
        const next = new Set(prev);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        return next;
      }),
    [],
  );

  const tasks = useMemo(
    () => flattenTree(tree, collapsed),
    [tree, collapsed],
  );
  const layout = useMemo(
    () => buildLayout(tasks, { collapsed, onToggle: toggleCollapse }),
    [tasks, collapsed, toggleCollapse],
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
      onOpenArtifacts: (task) => onOpenArtifacts?.(task),
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
  return statusMeta(status).color;
}
