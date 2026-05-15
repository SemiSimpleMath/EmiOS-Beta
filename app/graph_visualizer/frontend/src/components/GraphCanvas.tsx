import React, { useCallback, useEffect, useMemo, useRef } from 'react';
import ForceGraph2D from 'react-force-graph-2d';
import ForceGraph3D from 'react-force-graph-3d';
import { GraphData, Node, Edge, ViewMode } from '../types/graph';
import { getNodeColorByClassification } from '../lib/colors';
import { getNodeSize as getNodeSizeUtil, getNodeVisibleDegree, getEdgeWidth as getEdgeWidthUtil, getEdgeColor as getEdgeColorUtil } from '../lib/graphUtils';
import Legend from './Legend';

interface GraphCanvasProps {
  graphData: GraphData | null;
  highlightedNodes: Set<string>;
  highlightedEdges: Set<string>;
  viewMode: ViewMode;
  onNodeClick: (node: Node) => void;
  onEdgeClick: (edge: Edge) => void;
  onNodeRightClick: (node: Node, event: MouseEvent) => void;
  onBackgroundClick: () => void;
  graphRef: React.RefObject<any>;
  onEngineStop: () => void;
  onEngineTick: () => void;
  nodeTypes: string[];
  /** True after the force-directed engine has settled. While false the
   *  canvas is hidden behind a "computing layout..." overlay so the user
   *  never sees the jittery initial jumble. */
  graphStable: boolean;
  /** Number of physics ticks completed so far — surfaced in the overlay so
   *  the user can see that physics is actually running. */
  tickCount: number;
  /** Render-only filter. Nodes with importance below this value are hidden
   *  via ForceGraph's nodeVisibility / linkVisibility callbacks so the
   *  layout is not recomputed when the slider changes. */
  importanceThreshold: number;
}

