import { GraphData, GraphStats } from '../types/graph';

export const calculateStats = (data: GraphData): GraphStats => {
  const nodeTypeCount: { [key: string]: number } = {};
  const edgeTypeCount: { [key: string]: number } = {};
  const nodeDegree: { [key: string]: number } = {};

  // Count node types and classifications
  data.nodes.forEach(node => {
    nodeTypeCount[node.type] = (nodeTypeCount[node.type] || 0) + 1;
    nodeDegree[node.id] = 0;
  });

  // Count edge types and calculate degrees
  data.edges.forEach(edge => {
    edgeTypeCount[edge.type] = (edgeTypeCount[edge.type] || 0) + 1;
    const sourceId = getNodeId(edge.source_node);
    const targetId = getNodeId(edge.target_node);
    nodeDegree[sourceId] = (nodeDegree[sourceId] || 0) + 1;
    nodeDegree[targetId] = (nodeDegree[targetId] || 0) + 1;
  });

  const totalNodes = data.nodes.length;
  const totalEdges = data.edges.length;
  const avgNodeDegree = totalNodes > 0 ? Object.values(nodeDegree).reduce((a, b) => a + b, 0) / totalNodes : 0;
  const density = totalNodes > 1 ? (2 * totalEdges) / (totalNodes * (totalNodes - 1)) : 0;

  return {
    totalNodes,
    totalEdges,
    nodeTypes: nodeTypeCount,
    edgeTypes: edgeTypeCount,
    avgNodeDegree: Math.round(avgNodeDegree * 100) / 100,
    density: Math.round(density * 10000) / 10000
  };
};

// Helper function to get node ID from either string or node object
const getNodeId = (idOrNode: string | any): string => {
  if (typeof idOrNode === 'string') {
    return idOrNode;
  }
  if (idOrNode && typeof idOrNode === 'object' && idOrNode.id) {
    return String(idOrNode.id);
  }
  return String(idOrNode || 'unknown');
};
