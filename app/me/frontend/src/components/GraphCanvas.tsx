import { useCallback, useMemo, useRef } from "react";
import ForceGraph2D, { ForceGraphMethods } from "react-force-graph-2d";
import { LensEdge, LensNode } from "../types";

interface ForceNode {
  id: string;
  label: string;
  node_type: string;
  pagerank_score: number;
  is_seed: boolean;
  category: string | null;
  description: string;
  start_date: string | null;
  end_date: string | null;
  importance: number;
  aliases: string[];
}

interface ForceLink {
  source: string;
  target: string;
  relationship_type: string;
  importance: number;
}

interface Props {
  nodes: LensNode[];
  edges: LensEdge[];
  onNodeClick: (node: LensNode, event: MouseEvent) => void;
}

// Visual hierarchy: entities dominate, states subdue.
const STATE_TYPES = new Set(["State", "Goal"]);

export function GraphCanvas({ nodes, edges, onNodeClick }: Props) {
  const fgRef = useRef<ForceGraphMethods>();

  const graphData = useMemo(() => {
    const fNodes: ForceNode[] = nodes.map((n) => ({
      id: n.id,
      label: n.label,
      node_type: n.node_type,
      pagerank_score: n.pagerank_score,
      is_seed: n.is_seed,
      category: n.category,
      description: n.description,
      start_date: n.start_date,
      end_date: n.end_date,
      importance: n.importance,
      aliases: n.aliases,
    }));
    const fLinks: ForceLink[] = edges.map((e) => ({
      source: e.source_id,
      target: e.target_id,
      relationship_type: e.relationship_type,
      importance: e.importance,
    }));
    return { nodes: fNodes, links: fLinks };
  }, [nodes, edges]);

  const handleClick = useCallback(
    (node: ForceNode, event: MouseEvent) => {
      const original = nodes.find((n) => n.id === node.id);
      if (original) onNodeClick(original, event);
    },
    [nodes, onNodeClick],
  );

  const drawNode = useCallback(
    (node: ForceNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const isState = STATE_TYPES.has(node.node_type);
      const isSeed = node.is_seed;

      // Sizing: entities are ~12-22 radius (depends on PR + zoom); states are ~5-7.
      const baseRadius = isState ? 5 : 14;
      const prBoost = isState ? 0 : Math.min(8, node.pagerank_score * 80);
      const radius = baseRadius + prBoost;

      const x = (node as ForceNode & { x: number }).x;
      const y = (node as ForceNode & { y: number }).y;

      // Fill color by type. Single quiet palette; seeds get the accent.
      let fill = "#e5e7eb"; // gray-200 (default)
      if (isSeed) fill = "#2563eb"; // accent
      else if (node.node_type === "Entity") fill = "#1f2937"; // gray-800
      else if (node.node_type === "Event") fill = "#9ca3af"; // gray-400
      else if (node.node_type === "Concept") fill = "#d1d5db"; // gray-300

      ctx.beginPath();
      ctx.arc(x, y, radius, 0, 2 * Math.PI);
      ctx.fillStyle = fill;
      ctx.fill();

      // Seed gets a brighter ring; non-seed entities get a subtle white ring
      // to read against any backdrop.
      if (isSeed) {
        ctx.lineWidth = 3 / globalScale;
        ctx.strokeStyle = "#1d4ed8";
        ctx.stroke();
      }

      // Label below the node, with size scaled to globalScale.
      const fontSize = (isState ? 9 : 12) / globalScale;
      ctx.font = `${fontSize}px Inter, system-ui, sans-serif`;
      ctx.fillStyle = "#111827";
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      const labelText =
        isState && node.label.length > 18
          ? node.label.slice(0, 16) + "…"
          : node.label;
      ctx.fillText(labelText, x, y + radius + 4 / globalScale);
    },
    [],
  );

  const nodePointerArea = useCallback(
    (node: ForceNode, color: string, ctx: CanvasRenderingContext2D) => {
      const isState = STATE_TYPES.has(node.node_type);
      const radius = isState ? 7 : 18;
      const x = (node as ForceNode & { x: number }).x;
      const y = (node as ForceNode & { y: number }).y;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(x, y, radius, 0, 2 * Math.PI);
      ctx.fill();
    },
    [],
  );

  return (
    <ForceGraph2D
      ref={fgRef}
      graphData={graphData}
      nodeId="id"
      linkSource="source"
      linkTarget="target"
      backgroundColor="#ffffff"
      linkColor={() => "rgba(107, 114, 128, 0.4)"}
      linkWidth={(link: ForceLink) => 0.6 + (link.importance ?? 0.5) * 1.4}
      nodeCanvasObject={drawNode}
      nodePointerAreaPaint={nodePointerArea}
      onNodeClick={handleClick}
      cooldownTicks={120}
      d3AlphaDecay={0.04}
      d3VelocityDecay={0.3}
      enableZoomInteraction
      enablePanInteraction
    />
  );
}
