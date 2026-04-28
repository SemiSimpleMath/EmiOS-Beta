import { GraphData, SearchFilters } from '../types/graph';

export const exportGraphData = (
  graphData: GraphData | null,
  filters: SearchFilters
): void => {
  if (!graphData) return;
  
  const exportData = {
    metadata: {
      exportedAt: new Date().toISOString(),
      totalNodes: graphData.nodes.length,
      totalEdges: graphData.edges.length,
      filters: {
        searchQuery: filters.searchQuery,
        nodeTypeFilter: filters.nodeTypeFilter,
        edgeTypeFilter: filters.edgeTypeFilter
      }
    },
    nodes: graphData.nodes,
    edges: graphData.edges
  };
  
  const dataStr = JSON.stringify(exportData, null, 2);
  const dataBlob = new Blob([dataStr], { type: 'application/json' });
  const url = URL.createObjectURL(dataBlob);
  
  const link = document.createElement('a');
  link.href = url;
  link.download = `knowledge-graph-export-${new Date().toISOString().split('T')[0]}.json`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
};
