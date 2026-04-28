import dagre from "dagre";

export const NODE_WIDTH = 240;
export const NODE_HEIGHT = 90;

/**
 * Build ReactFlow nodes and edges from a compiled task IR.
 */
export function buildGraphFromIR(ir) {
  const steps = ir.steps ?? [];
  if (steps.length === 0) return { nodes: [], edges: [] };

  const rawNodes = steps.map((step) => ({
    id: step.id,
    type: "taskNode",
    data: { step },
    position: { x: 0, y: 0 },
  }));

  const rawEdges = buildEdges(steps);
  return applyDagreLayout(rawNodes, rawEdges);
}

export function buildEdges(steps) {
  const edges = [];
  for (const step of steps) {
    switch (step.kind) {
      case "action":
      case "wait_for_event":
      case "wait_gate":
        if (step.next_step) edges.push(makeEdge(step.id, step.next_step, "default", null));
        break;
      case "decision":
        if (step.on_true) edges.push(makeEdge(step.id, step.on_true, "true", "true", "✓ true"));
        if (step.on_false) edges.push(makeEdge(step.id, step.on_false, "false", "false", "✗ false"));
        break;
      default:
        break;
    }
  }
  return edges;
}

export function makeEdge(source, target, variant, sourceHandle = null, label = "") {
  return {
    id: `${source}→${target}`,
    source,
    target,
    sourceHandle,
    label,
    data: { variant },
    type: "smoothstep",
    animated: variant === "false",
    style: edgeStyle(variant),
    labelStyle: { fill: "#b0b8c8", fontSize: 11 },
    labelBgStyle: { fill: "#1a1e2a", opacity: 0.9 },
    labelBgPadding: [4, 6],
    labelBgBorderRadius: 4,
    markerEnd: { type: "arrowclosed", color: edgeColor(variant) },
  };
}

export function edgeStyle(variant) {
  switch (variant) {
    case "true":  return { stroke: "#4caf87", strokeWidth: 2 };
    case "false": return { stroke: "#e06c75", strokeWidth: 2, strokeDasharray: "5 3" };
    default:      return { stroke: "#5a7fa8", strokeWidth: 2 };
  }
}

function edgeColor(variant) {
  switch (variant) {
    case "true":  return "#4caf87";
    case "false": return "#e06c75";
    default:      return "#5a7fa8";
  }
}

export function applyDagreLayout(nodes, edges) {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "TB", nodesep: 70, ranksep: 90 });
  g.setDefaultEdgeLabel(() => ({}));

  for (const node of nodes) {
    g.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (const edge of edges) {
    g.setEdge(edge.source, edge.target);
  }

  dagre.layout(g);

  const layoutNodes = nodes.map((node) => {
    const pos = g.node(node.id);
    return {
      ...node,
      position: {
        x: (pos?.x ?? 0) - NODE_WIDTH / 2,
        y: (pos?.y ?? 0) - NODE_HEIGHT / 2,
      },
    };
  });

  return { nodes: layoutNodes, edges };
}
