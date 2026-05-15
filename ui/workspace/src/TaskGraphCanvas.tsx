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
 *   `TaskNodeDetail` drawer (с панелью "Рассуждение" и Provenance в L4).
 *
 * Custom node:
 * - статус-цвет через CSS-классы (см. styles.css `.tg-node--<status>`);
 * - acceleration `is_current` → highlight рамкой;
 * - небольшой badge с типом origin (`base_child`, `domain_contribution`,
 *   `repair`, `user_request`, `system`) — менеджеру важно отличать
 *   автоматически порождённые задачи от ручных override'ов.
 */

import { useEffect, useMemo } from "react";
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
} from "@xyflow/react";
import dagre from "@dagrejs/dagre";

import "@xyflow/react/dist/style.css";

import type { TaskNodeView } from "./types";

const NODE_WIDTH = 240;
const NODE_HEIGHT = 96;

interface TaskNodeCardData extends Record<string, unknown> {
  task: TaskNodeView;
}

type FlowNode = Node<TaskNodeCardData>;

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
      return "rgba(214, 173, 89, 0.5)"; // domain pack — желтоватый
    case "repair":
      return "rgba(215, 131, 131, 0.6)"; // repair — красноватый
    case "user_request":
      return "rgba(168, 132, 198, 0.6)"; // user override — фиолетовый
    default:
      return "rgba(255, 255, 255, 0.15)"; // base_child / objective_root
  }
}

function labelForOrigin(origin: string): string {
  switch (origin) {
    case "objective_root":
      return "корень цели";
    case "base_child":
      return "базовая";
    case "domain_contribution":
      return "domain pack";
    case "repair":
      return "исправление";
    case "user_request":
      return "ручная";
    case "system":
      return "система";
    default:
      return origin;
  }
}

function TaskCardNode({ data }: NodeProps<FlowNode>) {
  const task = data.task;
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
      <div className="tg-node__meta">
        <span>{task.template_type === "composite" ? "композит" : "задача"}</span>
        {task.blocking_clarification_count > 0 ? (
          <span className="tg-node__warn">⚠ {task.blocking_clarification_count}</span>
        ) : null}
      </div>
      <Handle type="source" position={Position.Bottom} className="tg-handle" />
    </div>
  );
}

const nodeTypes = { taskCard: TaskCardNode };

interface TaskGraphCanvasProps {
  tree: TaskNodeView[];
  onSelectNode?: (task: TaskNodeView) => void;
  height?: string | number;
}

function TaskGraphCanvasInner({ tree, onSelectNode, height = "70vh" }: TaskGraphCanvasProps) {
  const tasks = useMemo(() => flatten(tree), [tree]);
  const layout = useMemo(() => buildLayout(tasks), [tasks]);
  const [nodes, setNodes, onNodesChange] = useNodesState<FlowNode>(layout.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(layout.edges);

  // Когда tree обновляется (новые задачи, изменился статус) — пересчитываем
  // layout. dagre дешёвый — пересчёт ~миллисекунды на 50 узлов.
  useEffect(() => {
    setNodes(layout.nodes);
    setEdges(layout.edges);
  }, [layout, setNodes, setEdges]);

  return (
    <div className="tg-canvas" style={{ height }}>
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
            const status = ((node.data as TaskNodeCardData)?.task?.status ?? "candidate");
            return statusFillColor(status);
          }}
        />
      </ReactFlow>
    </div>
  );
}

export function TaskGraphCanvas(props: TaskGraphCanvasProps) {
  return (
    <ReactFlowProvider>
      <TaskGraphCanvasInner {...props} />
    </ReactFlowProvider>
  );
}

function statusFillColor(status: string): string {
  switch (status) {
    case "completed":
      return "rgba(140, 196, 153, 0.7)";
    case "in_progress":
    case "ready":
      return "rgba(120, 184, 201, 0.7)";
    case "failed":
    case "blocked":
      return "rgba(215, 131, 131, 0.7)";
    case "waiting_for_children":
      return "rgba(214, 173, 89, 0.6)";
    case "skipped":
    case "obsolete":
      return "rgba(150, 150, 150, 0.4)";
    default:
      return "rgba(150, 160, 180, 0.5)";
  }
}