const GraphCanvas: React.FC<GraphCanvasProps> = ({
  graphData,
  highlightedNodes,
  highlightedEdges,
  viewMode,
  onNodeClick,
  onEdgeClick,
  onNodeRightClick,
  onBackgroundClick,
  graphRef,
  onEngineStop,
  onEngineTick,
  nodeTypes,
  graphStable,
  tickCount,
  importanceThreshold,
}) => {
  // Read the threshold via a ref inside the visibility callbacks so the
  // callback identity is STABLE across renders. Otherwise ForceGraph sees
  // a "new" callback every render and restarts physics — which is why
  // moving the slider (or any re-render) prevented the engine from settling.
  const thresholdRef = useRef(importanceThreshold);
  useEffect(() => { thresholdRef.current = importanceThreshold; }, [importanceThreshold]);

  const isNodeVisible = useCallback((n: Node): boolean => {
    return (n.importance ?? -1) >= thresholdRef.current;
  }, []);

  const isEdgeVisible = useCallback((e: Edge): boolean => {
    // ForceGraph mutates source/target to objects after first render; extract safely.
    const src: any = e.source_node;
    const tgt: any = e.target_node;
    const sImp = typeof src === 'object' && src ? src.importance : undefined;
    const tImp = typeof tgt === 'object' && tgt ? tgt.importance : undefined;
    if (sImp === undefined || tImp === undefined) return true;
    const t = thresholdRef.current;
    return (sImp ?? -1) >= t && (tImp ?? -1) >= t;
  }, []);

  // After the threshold changes, directly toggle Three.js `Object3D.visible`
  // on each node/link mesh. We bypass the lib's `nodeVisibility` callback
  // because in 3D it dims opacity instead of properly hiding meshes (we
  // confirmed: filtered nodes still appeared as ghostly gray dots).
  //
  // The lib exposes the scene via `graphRef.current.scene()` (a THREE.Scene).
  // ForceGraph attaches per-node Object3D meshes with `__data` pointing at
  // the source node — we walk those and set `.visible` based on importance.
  // No physics restart, no re-render storm, just per-mesh boolean flips.
  useEffect(() => {
    const fg: any = graphRef.current;
    if (!fg) return;
    try {
      const scene: any = typeof fg.scene === 'function' ? fg.scene() : null;
      if (!scene || typeof scene.traverse !== 'function') return;
      const t = importanceThreshold;
      scene.traverse((obj: any) => {
        const data = obj?.__data;
        if (!data) return;
        // Node objects carry the node row in __data with an importance field.
        // Edge objects carry the edge row with source_node/target_node refs.
        if ('importance' in data && 'node_type' in data) {
          obj.visible = (data.importance ?? -1) >= t;
        } else if ('source_node' in data && 'target_node' in data) {
          const src: any = data.source_node;
          const tgt: any = data.target_node;
          const sImp = typeof src === 'object' && src ? src.importance : undefined;
          const tImp = typeof tgt === 'object' && tgt ? tgt.importance : undefined;
          obj.visible = (sImp ?? -1) >= t && (tImp ?? -1) >= t;
        }
      });
    } catch { /* best-effort */ }
  }, [importanceThreshold, graphRef]);
  const getNodeColor = useCallback((node: Node): string => {
    if (highlightedNodes.has(node.id)) return '#ff6b6b';
    return getNodeColorByClassification(node);
  }, [highlightedNodes]);

  const getNodeSize = useCallback((node: Node): number => {
    if (!graphData) return 4;
    // Use pre-computed degree if available (hub endpoint), otherwise derive from visible edges
    if (node.degree !== undefined) {
      return Math.max(4, Math.min(20, 4 + node.degree * 0.3));
    }
    return getNodeSizeUtil(node, graphData);
  }, [graphData]);

  const getEdgeWidth = useCallback((edge: Edge): number => {
    if (highlightedEdges.has(edge.id)) return 3;
    return getEdgeWidthUtil(edge);
  }, [highlightedEdges]);

  const getEdgeColor = useCallback((edge: Edge): string => {
    if (highlightedEdges.has(edge.id)) return '#ff6b6b';
    return getEdgeColorUtil(edge);
  }, [highlightedEdges]);

  // Memoize the wrapped graph object so ForceGraph sees the SAME data
  // reference across renders when the underlying nodes/edges arrays haven't
  // actually changed. Otherwise each parent re-render hands ForceGraph a
  // "new" data object and it restarts physics, preventing the engine from
  // ever settling. Defined BEFORE the null-data early return so hook order
  // stays stable.
  const wrappedGraphData = useMemo(
    () => graphData ? { nodes: graphData.nodes, links: graphData.edges } : { nodes: [], links: [] },
    [graphData],
  );

  if (!graphData) {
    return (
      <div
        className="flex-1 flex items-center justify-center"
        style={{ background: '#111827', color: '#9ca3af', fontSize: 14, gap: 12 }}
      >
        <div className="loading-spinner rounded-full h-4 w-4 border-b-2 border-gray-400 animate-spin" />
        <span className="ml-3">Loading graph…</span>
      </div>
    );
  }

  const sharedProps = {
    ref: graphRef,
    graphData: wrappedGraphData as any,
    linkSource: 'source_node',
    linkTarget: 'target_node',
    nodeLabel: (node: Node) => {
      // Prefer the server-side precomputed degree (from /api/graph/hubs),
      // otherwise derive from visible edges — nodes loaded via search or
      // neighborhood expansion don't carry `degree` and would otherwise
      // show as "degree: ?".
      const deg = node.degree ?? (graphData ? getNodeVisibleDegree(node, graphData) : 0);
      return `${node.label} (degree: ${deg})`;
    },
    nodeColor: getNodeColor,
    nodeVal: getNodeSize,
    linkWidth: getEdgeWidth,
    linkColor: getEdgeColor,
    linkLabel: (edge: Edge) => edge.type,
    onNodeClick: onNodeClick,
    onLinkClick: onEdgeClick,
    onNodeRightClick: onNodeRightClick,
    onBackgroundClick: onBackgroundClick,
    onEngineStop: onEngineStop,
    onEngineTick: onEngineTick,
    // warmupTicks runs SYNCHRONOUSLY before first paint and on 6k-node graphs
    // it freezes the JS thread for tens of seconds. Keep it at 0 so each
    // tick is async, the tick counter can advance, and the user sees life.
    warmupTicks: 0,
    cooldownTicks: 400,
    d3AlphaDecay: 0.02,
    d3VelocityDecay: 0.35,
    // Render-only visibility filters — driven by the importance slider.
    // ForceGraph does NOT restart physics when these change, so the slider
    // can be moved freely without recomputing the layout.
    nodeVisibility: isNodeVisible,
    linkVisibility: isEdgeVisible,
  };

  return (
    <div className="graph-visualization-container" style={{ position: 'relative' }}>
      {/* Layout-computing overlay: hide the jittery initial jumble until
          the d3 force engine settles (onEngineStop fires). */}
      {!graphStable && (
        <div
          style={{
            position: 'absolute', top: 0, left: 0, right: 0, bottom: 0,
            zIndex: 10, background: '#111827',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            color: '#9ca3af', fontSize: 14, gap: 12,
            flexDirection: 'column',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <div className="loading-spinner rounded-full h-4 w-4 border-b-2 border-gray-400 animate-spin" />
            Computing layout…
          </div>
          <div style={{ fontSize: 12, color: '#6b7280' }}>
            {tickCount > 0
              ? `${tickCount} physics ticks completed`
              : 'waiting for the d3 force engine to start…'}
          </div>
        </div>
      )}
      <div className="force-graph-container">
        {viewMode === '3d' ? (
          <ForceGraph3D
            {...sharedProps}
            // Orbit gives predictable scroll-wheel-to-zoom behavior across
            // mice and touchpads; trackball (the library default) sometimes
            // maps wheel to rotation speed instead of zoom.
            controlType="orbit"
            nodeThreeObjectExtend={false}
            linkDirectionalArrowLength={0}
            linkOpacity={0.6}
            backgroundColor="#111827"
          />
        ) : (
          <ForceGraph2D
            {...sharedProps}
            nodeCanvasObject={(node: Node, ctx: CanvasRenderingContext2D, globalScale: number) => {
              const label = node.label;
              const fontSize = Math.max(8, 12 / globalScale);
              ctx.font = `${fontSize}px Sans-Serif`;
              const textWidth = ctx.measureText(label).width;
              const bckgDimensions: [number, number] = [textWidth, fontSize].map(n => n + fontSize * 0.2) as [number, number];
              const x = node.x ?? 0;
              const y = node.y ?? 0;

              ctx.fillStyle = 'rgba(17, 24, 39, 0.85)';
              ctx.fillRect(x - bckgDimensions[0] / 2, y - bckgDimensions[1] / 2, bckgDimensions[0], bckgDimensions[1]);

              ctx.textAlign = 'center';
              ctx.textBaseline = 'middle';
              ctx.fillStyle = getNodeColor(node);
              ctx.fillText(label, x, y);
            }}
            // Recompute the hit rect with the current globalScale instead of
            // reading a cached bckgDimensions. The cached value is baked from
            // the last nodeCanvasObject call and can lag behind zoom changes,
            // causing the clickable rect to drift away from the visible label.
            nodePointerAreaPaint={(node: Node, color: string, ctx: CanvasRenderingContext2D, globalScale: number) => {
              const label = node.label;
              const fontSize = Math.max(8, 12 / globalScale);
              ctx.font = `${fontSize}px Sans-Serif`;
              const textWidth = ctx.measureText(label).width;
              const w = textWidth + fontSize * 0.2;
              const h = fontSize + fontSize * 0.2;
              const x = node.x ?? 0;
              const y = node.y ?? 0;
              ctx.fillStyle = color;
              ctx.fillRect(x - w / 2, y - h / 2, w, h);
            }}
            linkDirectionalArrowLength={0}
            enableZoomInteraction={true}
            enablePanInteraction={true}
            enableNodeDrag={true}
            backgroundColor="#111827"
          />
        )}

        <Legend nodeTypes={nodeTypes} />
      </div>
    </div>
  );
};

export default GraphCanvas;
