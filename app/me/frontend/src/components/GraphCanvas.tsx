import { useCallback, useEffect, useMemo, useRef } from "react";
import ForceGraph2D from "react-force-graph-2d";
import { LensEdge, LensNode } from "../types";

interface ForceNode {
  id: string;
  label: string;
  node_type: string;
  pagerank_score: number;
  is_seed: boolean;
  is_anchor: boolean;
  importance: number;
  photo_url: string | null;
  primary_anchor_id: string | null;
  // d3-force runtime fields. We PIN every node via fx/fy from the global
  // map layout, so the engine doesn't actually move anything.
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
  fx?: number;
  fy?: number;
}

interface ForceLink {
  source: string | ForceNode;
  target: string | ForceNode;
  relationship_type: string;
  importance: number;
}

interface Props {
  nodes: LensNode[];
  edges: LensEdge[];
  onNodeClick: (node: LensNode, event: MouseEvent) => void;
}

// Visual hierarchy: entities = rounded-rectangle boxes; states/goals = small
// subdued chips that read as edge-decorations.
const STATE_TYPES = new Set(["State", "Goal"]);

function entityWidth(prScore: number): number {
  return 70 + Math.min(60, prScore * 250);
}

const ENTITY_HEIGHT = 36;
const STATE_WIDTH = 56;
const STATE_HEIGHT = 16;
const PHOTO_SIZE = 44;
// Map-mode layout: every node is pinned at its global (x, y). No force
// sim runs. SEED_RING_RADIUS / CLUSTER_STRENGTH retired.

function roundRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
) {
  const rr = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.lineTo(x + w - rr, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + rr);
  ctx.lineTo(x + w, y + h - rr);
  ctx.quadraticCurveTo(x + w, y + h, x + w - rr, y + h);
  ctx.lineTo(x + rr, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - rr);
  ctx.lineTo(x, y + rr);
  ctx.quadraticCurveTo(x, y, x + rr, y);
  ctx.closePath();
}

function fitLabel(label: string, maxWidth: number, charPx: number): string {
  const maxChars = Math.max(4, Math.floor(maxWidth / charPx));
  if (label.length <= maxChars) return label;
  return label.slice(0, maxChars - 1) + "…";
}

// Footprint radius for collision purposes. Nodes are not circles, so we use
function nodeFootprint(n: ForceNode): { halfW: number; halfH: number } {
  if (STATE_TYPES.has(n.node_type)) {
    return { halfW: STATE_WIDTH / 2, halfH: STATE_HEIGHT / 2 };
  }
  if (n.photo_url) {
    return { halfW: PHOTO_SIZE / 2, halfH: PHOTO_SIZE / 2 };
  }
  const w = entityWidth(n.pagerank_score);
  return { halfW: w / 2, halfH: ENTITY_HEIGHT / 2 };
}

// Side-locked anchor: pick the side of a node's bounding rectangle whose
// orientation matches the route's primary axis. For horizontal routes this
// is left or right at the node's vertical center; for vertical routes it's
// top or bottom at the node's horizontal center. This guarantees lines
// emerge from the middle of a side, not from a corner.
function sideAnchor(
  n: ForceNode,
  axis: "horizontal" | "vertical",
  toward: { x: number; y: number },
): { x: number; y: number } {
  const cx = n.x ?? 0;
  const cy = n.y ?? 0;
  const fp = nodeFootprint(n);
  if (axis === "horizontal") {
    return {
      x: cx + (toward.x >= cx ? fp.halfW : -fp.halfW),
      y: cy,
    };
  }
  return {
    x: cx,
    y: cy + (toward.y >= cy ? fp.halfH : -fp.halfH),
  };
}

