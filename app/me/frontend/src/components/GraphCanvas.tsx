import { useCallback, useEffect, useMemo, useRef } from "react";
import ForceGraph2D from "react-force-graph-2d";
import { LensEdge, LensNode } from "../types";

interface ForceNode {
  id: string;
  label: string;
  node_type: string;
  pagerank_score: number;
  is_seed: boolean;
  importance: number;
  photo_url: string | null;
  primary_seed_id: string | null;
  // d3-force runtime fields (mutated by the engine).
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
const SEED_RING_RADIUS = 320;
const COLLIDE_PADDING = 14;
const CLUSTER_STRENGTH = 0.075;

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
// the half-diagonal of the bounding rect plus padding. State chips are tight,
// entity boxes are loose so neighborhoods can spread.
function collideRadius(n: ForceNode): number {
  if (STATE_TYPES.has(n.node_type)) {
    return Math.hypot(STATE_WIDTH / 2, STATE_HEIGHT / 2) + COLLIDE_PADDING;
  }
  if (n.photo_url) {
    return Math.hypot(PHOTO_SIZE / 2, PHOTO_SIZE / 2 + 14) + COLLIDE_PADDING;
  }
  const w = entityWidth(n.pagerank_score);
  return Math.hypot(w / 2, ENTITY_HEIGHT / 2) + COLLIDE_PADDING;
}

// Compute the entry point on a node's bounding rectangle along the line
// from the node center to (toX, toY). Used so links land on the box edge,
// not on the box center (which they'd otherwise overlap).
function rectEdgeAnchor(
  cx: number,
  cy: number,
  halfW: number,
  halfH: number,
  toX: number,
  toY: number,
): { x: number; y: number } {
  const dx = toX - cx;
  const dy = toY - cy;
  if (dx === 0 && dy === 0) return { x: cx, y: cy };
  const ax = Math.abs(dx);
  const ay = Math.abs(dy);
  // Scale to land on the rectangle's edge.
  const sx = ax === 0 ? Infinity : halfW / ax;
  const sy = ay === 0 ? Infinity : halfH / ay;
  const s = Math.min(sx, sy);
  return { x: cx + dx * s, y: cy + dy * s };
}

function nodeFootprint(n: ForceNode): { halfW: number; halfH: number; bottom: number } {
  if (STATE_TYPES.has(n.node_type)) {
    return { halfW: STATE_WIDTH / 2, halfH: STATE_HEIGHT / 2, bottom: STATE_HEIGHT / 2 };
  }
  if (n.photo_url) {
    // Photo + label below; treat the visual block as the rectangle plus label.
    return { halfW: PHOTO_SIZE / 2, halfH: PHOTO_SIZE / 2, bottom: PHOTO_SIZE / 2 + 14 };
  }
  const w = entityWidth(n.pagerank_score);
  return { halfW: w / 2, halfH: ENTITY_HEIGHT / 2, bottom: ENTITY_HEIGHT / 2 };
}

// Orthogonal routing with rounded corners. Path: from source edge anchor,
// goes one axis, bends, goes the other axis, ends at target edge anchor.
// For shorter horizontal/vertical pairs, a single bend; for diagonals, two
// bends produce a soft staircase shape.
function drawOrthogonalLink(
  ctx: CanvasRenderingContext2D,
  s: { x: number; y: number },
  t: { x: number; y: number },
  globalScale: number,
) {
  const dx = t.x - s.x;
  const dy = t.y - s.y;
  const ax = Math.abs(dx);
  const ay = Math.abs(dy);
  const radius = Math.min(14, Math.min(ax, ay) / 3, 14);

  ctx.beginPath();
  ctx.moveTo(s.x, s.y);

  if (ax < 1 || ay < 1) {
    // Pure vertical or horizontal — one straight segment.
    ctx.lineTo(t.x, t.y);
  } else if (ax > ay * 2.2) {
    // Mostly horizontal — single H/V bend at midpoint.
    const midX = s.x + dx / 2;
    ctx.lineTo(midX - Math.sign(dx) * radius, s.y);
    ctx.quadraticCurveTo(midX, s.y, midX, s.y + Math.sign(dy) * radius);
    ctx.lineTo(midX, t.y - Math.sign(dy) * radius);
    ctx.quadraticCurveTo(midX, t.y, midX + Math.sign(dx) * radius, t.y);
    ctx.lineTo(t.x, t.y);
  } else if (ay > ax * 2.2) {
    // Mostly vertical — single V/H bend at midpoint.
    const midY = s.y + dy / 2;
    ctx.lineTo(s.x, midY - Math.sign(dy) * radius);
    ctx.quadraticCurveTo(s.x, midY, s.x + Math.sign(dx) * radius, midY);
    ctx.lineTo(t.x - Math.sign(dx) * radius, midY);
    ctx.quadraticCurveTo(t.x, midY, t.x, midY + Math.sign(dy) * radius);
    ctx.lineTo(t.x, t.y);
  } else {
    // Diagonal — staircase: H, V, H with two rounded bends.
    const stepX = dx / 3;
    const x1 = s.x + stepX;
    const x2 = s.x + 2 * stepX;
    ctx.lineTo(x1 - Math.sign(dx) * radius, s.y);
    ctx.quadraticCurveTo(x1, s.y, x1, s.y + Math.sign(dy) * radius);
    ctx.lineTo(x1, t.y - Math.sign(dy) * radius);
    ctx.quadraticCurveTo(x1, t.y, x1 + Math.sign(dx) * radius, t.y);
    // Second bend skipped — single staircase reads cleaner. The variable
    // x2 is left available if we ever want a 3-segment staircase.
    void x2;
    ctx.lineTo(t.x, t.y);
  }

  ctx.lineWidth = 1.0 / globalScale;
  ctx.strokeStyle = "rgba(107, 114, 128, 0.45)";
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
      importance: n.importance,
      photo_url: null,
      primary_seed_id: n.primary_seed_id,
    }));
    const fLinks: ForceLink[] = edges.map((e) => ({
      source: e.source_id,
      target: e.target_id,
      relationship_type: e.relationship_type,
      importance: e.importance,
    }));
    return { nodes: fNodes, links: fLinks };
  }, [nodes, edges]);

  // Pin seeds in a ring around the canvas so each cluster has its own
  // anchor. Then add a cluster force that pulls each non-seed toward its
  // primary seed's pinned position. This produces the "neighborhood" feel.
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;
    const seeds = graphData.nodes.filter((n) => n.is_seed);
    const N = seeds.length;
    if (N === 0) return;

    seeds.forEach((s, i) => {
      if (N === 1) {
        s.fx = 0;
        s.fy = 0;
      } else {
        const angle = (2 * Math.PI * i) / N - Math.PI / 2;
        s.fx = Math.cos(angle) * SEED_RING_RADIUS;
        s.fy = Math.sin(angle) * SEED_RING_RADIUS;
      }
    });
    // Non-seed nodes: clear any prior fx/fy so they can be pulled.
    graphData.nodes.forEach((n) => {
      if (!n.is_seed) {
        n.fx = undefined;
        n.fy = undefined;
      }
    });

    // Build seed lookup once for the cluster force.
    const seedById = new Map<string, ForceNode>();
    seeds.forEach((s) => seedById.set(s.id, s));

    // Custom cluster force: pull each non-seed toward its primary seed.
    const clusterForce = (alpha: number) => {
      graphData.nodes.forEach((n) => {
        if (n.is_seed) return;
        const sid = n.primary_seed_id;
        if (!sid) return;
        const anchor = seedById.get(sid);
        if (!anchor || anchor.x === undefined || anchor.y === undefined) return;
        if (n.x === undefined || n.y === undefined) return;
        if (n.vx === undefined) n.vx = 0;
        if (n.vy === undefined) n.vy = 0;
        n.vx -= (n.x - anchor.x) * alpha * CLUSTER_STRENGTH;
        n.vy -= (n.y - anchor.y) * alpha * CLUSTER_STRENGTH;
      });
    };

    // d3-force has a `force(name, force)` API. The third-party type for
    // ForceGraphMethods is loose here; pull through `any`.
    fg.d3Force("cluster", clusterForce);

    // Bump collision so rectangles stop overlapping.
    // Replace the default circle-collide with a per-node-radius variant.
    // d3-force isn't re-imported here — react-force-graph-2d ships its own
    // d3 internally. We can swap the existing 'collide' force by giving it
    // a function-radius if it exists.
    const collide = fg.d3Force("collide");
    if (collide && typeof collide.radius === "function") {
      collide.radius((n: ForceNode) => collideRadius(n));
      collide.strength(0.95);
      collide.iterations(2);
    }

    // Charge & link tuning.
    const charge = fg.d3Force("charge");
    if (charge && typeof charge.strength === "function") {
      charge.strength(-180);
    }
    const link = fg.d3Force("link");
    if (link && typeof link.distance === "function") {
      link.distance(95).strength(0.35);
    }

    // Re-heat so the changes take effect on the visible frame.
    fg.d3ReheatSimulation();
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
      const sFp = nodeFootprint(src);
      const tFp = nodeFootprint(tgt);
      const sAnchor = rectEdgeAnchor(
        src.x,
        src.y,
        sFp.halfW,
        sFp.halfH,
        tgt.x,
        tgt.y,
      );
      const tAnchor = rectEdgeAnchor(
        tgt.x,
        tgt.y,
        tFp.halfW,
        tFp.halfH,
        src.x,
        src.y,
      );
      drawOrthogonalLink(ctx, sAnchor, tAnchor, globalScale);
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
      cooldownTicks={200}
      d3AlphaDecay={0.025}
      d3VelocityDecay={0.32}
      enableZoomInteraction
      enablePanInteraction
    />
  );
}
