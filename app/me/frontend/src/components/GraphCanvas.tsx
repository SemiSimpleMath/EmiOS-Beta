import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D from "react-force-graph-2d";
import { LensBridgeEdge, LensEdge, LensNode } from "../types";

interface ForceNode {
  id: string;
  label: string;
  node_type: string;
  pagerank_score: number;
  is_seed: boolean;
  is_anchor: boolean;
  importance: number;
  llm_importance: number;
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
  is_bridge: boolean;
  bridge_label: string;
  // For bridge edges: id of the via-state being bridged. If that state is
  // currently visible (opacity > 0), we suppress the bridge — real edges
  // through the visible state already show the connection.
  via_node_id: string | null;
}

interface Props {
  nodes: LensNode[];
  edges: LensEdge[];
  bridgeEdges: LensBridgeEdge[];
  onNodeClick: (node: LensNode, event: MouseEvent) => void;
}

// LOD is *physics-style*: each node has a `claim_radius` in graph
// coords. At zoom Z, claim_screen = claim × Z pixels. Iterate nodes in
// priority order (seed → anchor → by importance); each renders only if
// its on-screen claim doesn't overlap any already-visible higher-priority
// node. Zoom in → claims shrink relative to viewport → more nodes fit
// → they reveal organically.
//
// Wheel becomes pure (gentle) camera zoom. No threshold state.
// ---------- Logical scroll (linear radial slide) ----------
//
// Wheel input updates a single `currentScroll` value (graph units).
// Each entity's radius from the anchor is linear in that scroll value:
//
//   r = ENTRY_RADIUS + currentScroll − batch × SCROLL_PER_BATCH
//
// Entity is visible only when ENTRY_RADIUS ≤ r ≤ EXIT_RADIUS, with a
// small fade band at each edge.
//
// At currentScroll=0:
//   • batch 0 at r = ENTRY_RADIUS (just appeared at the inner edge)
//   • batch 1 at r = ENTRY_RADIUS − SCROLL_PER_BATCH (hidden inward)
//
// As currentScroll grows, batch 0 slides outward. When it reaches
// EXIT_RADIUS (currentScroll = SCROLL_PER_BATCH), batch 1 reaches
// ENTRY_RADIUS — fading in just as batch 0 fades out at EXIT_RADIUS.
// Brief crossfade, then batch 1 alone, then etc.
const ENTRY_RADIUS = 200;          // batch enters here, fades in
const EXIT_RADIUS = 600;           // batch leaves here, fades out
const FADE_BAND = 60;              // fade-in/out distance at each edge
// BATCH_TRIGGER < (EXIT − ENTRY) so consecutive batches OVERLAP — the
// next batch starts fading in at ENTRY before the current batch has
// reached EXIT. Without this, there's an awkward dead window between
// outer disappearing and inner appearing.
const BATCH_TRIGGER = 280;
const BATCH_SIZE = 14;             // entities per batch (ring)
const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));   // angular step
// Wheel UP = zoom in = entities move OUTWARD = currentScroll grows.
const SCROLL_PER_PIXEL = 0.25;

// ---------- Intersection mode (2+ seeds) ----------
// Layout: anchors at left/right of canvas, shared items in a vertical
// column down the middle. Scroll = page through batches — nodes do
// NOT move; current batch fades out, brief blank gap, next fades in.
//
// Hierarchy of items in the column (page 0 first, then later pages):
//   1. Direct shared State/Event/Goal nodes (connected to ALL seeds)
//      — the literal one-hop relationships BETWEEN the anchors
//      (Marriage, Family Time, Parent of Annika ...).
//   2. Shared entities (in every seed's 1-hop neighborhood) — people /
//      places / things both anchors connect to.
const ISEC_ANCHOR_X = 320;            // |x| of L/R anchors
const ISEC_COLUMN_X = 0;              // x of the middle column
const ISEC_COLUMN_HALF_HEIGHT = 360;  // taller column → more breathing room
const ISEC_BATCH_SIZE = 10;           // items per page
const ISEC_SCROLL_PER_BATCH = 100;    // graph units per page
// Crossfade with a hard gap: full opacity within ±FULL of the current
// batch index; linear fade over FADE; zero beyond that. Total visible
// width per batch = 2*(FULL+FADE). With FULL=0.25, FADE=0.10, each
// batch holds full opacity over 0.5 of a batch-step and is fully off
// after 0.7 — leaving a clean 0.3-wide blank gap between consecutive
// batches.
const ISEC_BATCH_FULL_HALFWIDTH = 0.25;
const ISEC_BATCH_FADE_HALFWIDTH = 0.10;

// Visibility set is computed in the component (depends on logicalZoom).
// This module-level shim lets the static draw functions read whether a
// given node id is currently visible without threading state through
// every callback dependency list.
let currentVisibleOpacity: Map<string, number> = new Map();
// `showLines` toggle mirrored to module scope so drawLink (a useCallback
// with empty deps) can read it without re-binding on every toggle change.
let currentShowLines = true;
// Node lookup mirror — drawLink needs to resolve link.source / link.target
// even when d3-force hasn't replaced the string id with a node ref yet
// (graphData rebuilds on every scroll tick, and d3 doesn't always re-bind
// in lockstep). Without this, every link fails the `src.x === undefined`
// early-return and no lines render at all.
let currentNodeById: Map<string, ForceNode> = new Map();

function visibilityOpacity(id: string): number {
  return currentVisibleOpacity.get(id) ?? 0;
}

function resolveEndpoint(ref: string | ForceNode): ForceNode | null {
  if (typeof ref === "string") {
    return currentNodeById.get(ref) ?? null;
  }
  if (ref && (ref as ForceNode).x !== undefined) return ref as ForceNode;
  // d3-force may have wrapped it but not yet positioned — fall back to
  // the lookup which has the (x, y) we computed in graphData.
  const id = (ref as ForceNode)?.id;
  if (id) return currentNodeById.get(id) ?? (ref as ForceNode);
  return null;
}

/**
 * Per-node claim radius in **screen pixels** (NOT graph coords).
 * Constant regardless of zoom — the claim disk's on-screen size stays
 * the same as you zoom. So at low zoom (tiny on-screen distances), the
 * claims overlap heavily and priority wins; at high zoom (large
 * on-screen distances), nodes naturally fit without collision.
 *
 * Sizes match the rendered bbox half-extent so collision aligns with
 * what the user actually sees overlapping.
 */
// Visual hierarchy: entities = rounded-rectangle boxes; states/events/goals
// = small subdued chips that read as edge-decorations (the connecting
// tissue between entities, not entities themselves).
const STATE_TYPES = new Set(["State", "Event", "Goal"]);