// Single-bend orthogonal routing. Picks the dominant axis. If the boxes
// are aligned on that axis (low cross-axis delta), draws a straight line.
// Otherwise, exits the source on the dominant-axis side and turns once
// (rounded) to land on the target's perpendicular-axis side.
function drawOrthogonalLink(
  ctx: CanvasRenderingContext2D,
  src: ForceNode,
  tgt: ForceNode,
  globalScale: number,
) {
  const sx = src.x ?? 0;
  const sy = src.y ?? 0;
  const tx = tgt.x ?? 0;
  const ty = tgt.y ?? 0;
  const dx = tx - sx;
  const dy = ty - sy;
  const ax = Math.abs(dx);
  const ay = Math.abs(dy);

  // Pick the dominant axis. Tie goes to horizontal — most labels are wider
  // than tall, so horizontal-first connectors read cleaner.
  const horizontal = ax >= ay;

  // Source side anchor on the dominant axis.
  const s = sideAnchor(src, horizontal ? "horizontal" : "vertical", { x: tx, y: ty });

  // For straight-line cases (boxes well-aligned on the cross-axis), land
  // on the target's same-axis side too. The line is then truly straight,
  // zero bends.
  const STRAIGHT_TOLERANCE = 6;
  const crossDelta = horizontal ? ay : ax;
  if (crossDelta <= STRAIGHT_TOLERANCE) {
    const t = sideAnchor(tgt, horizontal ? "horizontal" : "vertical", { x: sx, y: sy });
    ctx.beginPath();
    ctx.moveTo(s.x, s.y);
    ctx.lineTo(t.x, t.y);
    ctx.lineWidth = 1.0 / globalScale;
    ctx.strokeStyle = "rgba(107, 114, 128, 0.5)";
    ctx.stroke();
    return;
  }

  // Single-bend route: leave source on dominant axis, turn 90 degrees,
  // land on target's perpendicular-axis side. Bend is at the elbow point.
  const t = sideAnchor(tgt, horizontal ? "vertical" : "horizontal", { x: sx, y: sy });
  const elbow = horizontal ? { x: t.x, y: s.y } : { x: s.x, y: t.y };

  // Rounded corner at the elbow.
  const radius = Math.min(
    14,
    Math.abs(s.x - elbow.x) / 2,
    Math.abs(s.y - elbow.y) / 2,
    Math.abs(elbow.x - t.x) / 2,
    Math.abs(elbow.y - t.y) / 2,
  );
  const r = Math.max(0, radius);

  ctx.beginPath();
  ctx.moveTo(s.x, s.y);
  if (r < 0.5) {
    ctx.lineTo(elbow.x, elbow.y);
    ctx.lineTo(t.x, t.y);
  } else {
    if (horizontal) {
      // Pre-elbow: horizontal segment, stop r before elbow.
      const preX = elbow.x - Math.sign(elbow.x - s.x) * r;
      ctx.lineTo(preX, s.y);
      const postY = elbow.y + Math.sign(t.y - elbow.y) * r;
      ctx.quadraticCurveTo(elbow.x, elbow.y, elbow.x, postY);
      ctx.lineTo(t.x, t.y);
    } else {
      const preY = elbow.y - Math.sign(elbow.y - s.y) * r;
      ctx.lineTo(s.x, preY);
      const postX = elbow.x + Math.sign(t.x - elbow.x) * r;
      ctx.quadraticCurveTo(elbow.x, elbow.y, postX, elbow.y);
      ctx.lineTo(t.x, t.y);
    }
  }
  ctx.lineWidth = 1.0 / globalScale;
  ctx.strokeStyle = "rgba(107, 114, 128, 0.5)";
  ctx.stroke();
}

