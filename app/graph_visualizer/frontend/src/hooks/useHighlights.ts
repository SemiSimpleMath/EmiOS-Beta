import { useState, useCallback } from 'react';

export const useHighlights = () => {
  const [highlightedNodes, setHighlightedNodes] = useState<Set<string>>(new Set());
  const [highlightedEdges, setHighlightedEdges] = useState<Set<string>>(new Set());

  const highlightNodes = useCallback((nodeIds: string[]) => {
    setHighlightedNodes(new Set(nodeIds));
  }, []);

  const highlightEdges = useCallback((edgeIds: string[]) => {
    setHighlightedEdges(new Set(edgeIds));
  }, []);

  const addHighlightedNode = useCallback((nodeId: string) => {
    setHighlightedNodes(prev => new Set([...Array.from(prev), nodeId]));
  }, []);

  const addHighlightedEdge = useCallback((edgeId: string) => {
    setHighlightedEdges(prev => new Set([...Array.from(prev), edgeId]));
  }, []);

  const removeHighlightedNode = useCallback((nodeId: string) => {
    setHighlightedNodes(prev => {
      const newSet = new Set(prev);
      newSet.delete(nodeId);
      return newSet;
    });
  }, []);

  const removeHighlightedEdge = useCallback((edgeId: string) => {
    setHighlightedEdges(prev => {
      const newSet = new Set(prev);
      newSet.delete(edgeId);
      return newSet;
    });
  }, []);

  const clearHighlights = useCallback(() => {
    setHighlightedNodes(new Set());
    setHighlightedEdges(new Set());
  }, []);

  const isNodeHighlighted = useCallback((nodeId: string) => {
    return highlightedNodes.has(nodeId);
  }, [highlightedNodes]);

  const isEdgeHighlighted = useCallback((edgeId: string) => {
    return highlightedEdges.has(edgeId);
  }, [highlightedEdges]);

  return {
    highlightedNodes,
    highlightedEdges,
    highlightNodes,
    highlightEdges,
    addHighlightedNode,
    addHighlightedEdge,
    removeHighlightedNode,
    removeHighlightedEdge,
    clearHighlights,
    isNodeHighlighted,
    isEdgeHighlighted
  };
};
