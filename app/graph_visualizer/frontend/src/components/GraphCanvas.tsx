import React, { useCallback } from 'react';
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
  nodeTypes: string[];
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
  nodeTypes,
}) => {
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

  if (!graphData) {
    return (
      <div className="flex-1 flex items-center justify-center bg-gray-900">
        <div className="text-center">
          <div className="text-gray-400 text-lg">No graph data available</div>
          <div className="text-gray-500 text-sm mt-2">Adjust the degree threshold or search to load nodes</div>
        </div>
      </div>
    );
  }

  const sharedProps = {
    ref: graphRef,
    graphData: { nodes: graphData.nodes, links: graphData.edges } as any,
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
    cooldownTicks: 120,
    d3AlphaDecay: 0.02,
    d3VelocityDecay: 0.35,
  };

  return (
    <div className="graph-visualization-container">
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
