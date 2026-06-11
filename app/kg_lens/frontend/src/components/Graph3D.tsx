import { useCallback, useEffect, useMemo, useRef } from "react";
import ForceGraph3D from "react-force-graph-3d";
import { FocusGraphEdge, FocusGraphNode } from "../types";

// τ moves in half-point steps per ctrl+wheel tick (importance is 0..10).
const TAU_STEP = 0.5;

// Color palette ported from the 2D GraphCanvas.tsx:
//   focus/seed accent  #2563eb (seed fill)
//   Entity             #111827 (entity ink/text tone)
//   State / Goal       #6b7280 (state-chip text tone)
//   Event              #9ca3af (Event stroke tone)
//   anything else      #d1d5db (neutral stroke tone)
const FOCUS_COLOR = "#2563eb";
const NODE_TYPE_COLORS: Record<string, string> = {
  Entity: "#111827",
  State: "#6b7280",
  Goal: "#6b7280",
  Event: "#9ca3af",
};
const DEFAULT_NODE_COLOR = "#d1d5db";
// Link tone ports drawStraightLink's rgba(107, 114, 128, 0.4).
const LINK_COLOR = "#6b7280";
const LINK_OPACITY = 0.4;

// Pure-render node: positions come frozen from the server; fx/fy/fz pin
// every node so the (immediately stopped) engine never moves anything.
interface GNode {
  id: string;
  label: string;
  node_type: string;
  importance: number;
  is_focus: boolean;
  x: number;
  y: number;
  z: number;
  fx: number;
  fy: number;
  fz: number;
}

interface GLink {
  source: string | GNode;
  target: string | GNode;
  relationship_type: string;
}

interface Props {
  nodes: FocusGraphNode[];
  edges: FocusGraphEdge[];
  // Importance threshold: a node renders iff importance >= tau, OR it's
  // the focus node, OR it's the currently selected node.
  tau: number;
  selectedNodeId: string | null;
  // Ctrl+wheel ticks report ±TAU_STEP; App owns the clamped τ state.
  onTauDelta: (delta: number) => void;
  onNodeClick: (node: FocusGraphNode) => void;
  onBackgroundClick?: () => void;
}