function entityWidth(node: { llm_importance?: number; pagerank_score: number; label?: string }): number {
  // Width grows with LLM importance (0-10 scale → 55-115 px). PageRank
  // is a tiebreaker for unrated nodes. Width is bumped if the label
  // needs more room — but capped tighter than before so the lens can
  // fit more entities per ring.
  const imp = node.llm_importance ?? 5.0;
  const fromImp = 55 + (imp / 10.0) * 60; // 55..115
  const fromPr = Math.min(15, node.pagerank_score * 60);
  const baseW = Math.min(130, fromImp + fromPr);
  const labelLen = (node.label ?? "").length;
  const LABEL_BASED_MAX = 180;
  const fromLabel = Math.min(LABEL_BASED_MAX, labelLen * 6 + 14);
  return Math.max(baseW, fromLabel);
}

const ENTITY_HEIGHT = 30;
const STATE_WIDTH = 48;
const STATE_HEIGHT = 14;
const PHOTO_SIZE = 38;

// Seed renders at fixed screen size regardless of zoom — the focal node
// should be a stable visual anchor. Hoisted from drawNodeInner so the
// claim-radius helper can mirror the actual rendered footprint.
const SEED_SCREEN_W = 80;
const SEED_SCREEN_H = 34;

// Screen-pixel bounds per tier. Above natural size we clamp DOWN so the
// node doesn't blow up when zoomed in; below natural size we clamp UP so
// the node doesn't shrink past readability when zoomed out.
const PERSON_MIN_SCREEN_W = 50;
const PERSON_MAX_SCREEN_W = 180;
const PERSON_MIN_SCREEN_H = 20;
const PERSON_MAX_SCREEN_H = 48;

const PERSON_MIN_FONT_PX = 11;
const PERSON_MAX_FONT_PX = 22;
const STATE_MIN_FONT_PX = 8;
const STATE_MAX_FONT_PX = 14;

/**
 * Clamp the on-screen pixel size of a graph-coord measurement so it stays
 * readable when zoomed out and doesn't blow up when zoomed in. Returns the
 * graph-coord size to actually draw at given the current globalScale.
 */
function clampScreen(
  graphSize: number,
  globalScale: number,
  minPx: number,
  maxPx: number,
): number {
  const naturalScreen = graphSize * globalScale;
  const clamped = Math.max(minPx, Math.min(maxPx, naturalScreen));
  return clamped / globalScale;
}

// Map-mode layout: every node is pinned at its global (x, y). No force
// sim runs.

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

/**
 * Wrap a label into up to 2 lines if it doesn't fit on one. Each line is
 * capped at maxCharsPerLine; the second line is truncated with "…" if it
 * still overflows. Used for State chips so "Morning Routine" reads as
 * two lines instead of "Morning…".
 */
function wrapLabel(
  label: string,
  maxWidth: number,
  charPx: number,
): string[] {
  const maxChars = Math.max(4, Math.floor(maxWidth / charPx));
  if (label.length <= maxChars) return [label];
  // Try to break on a space near the middle.
  const target = Math.ceil(label.length / 2);
  let breakAt = -1;
  for (let i = 0; i < label.length; i++) {
    if (label[i] === " " && Math.abs(i - target) < Math.abs(breakAt - target)) {
      breakAt = i;
    }
  }
  let line1: string;
  let line2: string;
  if (breakAt > 0 && breakAt < label.length - 1) {
    line1 = label.slice(0, breakAt);
    line2 = label.slice(breakAt + 1);
  } else {
    // No good break point — split mid-word.
    const split = Math.min(maxChars, target);
    line1 = label.slice(0, split);
    line2 = label.slice(split);
  }
  if (line1.length > maxChars) line1 = line1.slice(0, maxChars - 1) + "…";
  if (line2.length > maxChars) line2 = line2.slice(0, maxChars - 1) + "…";
  return [line1, line2];
}

// Footprint radius for collision purposes. Nodes are not circles, so we use
function nodeFootprint(n: ForceNode): { halfW: number; halfH: number } {
  if (STATE_TYPES.has(n.node_type)) {
    return { halfW: STATE_WIDTH / 2, halfH: STATE_HEIGHT / 2 };
  }
  if (n.photo_url) {
    return { halfW: PHOTO_SIZE / 2, halfH: PHOTO_SIZE / 2 };
  }
  const w = entityWidth(n);
  return { halfW: w / 2, halfH: ENTITY_HEIGHT / 2 };
}

/**
 * Straight-line link from edge of source bbox to edge of target bbox.
 * Drops the orthogonal routing — with hundreds of links per cluster
 * the right-angle bends create visual noise. Straight lines scale
 * cleanly with edge density and let the eye focus on endpoint identity
 * rather than path geometry.
 */
function drawStraightLink(
  ctx: CanvasRenderingContext2D,
  src: ForceNode,
  tgt: ForceNode,
  globalScale: number,
) {
  const sx = src.x ?? 0;
  const sy = src.y ?? 0;
  const tx = tgt.x ?? 0;
  const ty = tgt.y ?? 0;
  // Trim line endpoints to the source/target bbox edges so the line
  // doesn't disappear under the box. Approximate with bbox half-width
  // along the link direction.
  const sFp = nodeFootprint(src);
  const tFp = nodeFootprint(tgt);
  const dx = tx - sx;
  const dy = ty - sy;
  const dist = Math.hypot(dx, dy);
  if (dist < 1) return;
  // Scale the bbox extent in the line's direction by projecting
  // (halfW, halfH) onto the unit vector. This is a conservative
  // approximation — fine for visual purposes.
  const ux = dx / dist;
  const uy = dy / dist;
  const sExtent = Math.abs(sFp.halfW * ux) + Math.abs(sFp.halfH * uy);
  const tExtent = Math.abs(tFp.halfW * ux) + Math.abs(tFp.halfH * uy);
  const startX = sx + ux * sExtent;
  const startY = sy + uy * sExtent;
  const endX = tx - ux * tExtent;
  const endY = ty - uy * tExtent;
  // If the line trim collapsed the segment (boxes overlap), skip.
  if ((endX - startX) * ux + (endY - startY) * uy <= 0) return;
  ctx.beginPath();
  ctx.moveTo(startX, startY);
  ctx.lineTo(endX, endY);
  ctx.lineWidth = 1.0 / globalScale;
  ctx.strokeStyle = "rgba(107, 114, 128, 0.4)";
  ctx.stroke();
}

