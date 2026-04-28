import { apiClient } from './client';
import { GraphData, NodeDetailsData, Node, Edge } from '../types/graph';

export const fetchGraphData = async (): Promise<GraphData> => {
  const response = await apiClient.get('/graph');
  return response.data;
};

export const fetchHubs = async (minDegree: number): Promise<GraphData> => {
  const response = await apiClient.get(`/graph/hubs?min_degree=${minDegree}`);
  return response.data;
};

export const fetchSubgraph = async (nodeId: string, depth: number = 1): Promise<GraphData> => {
  const d = Math.max(0, Math.min(3, Math.floor(depth)));
  const response = await apiClient.get(
    `/graph/subgraph?node_id=${encodeURIComponent(nodeId)}&depth=${d}`
  );
  return response.data;
};

export const searchGraph = async (filters: {
  searchQuery?: string;
  nodeTypeFilter?: string;
  edgeTypeFilter?: string;
}): Promise<GraphData> => {
  const params = new URLSearchParams();
  if (filters.searchQuery) params.append('q', filters.searchQuery);
  if (filters.nodeTypeFilter) params.append('node_type', filters.nodeTypeFilter);
  if (filters.edgeTypeFilter) params.append('edge_type', filters.edgeTypeFilter);

  const response = await apiClient.get(`/graph/search?${params.toString()}`);
  return response.data;
};

export const fetchTypes = async (): Promise<{
  nodeTypes: string[];
  edgeTypes: string[];
}> => {
  const [nodeTypesResponse, edgeTypesResponse] = await Promise.all([
    apiClient.get('/graph/node-types'),
    apiClient.get('/graph/edge-types')
  ]);

  const nodeTypes = nodeTypesResponse.data.map((nt: any) => nt.node_type);
  // Backend returns {type_name: string} objects
  const edgeTypes = edgeTypesResponse.data.map((et: any) => et.type_name);

  return { nodeTypes, edgeTypes };
};

export const fetchNodeDetails = async (nodeId: string): Promise<NodeDetailsData> => {
  const response = await apiClient.get(`/graph/node/${nodeId}`);
  return response.data;
};

export const deleteNode = async (nodeId: string): Promise<void> => {
  await apiClient.delete(`/graph/node/${nodeId}`);
};

export const deleteEdge = async (edgeId: string): Promise<void> => {
  await apiClient.delete(`/graph/edge/${edgeId}`);
};

export const updateNode = async (nodeId: string, updates: Partial<Node>): Promise<Node> => {
  const response = await apiClient.put(`/graph/node/${nodeId}`, updates);
  return response.data;
};

export const updateEdge = async (edgeId: string, updates: Partial<Edge>): Promise<Edge> => {
  const response = await apiClient.put(`/graph/edge/${edgeId}`, updates);
  return response.data;
};