export function GraphCanvas({ nodes, edges, onNodeClick }: Props) {
  const photoCache = useRef<Map<string, HTMLImageElement>>(new Map());
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const fgRef = useRef<any>(null);

  const graphData = useMemo(() => {
    const fNodes: ForceNode[] = nodes.map((n) => ({
      id: n.id,
      label: n.label,
      node_type: n.node_type,
      pagerank_score: n.pagerank_score,
      is_seed: n.is_seed,
      is_anchor: n.is_anchor,
      importance: n.importance,
      photo_url: null,
      primary_anchor_id: n.primary_anchor_id,
      // Pin every node at its global map position. The d3 sim won't move
      // pinned nodes, so the layout is fully deterministic.
      x: n.x,
      y: n.y,
      fx: n.x,
      fy: n.y,
    }));
    const fLinks: ForceLink[] = edges.map((e) => ({
      source: e.source_id,
      target: e.target_id,
      relationship_type: e.relationship_type,
      importance: e.importance,
    }));
    return { nodes: fNodes, links: fLinks };
  }, [nodes, edges]);

  // Map mode: every node is pinned via fx/fy from the backend's global
  // layout. We zero out the d3-force "charge" so the engine doesn't try
  // to push pinned nodes around (it won't succeed, but it wastes ticks).
  // The collide force is also disabled — global layout already spaced
  // nodes appropriately at compute time.
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;

    const charge = fg.d3Force("charge");
    if (charge && typeof charge.strength === "function") {
      charge.strength(0);
    }
    const link = fg.d3Force("link");
    if (link && typeof link.strength === "function") {
      link.strength(0);
    }
    fg.d3Force("center", null);
    fg.d3Force("collide", null);
    fg.d3Force("cluster", null);

    // Center viewport on the user / first seed at default zoom.
    const seedNode = graphData.nodes.find((n) => n.is_seed);
    if (seedNode && typeof fg.centerAt === "function") {
      fg.centerAt(seedNode.x ?? 0, seedNode.y ?? 0, 0);
      if (typeof fg.zoom === "function") {
        fg.zoom(0.45, 0);
      }
    }
  }, [graphData]);

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
      const x = node.x ?? 0;
      const y = node.y ?? 0;

      if (isState) {
        const w = STATE_WIDTH;
        const h = STATE_HEIGHT;
        roundRect(ctx, x - w / 2, y - h / 2, w, h, h / 2);
        ctx.fillStyle = "#f3f4f6";
        ctx.fill();
        ctx.lineWidth = 1 / globalScale;
        ctx.strokeStyle = "#d1d5db";
        ctx.stroke();

        const fontSize = 8;
        ctx.font = `${fontSize}px Inter, system-ui, sans-serif`;
        ctx.fillStyle = "#6b7280";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        const text = fitLabel(node.label, w - 6, fontSize * 0.55);
        ctx.fillText(text, x, y);
        return;
      }

      if (node.photo_url) {
        let img = photoCache.current.get(node.photo_url);
        if (!img) {
          img = new Image();
          img.src = node.photo_url;
          photoCache.current.set(node.photo_url, img);
        }
        const halfSize = PHOTO_SIZE / 2;
        ctx.lineWidth = (isSeed ? 3 : 1) / globalScale;
        ctx.strokeStyle = isSeed ? "#2563eb" : "#e5e7eb";
        roundRect(ctx, x - halfSize, y - halfSize, PHOTO_SIZE, PHOTO_SIZE, 6);
        ctx.stroke();

        if (img.complete && img.naturalWidth > 0) {
          ctx.save();
          roundRect(ctx, x - halfSize, y - halfSize, PHOTO_SIZE, PHOTO_SIZE, 6);
          ctx.clip();
          ctx.drawImage(img, x - halfSize, y - halfSize, PHOTO_SIZE, PHOTO_SIZE);
          ctx.restore();
        } else {
          ctx.fillStyle = "#f3f4f6";
          roundRect(ctx, x - halfSize, y - halfSize, PHOTO_SIZE, PHOTO_SIZE, 6);
          ctx.fill();
        }

        const fontSize = 11;
        ctx.font = `500 ${fontSize}px Inter, system-ui, sans-serif`;
        ctx.fillStyle = "#111827";
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillText(node.label, x, y + halfSize + 6);
        return;
      }

      const w = entityWidth(node.pagerank_score);
      const h = ENTITY_HEIGHT;
      let fill = "#ffffff";
      let stroke = "#d1d5db";
      let textColor = "#111827";
      if (isSeed) {
        fill = "#2563eb";
        stroke = "#1d4ed8";
        textColor = "#ffffff";
      } else if (node.node_type === "Event") {
        stroke = "#9ca3af";
      }

      roundRect(ctx, x - w / 2, y - h / 2, w, h, 8);
      ctx.fillStyle = fill;
      ctx.fill();
      ctx.lineWidth = (isSeed ? 2 : 1) / globalScale;
      ctx.strokeStyle = stroke;
      ctx.stroke();

      const fontSize = 12;
      ctx.font = `500 ${fontSize}px Inter, system-ui, sans-serif`;
      ctx.fillStyle = textColor;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      const text = fitLabel(node.label, w - 12, fontSize * 0.55);
      ctx.fillText(text, x, y);
    },
    [],
  );

  const drawLink = useCallback(
    (link: ForceLink, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const src = link.source as ForceNode;
      const tgt = link.target as ForceNode;
      if (
        !src ||
        !tgt ||
        src.x === undefined ||
        src.y === undefined ||
        tgt.x === undefined ||
        tgt.y === undefined
      ) {
        return;
      }

      // Viewport-cull: if either endpoint is offscreen (with a small margin
      // for edges that originate from a just-offscreen node), skip drawing.
      // Without this, panning shows edges flying off into emptiness.
      const fg = fgRef.current;
      if (fg && typeof fg.screen2GraphCoords === "function") {
        const canvas = ctx.canvas;
        const tl = fg.screen2GraphCoords(0, 0);
        const br = fg.screen2GraphCoords(canvas.width, canvas.height);
        const margin = 80 / globalScale;
        const minX = Math.min(tl.x, br.x) - margin;
        const maxX = Math.max(tl.x, br.x) + margin;
        const minY = Math.min(tl.y, br.y) - margin;
        const maxY = Math.max(tl.y, br.y) + margin;
        const sIn =
          src.x >= minX && src.x <= maxX && src.y >= minY && src.y <= maxY;
        const tIn =
          tgt.x >= minX && tgt.x <= maxX && tgt.y >= minY && tgt.y <= maxY;
        if (!sIn || !tIn) return;
      }

      drawOrthogonalLink(ctx, src, tgt, globalScale);
    },
    [],
  );

  const nodePointerArea = useCallback(
    (node: ForceNode, color: string, ctx: CanvasRenderingContext2D) => {
      const x = node.x ?? 0;
      const y = node.y ?? 0;
      const isState = STATE_TYPES.has(node.node_type);
      if (isState) {
        roundRect(
          ctx,
          x - STATE_WIDTH / 2,
          y - STATE_HEIGHT / 2,
          STATE_WIDTH,
          STATE_HEIGHT,
          STATE_HEIGHT / 2,
        );
      } else if (node.photo_url) {
        roundRect(ctx, x - PHOTO_SIZE / 2, y - PHOTO_SIZE / 2, PHOTO_SIZE, PHOTO_SIZE, 6);
      } else {
        const w = entityWidth(node.pagerank_score);
        roundRect(ctx, x - w / 2, y - ENTITY_HEIGHT / 2, w, ENTITY_HEIGHT, 8);
      }
      ctx.fillStyle = color;
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
      linkCanvasObject={drawLink}
      linkCanvasObjectMode={() => "replace"}
      nodeCanvasObject={drawNode}
      nodePointerAreaPaint={nodePointerArea}
      onNodeClick={handleClick}
      cooldownTicks={0}
      warmupTicks={0}
      d3AlphaDecay={1}
      d3VelocityDecay={1}
      enableZoomInteraction
      enablePanInteraction
    />
  );
}
