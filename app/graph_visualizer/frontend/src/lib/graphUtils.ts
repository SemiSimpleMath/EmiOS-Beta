import { GraphData, Node, Edge } from '../types/graph';

// Helper to get node ID from either string or node object
export const getNodeId = (idOrNode: string | any): string => {
  if (typeof idOrNode === 'string') {
    return idOrNode;
  }
  if (idOrNode && typeof idOrNode === 'object' && idOrNode.id) {
    return String(idOrNode.id);
  }
  return String(idOrNode || 'unknown');
};

// Helper to get node label by ID
export const getNodeLabelById = (idOrNode: string | any, graphData?: GraphData | null): string => {
  if (!graphData) return String(idOrNode || 'Unknown');
  
  // If it's already a node object with a label
  if (idOrNode && typeof idOrNode === 'object' && idOrNode.label) {
    return String(idOrNode.label);
  }
  
  // If it's a string ID, look it up
  if (typeof idOrNode === 'string') {
    const node = graphData.nodes.find(n => n.id === idOrNode);
    return node ? String(node.label) : String(idOrNode);
  }
  
  // Fallback - convert to string to avoid React rendering errors
  return String(idOrNode || 'Unknown');
};

// Count how many of the graph's visible edges touch this node.
export const getNodeVisibleDegree = (node: Node, graphData: GraphData): number => {
  return graphData.edges.filter(edge => {
    const sourceId = getNodeId(edge.source_node);
    const targetId = getNodeId(edge.target_node);
    return sourceId === node.id || targetId === node.id;
  }).length;
};

// Get node size based on degree (number of connections)
export const getNodeSize = (node: Node, graphData: GraphData): number => {
  const degree = getNodeVisibleDegree(node, graphData);
  // Base size + degree scaling
  return Math.max(4, Math.min(20, 4 + degree * 0.5));
};

// Get edge width based on importance or confidence
export const getEdgeWidth = (edge: Edge): number => {
  if (edge.importance) {
    return Math.max(1, Math.min(5, edge.importance * 5));
  }
  if (edge.confidence) {
    return Math.max(1, Math.min(5, edge.confidence * 5));
  }
  return 1;
};

// Get edge color based on type or confidence
export const getEdgeColor = (edge: Edge): string => {
  // Color by confidence if available
  if (edge.confidence) {
    if (edge.confidence > 0.8) return '#22c55e'; // Green for high confidence
    if (edge.confidence > 0.6) return '#eab308'; // Yellow for medium confidence
    return '#ef4444'; // Red for low confidence
  }
  
  // Color by importance if available
  if (edge.importance) {
    if (edge.importance > 0.8) return '#3b82f6'; // Blue for high importance
    if (edge.importance > 0.6) return '#8b5cf6'; // Purple for medium importance
    return '#6b7280'; // Gray for low importance
  }
  
  // Default color by type - only the most common edge types
  const typeColors: { [key: string]: string } = {
    'Has_State': '#10b981',
    'About': '#f59e0b',
    'Interested_In': '#8b5cf6',
    'Located_In': '#06b6d4',
    'Works_At': '#ef4444',
    'Friends_With': '#84cc16',
    'Related_To': '#6366f1',
    'Part_Of': '#ec4899',
    'Contains': '#14b8a6',
    'Belongs_To': '#f97316',
    'default': '#6b7280' // Gray for unknown types
  };
  
  return typeColors[edge.type] || typeColors['default'];
};