export function GraphCanvas({
  nodes: propNodes,
  edges,
  bridgeEdges,
  onNodeClick,
}: Props) {
  // Two independent toggles, defaults: lines ON, states OFF.
  // - showLines: edges drawn as labeled lines from anchor to entities.
  //   Off = pure neighborhood layout, no relationship-type info.
  // - showStates: state/event/goal chips at midpoints, picked as the
  //   single most-important state mediating each (seed, entity) pair.
  const [showLines, setShowLines] = useState(true);
  const [showStates, setShowStates] = useState(false);
  // Intersection-mode level toggles. Default both on; turning one off
  // collapses the column to just that hop level. With both off the
  // column is empty (only the two anchors render).
  const [showZeroHop, setShowZeroHop] = useState(true);
  const [showOneHop, setShowOneHop] = useState(true);

  // 0-hop intersection: State/Event/Goal nodes connected DIRECTLY to
  // every seed (Marriage, Parenthood, Family Time, Co-residence, ...).
  // These are the actual one-hop relationships BETWEEN the two anchors.
  // Returns null in single-seed mode.
  const directSharedStateIds = useMemo<Set<string> | null>(() => {
    const seedIds = propNodes.filter((n) => n.is_seed).map((n) => n.id);
    if (seedIds.length < 2) return null;
    const result = new Set<string>();
    for (const n of propNodes) {
      if (!STATE_TYPES.has(n.node_type)) continue;
      const stateNeighbors = new Set<string>();
      for (const e of edges) {
        if (e.source_id === n.id) stateNeighbors.add(e.target_id);
        else if (e.target_id === n.id) stateNeighbors.add(e.source_id);
      }
      if (seedIds.every((sid) => stateNeighbors.has(sid))) {
        result.add(n.id);
      }
    }
    return result;
  }, [propNodes, edges]);

  // 1-hop intersection. With 2+ seeds, restrict the rendered entity
  // set to the entities that appear in EVERY seed's 1-hop neighborhood —
  // "show me what these people share". With 0 or 1 seeds returns null
  // (no filter).
  //
  // 1-hop neighborhood = entities directly connected by an edge OR
  // entities reachable through a single intermediating State/Event/Goal.
  // The state-mediated path matters because most real KG connections
  // between two people run through a shared state (Marriage, Friendship,
  // Co-residence) rather than a direct entity↔entity edge.
  const intersectionEntityIds = useMemo<Set<string> | null>(() => {
    const seedIds = propNodes.filter((n) => n.is_seed).map((n) => n.id);
    if (seedIds.length < 2) return null;

    const nodeById = new Map(propNodes.map((n) => [n.id, n] as const));

    const neighborhoodOf = (seedId: string): Set<string> => {
      const out = new Set<string>();
      // Direct entity neighbors
      for (const e of edges) {
        let other: string | null = null;
        if (e.source_id === seedId) other = e.target_id;
        else if (e.target_id === seedId) other = e.source_id;
        if (!other || other === seedId) continue;
        const on = nodeById.get(other);
        if (!on) continue;
        if (on.node_type === "Entity") {
          out.add(other);
        } else if (STATE_TYPES.has(on.node_type)) {
          // Walk the state to its other entity neighbors
          for (const e2 of edges) {
            let viaOther: string | null = null;
            if (e2.source_id === other) viaOther = e2.target_id;
            else if (e2.target_id === other) viaOther = e2.source_id;
            if (!viaOther || viaOther === seedId) continue;
            const von = nodeById.get(viaOther);
            if (von && von.node_type === "Entity") out.add(viaOther);
          }
        }
      }
      return out;
    };

    const sets = seedIds.map(neighborhoodOf);
    let inter = sets[0];
    for (let i = 1; i < sets.length; i++) {
      inter = new Set([...inter].filter((x) => sets[i].has(x)));
    }
    return inter;
  }, [propNodes, edges]);

  // Per-pair best-state selection. For each visible Entity X (≠ seed),
  // pick the SINGLE highest-scoring state S that mediates (seed, X).
  // Score = S.llm_importance × avg(edge_seed_S.importance,
  // edge_S_X.importance). One state can win for multiple X's — its
  // edges to all of them render, and its DISPLAY anchor becomes the
  // most-important X (so it sits along that connection's midpoint
  // instead of the most-important neighbor it incidentally has).
  const stateSelection = useMemo(() => {
    const seedNode = propNodes.find((n) => n.is_seed);
    const seedNodeId = seedNode ? seedNode.id : null;
    const nodeById = new Map(propNodes.map((n) => [n.id, n] as const));

    // state.id -> Map<otherEntityId, edge.importance>
    const stateEdges = new Map<string, Map<string, number>>();
    for (const e of edges) {
      const s = nodeById.get(e.source_id);
      const t = nodeById.get(e.target_id);
      if (!s || !t) continue;
      let stateNode: typeof s | null = null;
      let otherNode: typeof s | null = null;
      if (STATE_TYPES.has(s.node_type) && t.node_type === "Entity") {
        stateNode = s;
        otherNode = t;
      } else if (STATE_TYPES.has(t.node_type) && s.node_type === "Entity") {
        stateNode = t;
        otherNode = s;
      } else {
        continue;
      }
      if (!stateEdges.has(stateNode.id)) stateEdges.set(stateNode.id, new Map());
      stateEdges.get(stateNode.id)!.set(otherNode.id, e.importance ?? 0.5);
    }

    // For each X, pick best mediating state.
    const bestStateForEntity = new Map<string, string>();
    if (seedNodeId) {
      for (const x of propNodes) {
        if (x.is_seed || x.node_type !== "Entity") continue;
        let bestState: string | null = null;
        let bestScore = -Infinity;
        for (const [stateId, neighbors] of stateEdges) {
          const eToSeed = neighbors.get(seedNodeId);
          const eToX = neighbors.get(x.id);
          if (eToSeed === undefined || eToX === undefined) continue;
          const stateNode = nodeById.get(stateId);
          if (!stateNode) continue;
          const stateImp = stateNode.llm_importance ?? 5.0;
          const score = stateImp * ((eToSeed + eToX) / 2);
          if (score > bestScore) {
            bestScore = score;
            bestState = stateId;
          }
        }
        if (bestState) bestStateForEntity.set(x.id, bestState);
      }
    }

    // Reverse: state → list of entities it was picked for. Pick its
    // display anchor = the most-important entity in that list so the
    // chip sits along the strongest connection it represents.
    const entitiesPerState = new Map<string, string[]>();
    for (const [x, s] of bestStateForEntity) {
      if (!entitiesPerState.has(s)) entitiesPerState.set(s, []);
      entitiesPerState.get(s)!.push(x);
    }
    const stateAnchor = new Map<string, string>();
    for (const [s, xs] of entitiesPerState) {
      let bestX: string | null = null;
      let bestImp = -Infinity;
      for (const x of xs) {
        const xNode = nodeById.get(x);
        if (!xNode) continue;
        const imp = xNode.llm_importance ?? 5.0;
        if (imp > bestImp) {
          bestImp = imp;
          bestX = x;
        }
      }
      if (bestX) stateAnchor.set(s, bestX);
    }

    return {
      allowedStates: new Set(stateAnchor.keys()),
      stateAnchor,
    };
  }, [propNodes, edges]);

  const nodes = useMemo(() => {
    // Intersection mode: keep seeds + 0-hop direct shared states +
    // 1-hop intersection entities, gated by the per-level toggles.
    // Ignores the showStates toggle (states-as-intersection are
    // their own thing here).
    if (intersectionEntityIds) {
      return propNodes.filter(
        (n) => n.is_seed
          || (showOneHop && intersectionEntityIds.has(n.id))
          || (showZeroHop && (directSharedStateIds?.has(n.id) ?? false)),
      );
    }
    // Single-anchor mode
    if (!showStates) {
      return propNodes.filter((n) => !STATE_TYPES.has(n.node_type));
    }
    return propNodes.filter(
      (n) => !STATE_TYPES.has(n.node_type) || stateSelection.allowedStates.has(n.id),
    );
  }, [propNodes, showStates, stateSelection, intersectionEntityIds, directSharedStateIds, showZeroHop, showOneHop]);

  // Mirror showLines to the module-level shim so the empty-deps drawLink
  // useCallback can read the latest value without re-binding.
  currentShowLines = showLines;
  const photoCache = useRef<Map<string, HTMLImageElement>>(new Map());
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const fgRef = useRef<any>(null);
  // Linear scroll cursor (graph units). Wheel UP increments it; entities
  // slide OUTWARD as it grows, INWARD as it shrinks. Clamped at 0 so
  // you can't scroll past the "most important entities at entry" view.
  const [currentScroll, setCurrentScroll] = useState(0);

  // Wheel: hijacked to update currentScroll. Canvas physical zoom is
  // FIXED at 1.0 (set in initial-mount effect). Pan still works.
  useEffect(() => {
    if (!fgRef.current) return;
    const fgInstance = fgRef.current;
    const canvas: HTMLCanvasElement | null =
      typeof fgInstance.canvas === "function"
        ? fgInstance.canvas()
        : (document.querySelector(".force-graph-container canvas") as HTMLCanvasElement);
    if (!canvas) return;

    const handleWheel = (e: WheelEvent) => {
      e.preventDefault();
      e.stopPropagation();
      // Wheel UP (deltaY < 0) → entities slide outward → currentScroll grows.
      const ds = -e.deltaY * SCROLL_PER_PIXEL;
      setCurrentScroll((s) => Math.max(0, s + ds));
    };

    canvas.addEventListener("wheel", handleWheel, { passive: false, capture: true });
    return () => {
      canvas.removeEventListener("wheel", handleWheel, { capture: true } as EventListenerOptions);
    };
  }, []);

  // Anchor-relative importance: rank entities by edge importance from
  // the anchor's perspective, not by global llm_importance (which is
  // Jukka-centric). For each entity X compute the strongest path from
  // anchor → X — direct edge OR via a single state — and score that.
  //   path importance = edge_anchor_S × edge_S_X  (or just edge_anchor_X)
  //   final = path × X.llm_importance  (llm_importance is the tiebreak)
  // Edge importance is rated from the SOURCE node's POV (per
  // feedback_edge_importance_source_perspective.md), so this naturally
  // answers "what matters around the anchor."
  const anchorImportance = useMemo(() => {
    const seedNode = propNodes.find((n) => n.is_seed);
    const anchorId = seedNode ? seedNode.id : null;
    const score = new Map<string, number>();
    if (!anchorId) return score;
    const nodeById = new Map(propNodes.map((n) => [n.id, n] as const));

    // Direct anchor → entity edges.
    for (const e of edges) {
      let other: LensNode | undefined;
      if (e.source_id === anchorId) other = nodeById.get(e.target_id);
      else if (e.target_id === anchorId) other = nodeById.get(e.source_id);
      if (!other || other.node_type !== "Entity") continue;
      const s = (e.importance ?? 0.5) * (other.llm_importance ?? 5.0);
      score.set(other.id, Math.max(score.get(other.id) ?? 0, s));
    }

    // Via a single state: anchor → S → entity.
    // Build state → (neighbor → edge importance) once, then for each
    // state with an anchor edge, score every other entity neighbor.
    const stateAdj = new Map<string, Map<string, number>>();
    for (const e of edges) {
      const s = nodeById.get(e.source_id);
      const t = nodeById.get(e.target_id);
      if (!s || !t) continue;
      let stateNode: LensNode | null = null;
      let otherNode: LensNode | null = null;
      if (STATE_TYPES.has(s.node_type)) {
        stateNode = s;
        otherNode = t;
      } else if (STATE_TYPES.has(t.node_type)) {
        stateNode = t;
        otherNode = s;
      } else {
        continue;
      }
      if (!stateAdj.has(stateNode.id)) stateAdj.set(stateNode.id, new Map());
      stateAdj.get(stateNode.id)!.set(otherNode.id, e.importance ?? 0.5);
    }
    for (const [stateId, adj] of stateAdj) {
      const eToAnchor = adj.get(anchorId);
      if (eToAnchor === undefined) continue;
      for (const [otherId, eToOther] of adj) {
        if (otherId === anchorId) continue;
        const other = nodeById.get(otherId);
        if (!other || other.node_type !== "Entity") continue;
        void stateId;
        const s = eToAnchor * eToOther * (other.llm_importance ?? 5.0);
        score.set(otherId, Math.max(score.get(otherId) ?? 0, s));
      }
    }

    return score;
  }, [propNodes, edges]);

  // Per-node depth metadata. Computed once per `nodes` change. Each
  // non-seed entity gets an importance rank (0 = top), batch index
  // (rank / BATCH_SIZE), and angular position (Vogel spiral by rank).
  // Ranking uses anchor-relative score above; global llm_importance is
  // the tiebreak so visually-equal scores stay deterministic.
  // States ride the position of the entity they were PICKED FOR (see
  // stateSelection above) — not their most-important neighbor.
  type EntityMeta = { rank: number; batch: number; theta: number };
  const { entityMetaById, stateAnchorEntityById, seedId } = useMemo(() => {
    const seed = propNodes.find((n) => n.is_seed);
    const seedNodeId = seed ? seed.id : null;

    const entityMeta = new Map<string, EntityMeta>();

    // Use propNodes (not the toggle-filtered `nodes`) so entity ranks
    // stay stable when "show states" is toggled — toggling shouldn't
    // shuffle which entity is at which batch.
    // BUT: in intersection mode (2+ seeds) restrict to the intersection
    // set so visible entities occupy contiguous ranks 0..N — no gappy
    // rings from filtered-out entities holding their original positions.
    const entities = propNodes
      .filter((n) => !n.is_seed && n.node_type === "Entity")
      .filter((n) => !intersectionEntityIds || intersectionEntityIds.has(n.id))
      .slice()
      .sort((a, b) => {
        const aa = anchorImportance.get(a.id) ?? 0;
        const bb = anchorImportance.get(b.id) ?? 0;
        if (bb !== aa) return bb - aa;
        const al = a.llm_importance ?? 5.0;
        const bl = b.llm_importance ?? 5.0;
        if (bl !== al) return bl - al;
        return a.id < b.id ? -1 : 1;
      });
    entities.forEach((n, i) => {
      entityMeta.set(n.id, {
        rank: i,
        batch: Math.floor(i / BATCH_SIZE),
        theta: i * GOLDEN_ANGLE,
      });
    });

    return {
      entityMetaById: entityMeta,
      stateAnchorEntityById: stateSelection.stateAnchor,
      seedId: seedNodeId,
    };
  }, [propNodes, stateSelection, anchorImportance, intersectionEntityIds]);

  // Linear radius: r = ENTRY + scroll − batch * BATCH_TRIGGER.
  // BATCH_TRIGGER < (EXIT − ENTRY) so consecutive batches overlap during
  // a crossfade window — inner ring fades in while outer is still partly
  // visible, instead of an awkward dead window in between.
  const radiusForBatch = useCallback(
    (batch: number): number => {
      return ENTRY_RADIUS + currentScroll - batch * BATCH_TRIGGER;
    },
    [currentScroll],
  );

  // Sorted seed-id list, used to deterministically assign anchors to
  // L/R positions in intersection mode (smallest id → left).
  const seedIdsSorted = useMemo(
    () => propNodes.filter((n) => n.is_seed).map((n) => n.id).sort(),
    [propNodes],
  );

  // Unified intersection-mode rank: 0-hop direct shared states first
  // (sorted by importance), then 1-hop intersection entities (sorted
  // by importance). Same column, sequential pages. Toggles gate which
  // hop levels participate in the ranking.
  const intersectionRanks = useMemo<Map<string, number> | null>(() => {
    if (!intersectionEntityIds) return null;
    const nodeById = new Map(propNodes.map((n) => [n.id, n] as const));
    const ranks = new Map<string, number>();
    let r = 0;
    if (showZeroHop && directSharedStateIds) {
      const items = [...directSharedStateIds]
        .map((id) => nodeById.get(id))
        .filter((n): n is LensNode => !!n)
        .sort((a, b) => (b.llm_importance ?? 5) - (a.llm_importance ?? 5));
      for (const n of items) ranks.set(n.id, r++);
    }
    if (showOneHop) {
      const ents = [...intersectionEntityIds]
        .map((id) => nodeById.get(id))
        .filter((n): n is LensNode => !!n)
        .sort((a, b) => (b.llm_importance ?? 5) - (a.llm_importance ?? 5));
      for (const n of ents) ranks.set(n.id, r++);
    }
    return ranks;
  }, [propNodes, intersectionEntityIds, directSharedStateIds, showZeroHop, showOneHop]);

  // Compute (x, y) for each node given the current scroll.
  const positionFor = useCallback(
    (n: LensNode): { x: number; y: number } => {
      // ---- Intersection mode (2+ seeds) ----
      if (intersectionEntityIds) {
        if (n.is_seed) {
          const idx = seedIdsSorted.indexOf(n.id);
          if (idx === 0) return { x: -ISEC_ANCHOR_X, y: 0 };
          if (idx === 1) return { x: +ISEC_ANCHOR_X, y: 0 };
          // 3+ seeds: stack the extras above/below right anchor.
          return { x: +ISEC_ANCHOR_X, y: (idx - 1) * 80 };
        }
        // Both 0-hop states and 1-hop entities use the unified
        // intersection rank for column placement.
        const rank = intersectionRanks?.get(n.id);
        if (rank === undefined) return { x: 0, y: 0 };
        const positionInBatch = rank % ISEC_BATCH_SIZE;
        const slots = Math.max(1, ISEC_BATCH_SIZE - 1);
        const yStep = (ISEC_COLUMN_HALF_HEIGHT * 2) / slots;
        const y = -ISEC_COLUMN_HALF_HEIGHT + positionInBatch * yStep;
        return { x: ISEC_COLUMN_X, y };
      }

      // ---- Single-anchor depth-cursor mode ----
      if (n.is_seed) return { x: 0, y: 0 };
      if (n.node_type === "Entity") {
        const m = entityMetaById.get(n.id);
        if (!m) return { x: 0, y: 0 };
        const r = radiusForBatch(m.batch);
        return { x: r * Math.cos(m.theta), y: r * Math.sin(m.theta) };
      }
      // State / Event / Goal: midpoint between anchor and the most-
      // important entity neighbor, with a perpendicular jitter so
      // co-anchored states don't stack.
      const anchorEntId = stateAnchorEntityById.get(n.id);
      if (!anchorEntId) return { x: 0, y: 0 };
      const m = entityMetaById.get(anchorEntId);
      if (!m) return { x: 0, y: 0 };
      const rEnt = radiusForBatch(m.batch);
      const ex = rEnt * Math.cos(m.theta);
      const ey = rEnt * Math.sin(m.theta);
      const mx = ex / 2;
      const my = ey / 2;
      let h = 0;
      for (let i = 0; i < n.id.length; i++) h = (h * 31 + n.id.charCodeAt(i)) >>> 0;
      const jitter = ((h % 200) / 200 - 0.5) * 60;
      const norm = Math.hypot(ex, ey);
      const px = norm > 0 ? -ey / norm : 0;
      const py = norm > 0 ? ex / norm : 0;
      return { x: mx + px * jitter, y: my + py * jitter };
    },
    [entityMetaById, stateAnchorEntityById, radiusForBatch,
     intersectionEntityIds, intersectionRanks, seedIdsSorted],
  );

  // Visibility:
  //   • Hard hide outside [ENTRY_RADIUS, EXIT_RADIUS].
  //   • Fade in over FADE_BAND just past ENTRY_RADIUS.
  //   • Fade out over FADE_BAND just before EXIT_RADIUS.
  //   • State opacity = its anchor-entity's opacity × 0.7 (subordinate).
  const visibleOpacity = useMemo(() => {
    const op = new Map<string, number>();

    // ---- Intersection mode: batch-paged crossfade with a hard gap ----
    // Both 0-hop states and 1-hop entities use the unified intersection
    // rank. The gap formula gives full opacity over a flat plateau,
    // then a short fade to zero — leaves a visible blank window
    // between consecutive batches so it's obvious the page changed.
    if (intersectionEntityIds) {
      const currentBatchFloat = currentScroll / ISEC_SCROLL_PER_BATCH;
      const opacityForBatch = (batch: number): number => {
        const dist = Math.abs(batch - currentBatchFloat);
        if (dist <= ISEC_BATCH_FULL_HALFWIDTH) return 1;
        if (dist >= ISEC_BATCH_FULL_HALFWIDTH + ISEC_BATCH_FADE_HALFWIDTH) return 0;
        return 1 - (dist - ISEC_BATCH_FULL_HALFWIDTH) / ISEC_BATCH_FADE_HALFWIDTH;
      };
      for (const n of nodes) {
        if (n.is_seed) {
          op.set(n.id, 1);
          continue;
        }
        const rank = intersectionRanks?.get(n.id);
        if (rank === undefined) continue;
        const batch = Math.floor(rank / ISEC_BATCH_SIZE);
        op.set(n.id, opacityForBatch(batch));
      }
      return op;
    }

    // ---- Single-anchor depth-cursor mode ----
    const radialAlpha = (r: number) => {
      if (r < ENTRY_RADIUS) return 0;
      if (r < ENTRY_RADIUS + FADE_BAND) return (r - ENTRY_RADIUS) / FADE_BAND;
      if (r > EXIT_RADIUS) return 0;
      if (r > EXIT_RADIUS - FADE_BAND) return (EXIT_RADIUS - r) / FADE_BAND;
      return 1;
    };
    for (const n of nodes) {
      if (n.is_seed) {
        op.set(n.id, 1);
        continue;
      }
      if (n.node_type === "Entity") {
        const m = entityMetaById.get(n.id);
        if (!m) continue;
        op.set(n.id, radialAlpha(radiusForBatch(m.batch)));
        continue;
      }
      const anchorEntId = stateAnchorEntityById.get(n.id);
      if (!anchorEntId) continue;
      const m = entityMetaById.get(anchorEntId);
      if (!m) continue;
      op.set(n.id, radialAlpha(radiusForBatch(m.batch)) * 0.7);
    }
    return op;
  }, [nodes, entityMetaById, stateAnchorEntityById, radiusForBatch,
      intersectionEntityIds, intersectionRanks, currentScroll]);

  const visibleIds = useMemo(
    () => new Set(Array.from(visibleOpacity.entries()).filter(([, v]) => v > 0).map(([k]) => k)),
    [visibleOpacity],
  );

  currentVisibleOpacity = visibleOpacity;
  void visibleIds;

  const graphData = useMemo(() => {
    const fNodes: ForceNode[] = nodes.map((n) => {
      const { x, y } = positionFor(n);
      return {
        id: n.id,
        label: n.label,
        node_type: n.node_type,
        pagerank_score: n.pagerank_score,
        is_seed: n.is_seed,
        is_anchor: n.is_anchor,
        importance: n.importance,
        llm_importance: n.llm_importance ?? 5.0,
        photo_url: null,
        primary_anchor_id: n.primary_anchor_id,
        x,
        y,
        fx: x,
        fy: y,
      };
    });
    const realLinks: ForceLink[] = edges.map((e) => ({
      source: e.source_id,
      target: e.target_id,
      relationship_type: e.relationship_type,
      importance: e.importance,
      is_bridge: false,
      bridge_label: "",
      via_node_id: null,
    }));
    const bridgeLinks: ForceLink[] = bridgeEdges.map((b) => ({
      source: b.source_id,
      target: b.target_id,
      relationship_type: "",
      importance: 0.6,
      is_bridge: true,
      bridge_label: b.label,
      via_node_id: b.via_node_id,
    }));
    // Mirror node lookup so drawLink can resolve string ids → ForceNode
    // even when d3-force hasn't rebound yet after a graphData swap.
    const idMap = new Map<string, ForceNode>();
    for (const fn of fNodes) idMap.set(fn.id, fn);
    currentNodeById = idMap;
    return { nodes: fNodes, links: [...realLinks, ...bridgeLinks] };
  }, [nodes, edges, bridgeEdges, positionFor]);
  // seedId is exposed for the centering effect below; reference here so
  // TS doesn't strip the destructuring.
  void seedId;

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

    // Center viewport. Single-anchor mode: center on the seed (which
    // sits at (0,0)). Intersection mode: center on (0,0) which is
    // between the L/R anchors so both are visible.
    if (typeof fg.centerAt === "function") {
      if (intersectionEntityIds) {
        fg.centerAt(0, 0, 0);
      } else {
        const seedNode = graphData.nodes.find((n) => n.is_seed);
        if (seedNode) fg.centerAt(seedNode.x ?? 0, seedNode.y ?? 0, 0);
      }
      if (typeof fg.zoom === "function") fg.zoom(1.0, 0);
    }
  }, [graphData, intersectionEntityIds]);

  const handleClick = useCallback(
    (node: ForceNode, event: MouseEvent) => {
      const original = nodes.find((n) => n.id === node.id);
      if (original) onNodeClick(original, event);
    },
    [nodes, onNodeClick],
  );

  const drawNode = useCallback(
    (node: ForceNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
      // LOD: hide nodes that didn't make the claim-collision visibility set.
      // Partial opacity = node is just past the collision boundary, fading in.
      const opacity = visibilityOpacity(node.id);
      if (opacity <= 0) return;
      ctx.save();
      ctx.globalAlpha = opacity;
      try {
        drawNodeInner(node, ctx, globalScale);
      } finally {
        ctx.restore();
      }
    }, []);

  const drawNodeInner = useCallback(
    (node: ForceNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const isState = STATE_TYPES.has(node.node_type);
      const isSeed = node.is_seed;
      const x = node.x ?? 0;
      const y = node.y ?? 0;

      if (isState) {
        // States: small chips that scale modestly with zoom but never
        // shrink past readability or balloon on close zoom. Wrap the
        // label to up to 2 lines instead of truncating with "…".
        const fontPx = Math.max(STATE_MIN_FONT_PX, Math.min(STATE_MAX_FONT_PX, 8 * globalScale));
        const fontSize = fontPx / globalScale;
        const w = clampScreen(STATE_WIDTH, globalScale, 56, 140);
        const lines = wrapLabel(node.label, w - 6, fontSize * 0.55);
        const lineH = fontSize * 1.15;
        const baseH = lines.length > 1 ? STATE_HEIGHT * 1.7 : STATE_HEIGHT;
        const h = clampScreen(baseH, globalScale, 14, 44);
        roundRect(ctx, x - w / 2, y - h / 2, w, h, Math.min(h / 2, 8));
        ctx.fillStyle = "#f3f4f6";
        ctx.fill();
        ctx.lineWidth = 1 / globalScale;
        ctx.strokeStyle = "#d1d5db";
        ctx.stroke();

        ctx.font = `${fontSize}px Inter, system-ui, sans-serif`;
        ctx.fillStyle = "#6b7280";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        const startY = y - ((lines.length - 1) * lineH) / 2;
        for (let i = 0; i < lines.length; i++) {
          ctx.fillText(lines[i], x, startY + i * lineH);
        }
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

      // Seed gets a fixed on-screen size that doesn't scale with zoom —
      // the focal node should be a stable visual anchor as you zoom.
      // Other entities use the importance/label-aware sizing. SEED_SCREEN_W
      // and SEED_SCREEN_H are hoisted to module scope so the screen-pixel
      // claim helper (`nodeRenderedScreenHalf`) can mirror them.
      const SEED_FONT_PX = 13;
      const naturalW = entityWidth(node);
      const w = isSeed
        ? SEED_SCREEN_W / globalScale
        : clampScreen(naturalW, globalScale, PERSON_MIN_SCREEN_W, PERSON_MAX_SCREEN_W);
      const fontPx = isSeed
        ? SEED_FONT_PX
        : Math.max(PERSON_MIN_FONT_PX, Math.min(PERSON_MAX_FONT_PX, 12 * globalScale));
      const fontSize = fontPx / globalScale;
      const lines = wrapLabel(node.label, w - 12, fontSize * 0.55);
      const naturalH = lines.length > 1 ? ENTITY_HEIGHT * 1.5 : ENTITY_HEIGHT;
      const h = isSeed
        ? SEED_SCREEN_H / globalScale
        : clampScreen(
            naturalH,
            globalScale,
            lines.length > 1 ? PERSON_MIN_SCREEN_H * 1.4 : PERSON_MIN_SCREEN_H,
            lines.length > 1 ? PERSON_MAX_SCREEN_H * 1.4 : PERSON_MAX_SCREEN_H,
          );
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

      // Corner radius in graph coords. For the seed (fixed-screen-size)
      // we want a fixed pixel radius too; for other entities the 8-graph-unit
      // value scales with zoom which is fine.
      const cornerR = isSeed ? 8 / globalScale : 8;
      roundRect(ctx, x - w / 2, y - h / 2, w, h, cornerR);
      ctx.fillStyle = fill;
      ctx.fill();
      ctx.lineWidth = (isSeed ? 2 : 1) / globalScale;
      ctx.strokeStyle = stroke;
      ctx.stroke();

      ctx.font = `500 ${fontSize}px Inter, system-ui, sans-serif`;
      ctx.fillStyle = textColor;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      const lineH = fontSize * 1.15;
      const startY = y - ((lines.length - 1) * lineH) / 2;
      for (let i = 0; i < lines.length; i++) {
        ctx.fillText(lines[i], x, startY + i * lineH);
      }
    },
    [],
  );

  const drawLink = useCallback(
    (link: ForceLink, ctx: CanvasRenderingContext2D, globalScale: number) => {
      // showLines toggle. Off = pure neighborhood layout, no edges.
      if (!currentShowLines) return;

      // Resolve endpoints via our id→ForceNode mirror — d3-force may not
      // have replaced the string id with a node ref yet (graphData rebuilds
      // on every scroll tick). Without this lookup, every link fails the
      // x/y undefined check below and no lines render at all.
      const src = resolveEndpoint(link.source);
      const tgt = resolveEndpoint(link.target);
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

      // Per-edge LOD (replaces the old isBridgeMode toggle):
      //   - Real edges (is_bridge=false): always draw if both endpoints
      //     have opacity > 0. The endpoint check below is the gate.
      //   - Bridge edges (is_bridge=true): draw ONLY when the via-state
      //     they're substituting for is NOT visible — otherwise the real
      //     edges through that visible state already show the connection
      //     and the bridge would be a redundant duplicate line.
      //
      // The previous global `isBridgeMode()` flag hid every real edge
      // touching a state whenever <25% of nodes were rendered, even when
      // the state itself was clearly on screen. That left visible-but-
      // disconnected entities at deep zoom, which is what the user kept
      // seeing.
      const opSrc = visibilityOpacity(src.id);
      const opTgt = visibilityOpacity(tgt.id);
      const edgeOp = Math.min(opSrc, opTgt);
      if (edgeOp <= 0) return;

      if (link.is_bridge) {
        const viaId = link.via_node_id;
        if (viaId && visibilityOpacity(viaId) > 0) return;
      }
      ctx.save();
      ctx.globalAlpha = edgeOp;

      // Viewport-cull: if either endpoint is offscreen (with a small margin)
      // skip drawing — without this, panning shows edges flying off-screen.
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

      drawStraightLink(ctx, src, tgt, globalScale);

      // Bridge label: render the via-state's name along the line so the
      // user sees "Marriage" / "Possession" / "Comparative Belief" between
      // Jukka and the entity, rather than a state chip with two edges.
      // Only for bridges (real edges already render the relationship via
      // the State node when state visibility is on).
      if (link.is_bridge && link.bridge_label) {
        const sx = src.x ?? 0;
        const sy = src.y ?? 0;
        const tx = tgt.x ?? 0;
        const ty = tgt.y ?? 0;
        const mx = (sx + tx) / 2;
        const my = (sy + ty) / 2;
        let angle = Math.atan2(ty - sy, tx - sx);
        // Keep text upright — flip 180° if the line angle would render
        // the label upside-down.
        if (angle > Math.PI / 2) angle -= Math.PI;
        if (angle < -Math.PI / 2) angle += Math.PI;
        const fontPx = Math.max(9, Math.min(13, 11 * globalScale));
        const fontSize = fontPx / globalScale;
        ctx.save();
        ctx.translate(mx, my);
        ctx.rotate(angle);
        ctx.font = `${fontSize}px Inter, system-ui, sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        const text = link.bridge_label;
        const metrics = ctx.measureText(text);
        const padX = 4 / globalScale;
        const padY = 2 / globalScale;
        // White-ish background for legibility against any underlying line.
        ctx.fillStyle = "rgba(255, 255, 255, 0.92)";
        ctx.fillRect(
          -metrics.width / 2 - padX,
          -fontSize / 2 - padY,
          metrics.width + 2 * padX,
          fontSize + 2 * padY,
        );
        ctx.fillStyle = "#6b7280";
        ctx.fillText(text, 0, 0);
        ctx.restore();
      }

      ctx.restore();
    },
    [],
  );

  const nodePointerArea = useCallback(
    (node: ForceNode, color: string, ctx: CanvasRenderingContext2D) => {
      // Visibility-gate the hit region. Without this, an "invisible" node
      // (opacity 0 in drawNode) still has a clickable region, and
      // ForceGraph2D's default hover/click handling triggers on it —
      // producing ghost tooltips and clicks at empty space.
      if (visibilityOpacity(node.id) <= 0) return;

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
        const w = entityWidth(node);
        roundRect(ctx, x - w / 2, y - ENTITY_HEIGHT / 2, w, ENTITY_HEIGHT, 8);
      }
      ctx.fillStyle = color;
      ctx.fill();
    },
    [],
  );

  // Reserve-space bubbles removed — the fade-in algorithm subsumes them.

  // Debug HUD: shows visible count and the linear scroll cursor.
  const debugStats = useMemo(() => {
    return {
      currentScroll,
      visibleCount: visibleIds.size,
      totalNodes: nodes.length,
    };
  }, [currentScroll, nodes, visibleIds]);

  return (
    <>
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
        enableZoomInteraction={false}
        enablePanInteraction
      />
      <div
        style={{
          position: "fixed",
          bottom: 8,
          right: 8,
          background: "rgba(255, 255, 255, 0.9)",
          border: "1px solid #d1d5db",
          borderRadius: 6,
          padding: "6px 10px",
          font: "11px ui-monospace, SFMono-Regular, Menlo, monospace",
          color: "#374151",
          pointerEvents: "none",
          lineHeight: 1.45,
          zIndex: 100,
        }}
      >
        <div>visible: {debugStats.visibleCount} / {debugStats.totalNodes}</div>
        <div>scroll: {debugStats.currentScroll.toFixed(0)}</div>
      </div>
      <button
        type="button"
        onClick={() => {
          setCurrentScroll(0);
          // Re-center the camera on the seed in case the user panned.
          const fg = fgRef.current;
          const seedNode = graphData.nodes.find((n) => n.is_seed);
          if (fg && seedNode && typeof fg.centerAt === "function") {
            fg.centerAt(seedNode.x ?? 0, seedNode.y ?? 0, 250);
          }
        }}
        style={{
          position: "fixed",
          top: 8,
          right: 8,
          marginTop: 122, // sit just below the HUD
          padding: "6px 12px",
          background: "#ffffff",
          border: "1px solid #d1d5db",
          borderRadius: 6,
          font: "12px ui-sans-serif, system-ui, sans-serif",
          color: "#111827",
          cursor: "pointer",
          zIndex: 100,
          boxShadow: "0 1px 2px rgba(0,0,0,0.05)",
        }}
        title="Reset depth cursor (z=0) and re-center on the seed"
      >
        Reset
      </button>
      <button
        type="button"
        onClick={() => setShowLines((v) => !v)}
        style={{
          position: "fixed",
          top: 8,
          right: 8,
          marginTop: 156, // sit just below the Reset button
          padding: "6px 12px",
          background: showLines ? "#2563eb" : "#ffffff",
          color: showLines ? "#ffffff" : "#111827",
          border: "1px solid #d1d5db",
          borderRadius: 6,
          font: "12px ui-sans-serif, system-ui, sans-serif",
          cursor: "pointer",
          zIndex: 100,
          boxShadow: "0 1px 2px rgba(0,0,0,0.05)",
        }}
        title="Show or hide the connection lines between the anchor and entities."
      >
        {showLines ? "Hide lines" : "Show lines"}
      </button>
      <button
        type="button"
        onClick={() => setShowStates((v) => !v)}
        style={{
          position: "fixed",
          top: 8,
          right: 8,
          marginTop: 190, // sit just below the Lines button
          padding: "6px 12px",
          background: showStates ? "#2563eb" : "#ffffff",
          color: showStates ? "#ffffff" : "#111827",
          border: "1px solid #d1d5db",
          borderRadius: 6,
          font: "12px ui-sans-serif, system-ui, sans-serif",
          cursor: "pointer",
          zIndex: 100,
          boxShadow: "0 1px 2px rgba(0,0,0,0.05)",
        }}
        title="Show or hide State / Event / Goal node chips at midpoints. Capped at top 3 per entity by state importance."
      >
        {showStates ? "Hide states" : "Show states"}
      </button>
      {/* 0-hop / 1-hop toggles — only meaningful in intersection mode */}
      {intersectionEntityIds && (
        <>
          <button
            type="button"
            onClick={() => setShowZeroHop((v) => !v)}
            style={{
              position: "fixed",
              top: 8,
              right: 8,
              marginTop: 232,
              padding: "6px 12px",
              background: showZeroHop ? "#2563eb" : "#ffffff",
              color: showZeroHop ? "#ffffff" : "#111827",
              border: "1px solid #d1d5db",
              borderRadius: 6,
              font: "12px ui-sans-serif, system-ui, sans-serif",
              cursor: "pointer",
              zIndex: 100,
              boxShadow: "0 1px 2px rgba(0,0,0,0.05)",
            }}
            title="0-hop: State/Event nodes connected directly to BOTH anchors (Marriage, Family Time, Parent of Annika, ...)."
          >
            {showZeroHop ? "Hide 0-hop" : "Show 0-hop"}
          </button>
          <button
            type="button"
            onClick={() => setShowOneHop((v) => !v)}
            style={{
              position: "fixed",
              top: 8,
              right: 8,
              marginTop: 266,
              padding: "6px 12px",
              background: showOneHop ? "#2563eb" : "#ffffff",
              color: showOneHop ? "#ffffff" : "#111827",
              border: "1px solid #d1d5db",
              borderRadius: 6,
              font: "12px ui-sans-serif, system-ui, sans-serif",
              cursor: "pointer",
              zIndex: 100,
              boxShadow: "0 1px 2px rgba(0,0,0,0.05)",
            }}
            title="1-hop: entities reachable from BOTH anchors via one mediating state (people / places / things they share)."
          >
            {showOneHop ? "Hide 1-hop" : "Show 1-hop"}
          </button>
        </>
      )}
    </>
  );
}