export function Graph3D({
  nodes,
  edges,
  tau,
  selectedNodeId,
  onTauDelta,
  onNodeClick,
  onBackgroundClick,
}: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const fgRef = useRef<any>(null);

  // graphData is CONSTANT per focus — τ changes only flip the
  // nodeVisibility / linkVisibility accessors below, so filtering never
  // rebuilds the scene graph.
  const graphData = useMemo(() => {
    const gNodes: GNode[] = nodes.map((n) => ({
      id: n.id,
      label: n.label,
      node_type: n.node_type,
      importance: n.importance,
      is_focus: n.is_focus,
      x: n.x,
      y: n.y,
      z: n.z,
      fx: n.x,
      fy: n.y,
      fz: n.z,
    }));
    const gLinks: GLink[] = edges.map((e) => ({
      source: e.source_id,
      target: e.target_id,
      relationship_type: e.relationship_type,
    }));
    return { nodes: gNodes, links: gLinks };
  }, [nodes, edges]);

  const nodeById = useMemo(() => {
    const m = new Map<string, GNode>();
    for (const n of graphData.nodes) m.set(n.id, n);
    return m;
  }, [graphData]);

  const originalById = useMemo(() => {
    const m = new Map<string, FocusGraphNode>();
    for (const n of nodes) m.set(n.id, n);
    return m;
  }, [nodes]);

  const isVisible = useCallback(
    (n: GNode): boolean =>
      n.is_focus || n.id === selectedNodeId || n.importance >= tau,
    [tau, selectedNodeId],
  );

  // Resolve a link endpoint whether the lib has already swapped the
  // string id for a node ref or not.
  const resolveEndpoint = useCallback(
    (ref: string | GNode): GNode | undefined =>
      typeof ref === "string" ? nodeById.get(ref) : ref,
    [nodeById],
  );

  const isLinkVisible = useCallback(
    (l: GLink): boolean => {
      const src = resolveEndpoint(l.source);
      const tgt = resolveEndpoint(l.target);
      return !!src && !!tgt && isVisible(src) && isVisible(tgt);
    },
    [resolveEndpoint, isVisible],
  );

  // Mirror the visibility check + data into refs so the one-shot camera
  // fit below frames the τ-visible set without re-binding onEngineStop.
  const isVisibleRef = useRef(isVisible);
  isVisibleRef.current = isVisible;
  const graphDataRef = useRef(graphData);
  graphDataRef.current = graphData;

  // Camera refit once per data load (first load + every view change —
  // focus and explicit-set payloads both arrive as fresh node/edge
  // arrays, so graphData changes either way). With cooldownTicks=0 the
  // engine stops on its first frame, so onEngineStop fires right after
  // each graphData ingest.
  //
  // Manual fit instead of zoomToFit: with a high starting τ only a
  // handful of nodes are visible, and zoomToFit would dive in until the
  // focus node fills half the screen. Frame the visible set's bounding
  // sphere but never closer than MIN_CAMERA_DISTANCE.
  const needFitRef = useRef(true);
  useEffect(() => {
    needFitRef.current = true;
  }, [graphData]);

  const handleEngineStop = useCallback(() => {
    if (!needFitRef.current) return;
    needFitRef.current = false;
    const visible = graphDataRef.current.nodes.filter((n) => isVisibleRef.current(n));
    if (!visible.length) return;
    let cx = 0, cy = 0, cz = 0;
    for (const n of visible) { cx += n.fx; cy += n.fy; cz += n.fz; }
    cx /= visible.length; cy /= visible.length; cz /= visible.length;
    // 85th-percentile radius: frame the bulk of the visible set and let a
    // straggler or two sit off-screen rather than zooming out for them.
    const dists = visible
      .map((n) => Math.hypot(n.fx - cx, n.fy - cy, n.fz - cz))
      .sort((a, b) => a - b);
    const radius = dists[Math.min(dists.length - 1, Math.floor(dists.length * 0.85))];
    // ~1.3×R fits a bounding sphere at the default ~75° fov; 1.5 adds a
    // little margin. Clamp so tiny sets don't fill the screen and huge
    // ones don't shrink to dust.
    const MIN_CAMERA_DISTANCE = 900;
    const MAX_CAMERA_DISTANCE = 2800;
    const dist = Math.min(MAX_CAMERA_DISTANCE, Math.max(MIN_CAMERA_DISTANCE, radius * 1.5));
    fgRef.current?.cameraPosition(
      { x: cx, y: cy, z: cz + dist },
      { x: cx, y: cy, z: cz },
      600,
    );
  }, []);

  // Ctrl+wheel = importance filter; plain wheel falls through to the
  // lib's orbit-controls camera zoom. Capture phase is required to beat
  // both OrbitControls' own wheel handler and the browser's pinch-zoom.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const handleWheel = (e: WheelEvent) => {
      if (!e.ctrlKey) return;
      e.preventDefault();
      e.stopPropagation();
      // Wheel up = raise τ = fewer nodes; wheel down = reveal more.
      onTauDelta(e.deltaY < 0 ? TAU_STEP : -TAU_STEP);
    };
    el.addEventListener("wheel", handleWheel, { passive: false, capture: true });
    return () => {
      el.removeEventListener("wheel", handleWheel, {
        capture: true,
      } as EventListenerOptions);
    };
  }, [onTauDelta]);

  const handleClick = useCallback(
    (node: GNode) => {
      const original = originalById.get(node.id);
      if (original) onNodeClick(original);
    },
    [originalById, onNodeClick],
  );

  return (
    <div ref={containerRef} className="absolute inset-0">
      <ForceGraph3D
        ref={fgRef}
        graphData={graphData}
        nodeId="id"
        linkSource="source"
        linkTarget="target"
        backgroundColor="#ffffff"
        controlType="orbit"
        cooldownTicks={0}
        warmupTicks={0}
        enableNodeDrag={false}
        nodeVisibility={(n) => isVisible(n as GNode)}
        linkVisibility={(l) => isLinkVisible(l as GLink)}
        nodeVal={(n) => {
          const g = n as GNode;
          // Size from importance; the focus node is markedly bigger.
          return g.is_focus ? (1 + g.importance) * 3 : 1 + g.importance;
        }}
        nodeColor={(n) => {
          const g = n as GNode;
          if (g.is_focus) return FOCUS_COLOR;
          return NODE_TYPE_COLORS[g.node_type] ?? DEFAULT_NODE_COLOR;
        }}
        nodeLabel={(n) => {
          const g = n as GNode;
          return `${g.label} (${g.node_type})`;
        }}
        nodeOpacity={0.9}
        linkColor={() => LINK_COLOR}
        linkOpacity={LINK_OPACITY}
        onNodeClick={(n) => handleClick(n as GNode)}
        onBackgroundClick={onBackgroundClick}
        onEngineStop={handleEngineStop}
      />
    </div>
  );
}
