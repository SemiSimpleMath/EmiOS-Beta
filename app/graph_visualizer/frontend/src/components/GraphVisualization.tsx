import React, { useRef, useCallback } from 'react';
import ForceGraph2D from 'react-force-graph-2d';

interface Node {
  id: string;
  label: string;
  type: string;
  [key: string]: any;
}

interface Edge {
  id: string;
  source_node: string;
  target_node: string;
  type: string;
  [key: string]: any;
}

interface GraphData {
  nodes: Node[];
  edges: Edge[];
}

interface GraphVisualizationProps {
  graphData: GraphData | null;
  highlightedNodes: Set<string>;
  highlightedEdges: Set<string>;
  onNodeClick: (node: Node) => void;
  onNodeRightClick: (node: Node, event: MouseEvent) => void;
  onEdgeClick: (edge: Edge) => void;
  getNodeColor: (node: Node) => string;
  getNodeSize: (node: Node) => number;
  getEdgeColor: (edge: Edge) => string;
  getEdgeWidth: (edge: Edge) => number;
}

const GraphVisualization: React.FC<GraphVisualizationProps> = ({
  graphData,
  highlightedNodes,
  highlightedEdges,
  onNodeClick,
  onNodeRightClick,
  onEdgeClick,
  getNodeColor,
  getNodeSize,
  getEdgeColor,
  getEdgeWidth,
}) => {
  const graphRef = useRef<any>(null);

  const nodeCanvasObject = useCallback((node: any, ctx: CanvasRenderingContext2D, globalScale: number) => {
    const label = node.semantic_label || node.label;
    const fontSize = Math.max(8, 12 / globalScale);
    ctx.font = `${fontSize}px Sans-Serif`;
    const textWidth = ctx.measureText(label).width;
    const bckgDimensions: [number, number] = [textWidth, fontSize].map(n => n + fontSize * 0.2) as [number, number];

    // Draw background
    ctx.fillStyle = 'rgba(255, 255, 255, 0.9)';
    ctx.fillRect(
      node.x - bckgDimensions[0] / 2,
      node.y - bckgDimensions[1] / 2,
      bckgDimensions[0],
      bckgDimensions[1]
    );

    // Draw text
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = getNodeColor(node);
    ctx.fillText(label, node.x, node.y);

    node.__bckgDimensions = bckgDimensions;
  }, [getNodeColor]);

  const nodePointerAreaPaint = useCallback((node: any, color: string, ctx: CanvasRenderingContext2D) => {
    // Use the background dimensions from nodeCanvasObject if available
    if (node.__bckgDimensions) {
      const [width, height] = node.__bckgDimensions;
      ctx.fillStyle = color;
      ctx.fillRect(
        node.x - width / 2,
        node.y - height / 2,
        width,
        height
      );
    } else {
      // Fallback to circle if background dimensions not available
      const size = getNodeSize(node);
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(node.x, node.y, size, 0, 2 * Math.PI, false);
      ctx.fill();
    }
  }, [getNodeSize]);

  if (!graphData) return null;

  return (
    <div className="flex-1 bg-gray-100 relative graph-visualization-container">
      <ForceGraph2D
        ref={graphRef}
        graphData={{
          nodes: graphData.nodes,
          links: graphData.edges,
        }}
        linkSource="source_node"
        linkTarget="target_node"
        nodeLabel={(node: any) => node.semantic_label || node.label}
        nodeColor={getNodeColor}
        nodeVal={getNodeSize}
        linkLabel="type"
        linkColor={getEdgeColor}
        linkWidth={getEdgeWidth}
        onNodeClick={onNodeClick}
        onNodeRightClick={onNodeRightClick}
        onLinkClick={onEdgeClick}
        cooldownTicks={100}
        enableNodeDrag={true}
        enableZoomInteraction={true}
        enablePanInteraction={true}
        nodeCanvasObject={nodeCanvasObject}
        nodePointerAreaPaint={nodePointerAreaPaint}
      />
      
      {/* Graph Info Overlay */}
      <div className="absolute top-4 right-4 bg-white bg-opacity-90 p-3 rounded-lg shadow-lg">
        <div className="text-sm text-gray-600">
          <div>📊 {graphData.nodes.length} nodes</div>
          <div>🔗 {graphData.edges.length} edges</div>
          <div className="text-xs mt-1">Click nodes/edges for details</div>
        </div>
      </div>
    </div>
  );
};

export default GraphVisualization;
