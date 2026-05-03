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

// Flat edge width. Mapping importance to width made the high-importance
// edges (most of them) overwhelmingly dominant — visually noisy and the
// hub structure hard to read. Edge importance is now strictly a backend
// signal (used by the /me lens for ranking); the 3D viz keeps every
// edge thin and equal so type-based color carries the meaning.
export const getEdgeWidth = (_edge: Edge): number => 1;

// Get edge color. Type-first because that's the most semantically
// meaningful signal — color groups conceptually similar edges (all
// "Has_State" edges read as one family, all "Located_In" edges
// another). Importance and confidence drive width / opacity instead;
// using them for color too made every edge gray once the rater
// populated importance for the whole graph.
export const getEdgeColor = (edge: Edge): string => {
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