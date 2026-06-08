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

import type { ReactNode } from "react";
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
import { AlertTriangle, Bot, ChevronDown, ChevronRight, FileText, Layers, Split, Undo2 } from "lucide-react";

import "@xyflow/react/dist/style.css";

import type { FanOutMeta, TaskNodeView } from "./types";
import { taskStatusVisual } from "./workflowStatus";

const NODE_WIDTH = 180;
const NODE_HEIGHT = 76;

// ── Callbacks context ──────────────────────────────────────────────────────

interface TaskGraphActions {
  onRetry: (taskId: string) => void;
  onOpenArtifacts: (task: TaskNodeView) => void;
  onGoToDecisions: () => void;
  onRollback: (taskId: string) => void;
  // Транзитивное UI-состояние, общее для всех узлов (через контекст, чтобы не
  // протаскивать в data каждого узла и не ломать мемоизацию ReactFlow):
  // задача с ретраем в полёте — её кнопка «Повторить» блокируется;
  // цель отката и транзитивно затрагиваемые шаги — подсветка на графе.
  retryingTaskId: string | null;
  rollbackTargetId: string | null;
  rollbackAffectedIds: Set<string>;
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

// Статус → подпись/цвет/тон берём из единого словаря (workflowStatus.ts).
// Локального дубля STATUS_META больше нет — один источник на граф, ленту,
// дорожку и пилюли.

// fan-out-узлы выше (прогресс + переключатель), остальные — компактные.
// Заголовок теперь показываем целиком, поэтому высоту оцениваем по числу строк
// (~22 символа на строку при ширине 180) — чтобы dagre зарезервировал место и
// узлы не налезали друг на друга.
function nodeHeight(task: TaskNodeView): number {
  if (task.template_type === "fan_out") return 124;
  const lines = Math.max(1, Math.ceil((task.title?.length ?? 0) / 22));
  const errorH = task.status === "failed" && (task.error_message || task.status_summary) ? 18 : 0;
  return Math.max(NODE_HEIGHT, 38 + lines * 17 + errorH);
}

/** Условия рендеринга кнопок действий вынесены из JSX. */
function resolveActions(task: TaskNodeView): {
  showRetry: boolean;
  showArtifacts: boolean;
  showDecisions: boolean;
  showRollback: boolean;
} {
  // Ф1: задачи неактивного гейта (скелет/история) недоступны — никаких действий.
  if (task.available === false) {
    return { showRetry: false, showArtifacts: false, showDecisions: false, showRollback: false };
  }
  return {
    showRetry:     task.retryable === true && task.status === "failed",
    showArtifacts: task.status === "completed",
    showDecisions: (task.blocking_clarification_count ?? 0) > 0,
    // Откат доступен для выполненного листового шага: только у листов есть
    // чекпоинт (pre-state их исполнения) — база реконструкции состояния.
    showRollback:  task.status === "completed" && task.template_type === "leaf",
  };
}

// ── Custom node ────────────────────────────────────────────────────────────

function TaskCardNode({ data }: { data: TaskNodeCardData }) {
  const task = data.task;
  const actions = useContext(TaskGraphActionsCtx);
  const { showRetry, showArtifacts, showDecisions, showRollback } = resolveActions(task);
  const hasActions = showRetry || showArtifacts || showDecisions || showRollback;
  const meta = taskStatusVisual(task.status);
  const warnCount = task.blocking_clarification_count ?? 0;
  const isComposite = task.template_type === "composite";
  const childCount = data.childCount ?? 0;
  const collapsible = isComposite && childCount > 0 && Boolean(data.onToggle);

  // Ретрай этой задачи в полёте — кнопка «Повторить» блокируется (задача 2).
  const retrying = actions?.retryingTaskId === task.task_id;
  // Подсветка отката: цель / транзитивно затрагиваемый / приглушённый (вне
  // зоны отката, пока он «взведён»).
  const rbState =
    !actions || !actions.rollbackTargetId
      ? null
      : actions.rollbackTargetId === task.task_id
        ? "target"
        : actions.rollbackAffectedIds.has(task.task_id)
          ? "affected"
          : "dimmed";

  return (
    <div
      className={
        "tg-node" +
        (task.is_current ? " tg-node--current" : "") +
        (task.available === false ? " tg-node--locked" : "") +
        (task.is_harness ? " tg-node--harness" : "") +
        (rbState ? ` tg-node--rb-${rbState}` : "")
      }
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
        <span
          className={"tg-dot" + (meta.pulse ? " tg-dot--pulse" : "")}
          style={{ background: meta.color }}
        />
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
        ) : task.is_harness ? (
          <Bot size={13} className="tg-node__type-icon tg-node__type-icon--harness" aria-label="агент" />
        ) : (
          <FileText size={13} className="tg-node__type-icon" aria-label="задача" />
        )}
      </div>
      {task.is_harness ? (
        <span className="tg-node__agent-tag" title="Исполняется автономным агентом (harness)">
          агент
        </span>
      ) : null}
      <div className="tg-node__title" title={task.title}>{task.title}</div>
      {task.status === "failed" && (task.error_message || task.status_summary) ? (
        <div
          className="tg-node__error"
          title={task.error_message || task.status_summary || undefined}
        >
          {task.error_message || task.status_summary}
        </div>
      ) : null}
      {hasActions && actions ? (
        <div className="tg-node__actions">
          {showRetry && (
            <button
              className="tg-action-btn tg-action-btn--danger"
              disabled={retrying}
              onClick={(e) => { e.stopPropagation(); actions.onRetry(task.task_id); }}
            >
              {retrying ? "Повторяю…" : "Повторить"}
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
          {showRollback && (
            <button
              className={
                "tg-action-btn tg-action-btn--rollback" +
                (rbState === "target" ? " tg-action-btn--rollback-armed" : "")
              }
              title="Откатить проект к состоянию до выполнения этого шага"
              onClick={(e) => { e.stopPropagation(); actions.onRollback(task.task_id); }}
            >
              <Undo2 size={11} /> Откатить
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
  const status = taskStatusVisual(task.status);
  const pct =
    meta && meta.total_instances > 0
      ? Math.round((meta.completed_instances / meta.total_instances) * 100)
      : 0;

  return (
    <div className="tg-node tg-node--fanout" style={{ borderLeftColor: status.color }}>
      <Handle type="target" position={Position.Top} className="tg-handle" />
      <div className="tg-node__status">
        <span
          className={"tg-dot" + (status.pulse ? " tg-dot--pulse" : "")}
          style={{ background: status.color }}
        />
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
      {task.status === "failed" && (task.error_message || task.status_summary) ? (
        <div
          className="tg-node__error"
          title={task.error_message || task.status_summary || undefined}
        >
          {task.error_message || task.status_summary}
        </div>
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
  onRollback?: (taskId: string) => void;
  // Задача с ретраем в полёте — её кнопка «Повторить» блокируется (задача 2).
  retryingTaskId?: string | null;
  // Откат: цель + транзитивно затрагиваемые шаги — для подсветки на графе;
  // overlay — плавающая панель подтверждения внутри канвы (не модал, чтобы
  // подсветка оставалась видна).
  rollbackTargetId?: string | null;
  rollbackAffectedIds?: readonly string[];
  rollbackOverlay?: ReactNode;
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
  onRollback,
  retryingTaskId = null,
  rollbackTargetId = null,
  rollbackAffectedIds,
  rollbackOverlay,
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

  const rollbackAffectedSet = useMemo(
    () => new Set(rollbackAffectedIds ?? []),
    [rollbackAffectedIds],
  );
  const actions: TaskGraphActions = useMemo(
    () => ({
      onRetry:         (id) => onRetry?.(id),
      onOpenArtifacts: (task) => onOpenArtifacts?.(task),
      onGoToDecisions: () => onGoToDecisions?.(),
      onRollback:      (id) => onRollback?.(id),
      retryingTaskId,
      rollbackTargetId,
      rollbackAffectedIds: rollbackAffectedSet,
    }),
    [onRetry, onOpenArtifacts, onGoToDecisions, onRollback, retryingTaskId, rollbackTargetId, rollbackAffectedSet],
  );

  return (
    <TaskGraphActionsCtx.Provider value={actions}>
      <div className="tg-canvas" style={{ height }}>
        <ProgressBanner
          tasks={tasks}
          completedLeafTasks={completedLeafTasks}
          totalLeafTasks={totalLeafTasks}
        />
        {/* Панель подтверждения отката — плавающая внутри канвы (не модал),
            чтобы подсветка затрагиваемых узлов оставалась видна (задача 3). */}
        {rollbackOverlay}
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
  return taskStatusVisual(status).color;
}
