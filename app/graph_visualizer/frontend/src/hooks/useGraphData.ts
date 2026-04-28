import { useState, useCallback, useRef } from 'react';
import { GraphData, GraphStats, Node, Edge } from '../types/graph';
import { fetchHubs, fetchSubgraph } from '../api/graph';
import { calculateStats } from '../lib/graphStats';

const MAX_NODES = 500;

const mergeGraphData = (
  existing: GraphData,
  incoming: GraphData,
): GraphData => {
  const nodeMap = new Map<string, Node>(existing.nodes.map(n => [n.id, n]));
  for (const n of incoming.nodes) {
    nodeMap.set(n.id, n);
  }

  const edgeMap = new Map<string, Edge>(existing.edges.map(e => [e.id, e]));
  for (const e of incoming.edges) {
    edgeMap.set(e.id, e);
  }

  const nodes = Array.from(nodeMap.values());
  const nodeIds = new Set(nodes.map(n => n.id));

  // Only keep edges where both endpoints are visible
  const edges = Array.from(edgeMap.values()).filter(
    e => nodeIds.has(getEndpointId(e.source_node)) && nodeIds.has(getEndpointId(e.target_node))
  );

  return {
    nodes,
    edges,
    timestamp: incoming.timestamp,
  };
};

// ForceGraph mutates source_node/target_node to objects after first render.
// Always extract the string id safely.
const getEndpointId = (v: string | any): string => {
  if (typeof v === 'string') return v;
  if (v && typeof v === 'object' && v.id) return String(v.id);
  return String(v);
};

export const useGraphData = () => {
  const [graphData, setGraphData] = useState<GraphData | null>(null);
  const [graphStats, setGraphStats] = useState<GraphStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [degreeThreshold, setDegreeThreshold] = useState(3);
  // Track which node IDs have already been expanded so we don't re-fetch
  const expandedRef = useRef<Set<string>>(new Set());

  const _apply = useCallback((data: GraphData) => {
    setGraphData(data);
    setGraphStats(calculateStats(data));
    setError(null);
  }, []);

  const loadHubs = useCallback(async (minDegree?: number) => {
    const threshold = minDegree ?? degreeThreshold;
    setLoading(true);
    try {
      expandedRef.current = new Set();
      const data = await fetchHubs(threshold);
      _apply(data);
      if (minDegree !== undefined) {
        setDegreeThreshold(minDegree);
      }
    } catch (err) {
      setError('Failed to load hub nodes');
      throw err;
    } finally {
      setLoading(false);
    }
  }, [degreeThreshold, _apply]);

  const expandNode = useCallback(async (nodeId: string, depth: number = 1) => {
    // Re-fetch at a different depth even if the node has been "expanded"
    // before — the user may want a wider hop count than last time.
    const expandKey = `${nodeId}@${depth}`;
    if (expandedRef.current.has(expandKey)) return;
    expandedRef.current.add(expandKey);

    setLoading(true);
    try {
      const subgraph = await fetchSubgraph(nodeId, depth);

      setGraphData(prev => {
        if (!prev) return subgraph;

        const merged = mergeGraphData(prev, subgraph);

        // Enforce cap — if we'd go over MAX_NODES, trim lowest-degree newcomers
        if (merged.nodes.length > MAX_NODES) {
          const existingIds = new Set(prev.nodes.map(n => n.id));
          const newNodes = merged.nodes.filter(n => !existingIds.has(n.id));
          // Sort new nodes by degree desc (degree may be undefined for subgraph nodes — treat as 0)
          newNodes.sort((a, b) => (b.degree ?? 0) - (a.degree ?? 0));
          const budget = MAX_NODES - prev.nodes.length;
          const kept = new Set([
            ...prev.nodes.map(n => n.id),
            ...newNodes.slice(0, Math.max(0, budget)).map(n => n.id),
          ]);
          const trimmedNodes = merged.nodes.filter(n => kept.has(n.id));
          const trimmedEdges = merged.edges.filter(
            e => kept.has(getEndpointId(e.source_node)) && kept.has(getEndpointId(e.target_node))
          );
          return { nodes: trimmedNodes, edges: trimmedEdges, timestamp: merged.timestamp };
        }

        return merged;
      });

      setGraphStats(prev => {
        // Recalculate after state updates propagate — this is a best-effort update
        return prev;
      });
    } catch (err) {
      // Remove from expanded set so retry is possible
      expandedRef.current.delete(nodeId);
      setError(`Failed to expand node ${nodeId}`);
      throw err;
    } finally {
      setLoading(false);
    }
  }, []);

  const resetToHubs = useCallback(async (minDegree?: number) => {
    await loadHubs(minDegree);
  }, [loadHubs]);

  const updateGraphData = useCallback((newData: GraphData) => {
    setGraphData(newData);
    setGraphStats(calculateStats(newData));
  }, []);

  return {
    graphData,
    graphStats,
    loading,
    error,
    degreeThreshold,
    loadHubs,
    expandNode,
    resetToHubs,
    updateGraphData,
  };
};
