import { useCallback, useMemo, useRef } from "react";
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

// Visual hierarchy: entities are rounded-rectangle boxes; states/goals are
// small subdued chips that read as edge-decorations. Person entities with
// photos get a square photo + name below.
const STATE_TYPES = new Set(["State", "Goal"]);

// Size-from-importance heuristic. PageRank scores are in roughly
// [0, 0.3] for the seed-set graphs we render; this maps that into a
// useful pixel-width range.
function entityWidth(prScore: number): number {
  return 70 + Math.min(60, prScore * 250);
}

const ENTITY_HEIGHT = 36;
const STATE_WIDTH = 56;
const STATE_HEIGHT = 16;
const PHOTO_SIZE = 44;

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

// Truncate a label to fit within a max pixel width at a given font size.
// Cheap heuristic — assumes ~6.5px per char at 12px font.
function fitLabel(label: string, maxWidth: number, charPx: number): string {
  const maxChars = Math.max(4, Math.floor(maxWidth / charPx));
  if (label.length <= maxChars) return label;
  return label.slice(0, maxChars - 1) + "…";
}

export function GraphCanvas({ nodes, edges, onNodeClick }: Props) {
  // Cache loaded photo HTMLImageElements so we don't kick off a new fetch
  // per render frame. Keyed by photo URL.
  const photoCache = useRef<Map<string, HTMLImageElement>>(new Map());

  const graphData = useMemo(() => {
    const fNodes: ForceNode[] = nodes.map((n) => ({
      id: n.id,
      label: n.label,
      node_type: n.node_type,
      pagerank_score: n.pagerank_score,
      is_seed: n.is_seed,
      importance: n.importance,
      // photo_url plumbed in but not yet returned by the backend; will be
      // wired alongside the /api/me/photo/<id> endpoint.
      photo_url: null,
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
      const x = (node as ForceNode & { x: number }).x;
      const y = (node as ForceNode & { y: number }).y;

      if (isState) {
        // ---- State / Goal: small subdued chip with single-word label ----
        const w = STATE_WIDTH;
        const h = STATE_HEIGHT;
        roundRect(ctx, x - w / 2, y - h / 2, w, h, h / 2);
        ctx.fillStyle = "#f3f4f6"; // gray-100
        ctx.fill();
        ctx.lineWidth = 1 / globalScale;
        ctx.strokeStyle = "#d1d5db"; // gray-300
        ctx.stroke();

        const fontSize = 8;
        ctx.font = `${fontSize}px Inter, system-ui, sans-serif`;
        ctx.fillStyle = "#6b7280"; // gray-500
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        const text = fitLabel(node.label, w - 6, fontSize * 0.55);
        ctx.fillText(text, x, y);
        return;
      }

      // ---- Person with photo: square thumbnail + name below ----
      if (node.photo_url) {
        const cached = photoCache.current.get(node.photo_url);
        let img = cached;
        if (!img) {
          img = new Image();
          img.src = node.photo_url;
          photoCache.current.set(node.photo_url, img);
        }

        const size = PHOTO_SIZE;
        const halfSize = size / 2;

        // Subtle border + accent for seed.
        ctx.lineWidth = (isSeed ? 3 : 1) / globalScale;
        ctx.strokeStyle = isSeed ? "#2563eb" : "#e5e7eb";
        roundRect(ctx, x - halfSize, y - halfSize, size, size, 6);
        ctx.stroke();

        if (img.complete && img.naturalWidth > 0) {
          ctx.save();
          roundRect(ctx, x - halfSize, y - halfSize, size, size, 6);
          ctx.clip();
          ctx.drawImage(img, x - halfSize, y - halfSize, size, size);
          ctx.restore();
        } else {
          ctx.fillStyle = "#f3f4f6";
          roundRect(ctx, x - halfSize, y - halfSize, size, size, 6);
          ctx.fill();
        }

        // Name below the photo.
        const fontSize = 11;
        ctx.font = `500 ${fontSize}px Inter, system-ui, sans-serif`;
        ctx.fillStyle = "#111827";
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillText(node.label, x, y + halfSize + 6);
        return;
      }

      // ---- Entity (default): rounded rectangle with title centered ----
      const w = entityWidth(node.pagerank_score);
      const h = ENTITY_HEIGHT;

      // Quiet palette. Seeds get accent fill; everything else is white-on-border.
      let fill = "#ffffff";
      let stroke = "#d1d5db"; // gray-300
      let textColor = "#111827"; // gray-900
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

  const nodePointerArea = useCallback(
    (node: ForceNode, color: string, ctx: CanvasRenderingContext2D) => {
      const isState = STATE_TYPES.has(node.node_type);
      const x = (node as ForceNode & { x: number }).x;
      const y = (node as ForceNode & { y: number }).y;

      if (isState) {
        roundRect(ctx, x - STATE_WIDTH / 2, y - STATE_HEIGHT / 2, STATE_WIDTH, STATE_HEIGHT, STATE_HEIGHT / 2);
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
      graphData={graphData}
      nodeId="id"
      linkSource="source"
      linkTarget="target"
      backgroundColor="#ffffff"
      linkColor={() => "rgba(107, 114, 128, 0.35)"}
      linkWidth={(link: ForceLink) => 0.6 + (link.importance ?? 0.5) * 1.2}
      nodeCanvasObject={drawNode}
      nodePointerAreaPaint={nodePointerArea}
      onNodeClick={handleClick}
      cooldownTicks={120}
      d3AlphaDecay={0.04}
      d3VelocityDecay={0.32}
      enableZoomInteraction
      enablePanInteraction
    />
  );
}
