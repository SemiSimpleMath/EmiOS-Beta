import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import './App.css';

import HeaderBar from './components/HeaderBar';
import SearchFilters from './components/SearchFilters';
import { SearchModal } from './components/SearchModal';
import StatsPanel from './components/StatsPanel';
import GraphCanvas from './components/GraphCanvas';
import Sidebar from './components/Sidebar/Sidebar';
import ContextMenu from './components/ContextMenu';
import KeyboardHelpModal from './components/KeyboardHelpModal';
import LoadError from './components/LoadError';
import LoadingScreen from './components/LoadingScreen';

import { useGraphData } from './hooks/useGraphData';
import { useFilters } from './hooks/useFilters';
import { useHighlights } from './hooks/useHighlights';
import { useContextMenu } from './hooks/useContextMenu';
import { useGraphPhysics } from './hooks/useGraphPhysics';
import { useKeyboardShortcuts } from './hooks/useKeyboardShortcuts';

import { fetchTypes, fetchNodeDetails, deleteNode, deleteEdge, updateNode, updateEdge } from './api/graph';
import { exportGraphData } from './lib/exportJson';
import { getNodeLabelById, getNodeId } from './lib/graphUtils';
import { passesTaxonomyFilters } from './lib/taxonomyFilter';
import { calculateStats } from './lib/graphStats';

import { Node, Edge, NodeDetailsData, ViewMode } from './types/graph';

const App: React.FC = () => {
  const [selectedNode, setSelectedNode] = useState<Node | null>(null);
  const [selectedEdge, setSelectedEdge] = useState<Edge | null>(null);
  const [nodeDetails, setNodeDetails] = useState<NodeDetailsData | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [showHelpModal, setShowHelpModal] = useState(false);
  const [showSearchModal, setShowSearchModal] = useState(false);
  const [showStats, setShowStats] = useState(true);
  const [nodeTypes, setNodeTypes] = useState<string[]>([]);
  const [edgeTypes, setEdgeTypes] = useState<string[]>([]);
  const [viewMode, setViewMode] = useState<ViewMode>('3d');
  // Slider local state — min importance threshold. Default 10 (top tier only).
  // User drops the slider to reveal lower-importance nodes.
  const [sliderValue, setSliderValue] = useState(10);
  // Expansion hops — how far to walk out from a clicked/searched node.
  // 0 = node only, 1 = direct neighbors, 2 = neighbors-of-neighbors.
  const [expandHops, setExpandHops] = useState(1);
  // Debounce timer for auto-expand-on-search.
  const searchExpandTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const {
    graphData,
    loading,
    error,
    loadHubs,
    expandNode,
    resetToHubs,
    updateGraphData,
  } = useGraphData();

  const {
    searchQuery,
    nodeTypeFilter,
    edgeTypeFilter,
    taxonomyKeyword,
    taxonomyPath,
    updateSearchQuery,
    updateNodeTypeFilter,
    updateEdgeTypeFilter,
    updateFilters,
    clearFilters,
    getFilters,
  } = useFilters();

  const { highlightedNodes, highlightedEdges, highlightNodes, highlightEdges, clearHighlights } = useHighlights();
  const { contextMenu, openContextMenu, closeContextMenu } = useContextMenu();
  const { physicsConfigured, graphStable, tickCount, graphRef, configurePhysics, onEngineStop, onEngineTick, resetPhysics } = useGraphPhysics();

  // Initial load
  useEffect(() => {
    const init = async () => {
      try {
        const { nodeTypes: types, edgeTypes: eTypes } = await fetchTypes();
        setNodeTypes(types);
        setEdgeTypes(eTypes);
      } catch (err) {
        console.error('Error fetching types:', err);
      }
    };
    init();
    loadHubs();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (graphData && !physicsConfigured) {
      configurePhysics();
    }
  }, [graphData, physicsConfigured, configurePhysics]);

  // Slider only updates the threshold. Visibility filtering happens in the
  // render layer (nodeVisibility/linkVisibility in GraphCanvas) so the
  // physics layout doesn't restart — the graph is the same simulated graph,
  // just with some nodes hidden.
  const handleImportanceChange = useCallback((value: number) => {
    setSliderValue(value);
  }, []);

  const handleResetToHubs = useCallback(() => {
    resetToHubs(sliderValue);
  }, [resetToHubs, sliderValue]);

  const handleNodeClick = useCallback(async (node: Node) => {
    setSelectedNode(node);
    setSelectedEdge(null);
    setSidebarOpen(true);
    clearHighlights();

    // Expand neighborhood (accumulate mode) at the currently-selected hop depth
    try {
      await expandNode(node.id, expandHops);
    } catch (err) {
      console.error('Error expanding node:', err);
    }

    // Fetch sidebar details
    try {
      const details = await fetchNodeDetails(node.id);
      setNodeDetails(details);
    } catch (err) {
      console.error('Error fetching node details:', err);
      setNodeDetails(null);
    }
  }, [clearHighlights, expandNode, expandHops]);

  const handleEdgeClick = useCallback((edge: Edge) => {
    setSelectedEdge(edge);
    setSelectedNode(null);
    setNodeDetails(null);
    setSidebarOpen(true);

    if (graphData) {
      const sourceId = getNodeId(edge.source_node);
      const targetId = getNodeId(edge.target_node);
      highlightNodes([sourceId, targetId]);
      highlightEdges([edge.id]);
    }
  }, [graphData, highlightNodes, highlightEdges]);

  const handleNodeRightClick = useCallback((node: Node, event: MouseEvent) => {
    event.preventDefault();
    openContextMenu(event.clientX, event.clientY, { id: node.id, label: node.label });
  }, [openContextMenu]);

  const handleBackgroundClick = useCallback(() => {
    setSelectedNode(null);
    setSelectedEdge(null);
    setNodeDetails(null);
    setSidebarOpen(false);
    clearHighlights();
  }, [clearHighlights]);

  const handleExport = useCallback(() => {
    if (graphData) exportGraphData(graphData, getFilters());
  }, [graphData, getFilters]);

  const handleNodeSave = useCallback(async (updatedNode: Partial<Node>) => {
    if (!selectedNode) return;
    try {
      await updateNode(selectedNode.id, updatedNode);
      if (graphData) {
        const updatedNodes = graphData.nodes.map(n =>
          n.id === selectedNode.id ? { ...n, ...updatedNode } : n
        );
        updateGraphData({ ...graphData, nodes: updatedNodes });
      }
      setSelectedNode({ ...selectedNode, ...updatedNode });
    } catch (err) {
      console.error('Error updating node:', err);
    }
  }, [selectedNode, graphData, updateGraphData]);

  const handleNodeDelete = useCallback(async (nodeId: string) => {
    try {
      await deleteNode(nodeId);
      if (graphData) {
        const updatedNodes = graphData.nodes.filter(n => n.id !== nodeId);
        const updatedEdges = graphData.edges.filter(e => {
          const src = getNodeId(e.source_node);
          const tgt = getNodeId(e.target_node);
          return src !== nodeId && tgt !== nodeId;
        });
        updateGraphData({ ...graphData, nodes: updatedNodes, edges: updatedEdges });
      }
      setSidebarOpen(false);
      setSelectedNode(null);
      setSelectedEdge(null);
      setNodeDetails(null);
      clearHighlights();
    } catch (err) {
      console.error('Error deleting node:', err);
    }
  }, [graphData, updateGraphData, clearHighlights]);

  const handleEdgeSave = useCallback(async (updatedEdge: Partial<Edge>) => {
    if (!selectedEdge) return;
    try {
      await updateEdge(selectedEdge.id, updatedEdge);
      if (graphData) {
        const updatedEdges = graphData.edges.map(e =>
          e.id === selectedEdge.id ? { ...e, ...updatedEdge } : e
        );
        updateGraphData({ ...graphData, edges: updatedEdges });
      }
      setSelectedEdge({ ...selectedEdge, ...updatedEdge });
    } catch (err) {
      console.error('Error updating edge:', err);
    }
  }, [selectedEdge, graphData, updateGraphData]);

  const handleEdgeDelete = useCallback(async (edgeId: string) => {
    try {
      await deleteEdge(edgeId);
      if (graphData) {
        const updatedEdges = graphData.edges.filter(e => e.id !== edgeId);
        updateGraphData({ ...graphData, edges: updatedEdges });
      }
      setSidebarOpen(false);
      setSelectedNode(null);
      setSelectedEdge(null);
      setNodeDetails(null);
      clearHighlights();
    } catch (err) {
      console.error('Error deleting edge:', err);
    }
  }, [graphData, updateGraphData, clearHighlights]);

  useKeyboardShortcuts({
    onRefresh: () => resetToHubs(sliderValue),
    onExport: handleExport,
    onClearHighlights: clearHighlights,
    onToggleAutoRefresh: () => {},
    onSearch: () => setShowSearchModal(true),
    onCloseSidebar: () => setSidebarOpen(false),
    onImportanceUp: () => setSliderValue(v => Math.min(10, +(v + 0.1).toFixed(1))),
    onImportanceDown: () => setSliderValue(v => Math.max(0, +(v - 0.1).toFixed(1))),
  });

  // Client-side filtering on top of the current visible set.
  //
  // Semantic: node filters (search, node-type, taxonomy) determine the set
  // of "main" nodes. If expandHops > 0, main-node neighborhoods are pulled
  // in by BFS over the full graph topology — neighbors do NOT have to pass
  // the filters. This matches user expectation: "filter to Katy, show her
  // 2-hop neighborhood" should surface every node within 2 hops of Katy,
  // not just the ones that also happen to match the filter.
  const filteredGraphData = useMemo(() => {
    if (!graphData) return null;

    const hasAnyNodeFilter =
      !!searchQuery || !!nodeTypeFilter || !!taxonomyKeyword || !!taxonomyPath;

    // Importance threshold is handled at the *render* layer (nodeVisibility /
    // linkVisibility callbacks in GraphCanvas) so the physics layout doesn't
    // restart every time the user nudges the slider. filteredGraphData here
    // only handles search / type / taxonomy filters, which DO change the
    // membership of the simulated graph.

    // Step 1: compute main-node ids by applying all node filters.
    let mainIds: Set<string>;
    if (hasAnyNodeFilter) {
      let mains = graphData.nodes;
      if (searchQuery) {
        const q = searchQuery.toLowerCase();
        mains = mains.filter(n =>
          n.label.toLowerCase().includes(q) ||
          (n.description && n.description.toLowerCase().includes(q)) ||
          (n.aliases && n.aliases.some(a => a.toLowerCase().includes(q)))
        );
      }
      if (nodeTypeFilter) {
        mains = mains.filter(n => n.type === nodeTypeFilter);
      }
      if (taxonomyKeyword || taxonomyPath) {
        mains = mains.filter(n => passesTaxonomyFilters(n, taxonomyKeyword, taxonomyPath));
      }
      mainIds = new Set(mains.map(n => n.id));
    } else {
      mainIds = new Set(graphData.nodes.map(n => n.id));
    }

    // Step 2: expand N hops from main nodes by BFS over the full edge set.
    // Neighbors are added regardless of filter matches — the filter defines
    // main nodes, hops defines reach.
    const visibleIds = new Set<string>(mainIds);
    if (expandHops > 0 && mainIds.size > 0) {
      let frontier = new Set<string>(mainIds);
      for (let hop = 0; hop < expandHops; hop++) {
        const next = new Set<string>();
        for (const e of graphData.edges) {
          const src = getNodeId(e.source_node);
          const tgt = getNodeId(e.target_node);
          if (frontier.has(src) && !visibleIds.has(tgt)) next.add(tgt);
          if (frontier.has(tgt) && !visibleIds.has(src)) next.add(src);
        }
        next.forEach(id => visibleIds.add(id));
        if (next.size === 0) break;
        frontier = next;
      }
    }

    // Step 3: materialize visible nodes + visible edges. Edge-type filter
    // is orthogonal (it's about edge identity, not node membership) and is
    // the only filter that still applies after hop expansion.
    const filteredNodes = graphData.nodes.filter(n => visibleIds.has(n.id));
    let filteredEdges = graphData.edges.filter(e =>
      visibleIds.has(getNodeId(e.source_node)) && visibleIds.has(getNodeId(e.target_node))
    );
    if (edgeTypeFilter) {
      filteredEdges = filteredEdges.filter(e => e.type === edgeTypeFilter);
    }

    return { nodes: filteredNodes, edges: filteredEdges, timestamp: graphData.timestamp };
  }, [graphData, searchQuery, nodeTypeFilter, edgeTypeFilter, taxonomyKeyword, taxonomyPath, expandHops]);

  const filteredStats = useMemo(() => {
    if (!filteredGraphData) return null;
    return calculateStats(filteredGraphData);
  }, [filteredGraphData]);

  // Auto-expand on unique search match. When the user filters down to
  // exactly one node (by label, alias, or description), fetch that node's
  // subgraph at the currently-selected hops. Debounced so typing doesn't
  // fire a fetch per keystroke.
  useEffect(() => {
    if (searchExpandTimer.current) {
      clearTimeout(searchExpandTimer.current);
      searchExpandTimer.current = null;
    }
    if (!searchQuery || !filteredGraphData) return;
    const matches = filteredGraphData.nodes;
    if (matches.length !== 1) return;
    const match = matches[0];
    searchExpandTimer.current = setTimeout(() => {
      expandNode(match.id, expandHops).catch(err => {
        console.error('Error auto-expanding from search:', err);
      });
    }, 400);
    return () => {
      if (searchExpandTimer.current) {
        clearTimeout(searchExpandTimer.current);
        searchExpandTimer.current = null;
      }
    };
  }, [searchQuery, expandHops, filteredGraphData, expandNode]);

  // Visible counts respect both the (membership) filter and the (render-time)
  // importance threshold. Nodes with null importance are treated as below
  // any positive threshold.
  const { visibleNodeCount, visibleEdgeCount } = useMemo(() => {
    if (!filteredGraphData) return { visibleNodeCount: 0, visibleEdgeCount: 0 };
    const visibleNodeIds = new Set<string>();
    for (const n of filteredGraphData.nodes) {
      if ((n.importance ?? -1) >= sliderValue) visibleNodeIds.add(n.id);
    }
    let edgeCount = 0;
    for (const e of filteredGraphData.edges) {
      if (visibleNodeIds.has(getNodeId(e.source_node)) &&
          visibleNodeIds.has(getNodeId(e.target_node))) edgeCount++;
    }
    return { visibleNodeCount: visibleNodeIds.size, visibleEdgeCount: edgeCount };
  }, [filteredGraphData, sliderValue]);

  return (
    <div className="h-screen flex flex-col bg-gray-900">
      <HeaderBar
        onRefresh={() => resetToHubs(sliderValue)}
        onExport={handleExport}
        onOpenSearch={() => setShowSearchModal(true)}
        loading={loading}
        showStats={showStats}
        onToggleStats={() => setShowStats(!showStats)}
        importanceThreshold={sliderValue}
        onImportanceChange={handleImportanceChange}
        onResetToHubs={handleResetToHubs}
        visibleNodeCount={visibleNodeCount}
        visibleEdgeCount={visibleEdgeCount}
        viewMode={viewMode}
        onToggleViewMode={() => {
          setViewMode(v => v === '3d' ? '2d' : '3d');
          resetPhysics();
        }}
        expandHops={expandHops}
        onExpandHopsChange={setExpandHops}
      />

      <SearchModal
        isOpen={showSearchModal}
        onClose={() => setShowSearchModal(false)}
        filters={getFilters()}
        onFilterChange={updateFilters}
        onClearFilters={clearFilters}
      />

      <SearchFilters
        filters={getFilters()}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onUpdateSearchQuery={updateSearchQuery}
        onUpdateNodeTypeFilter={updateNodeTypeFilter}
        onUpdateEdgeTypeFilter={updateEdgeTypeFilter}
        onSearch={() => {}}
        onClearFilters={clearFilters}
        loading={loading}
      />

      {showStats && (
        <StatsPanel
          stats={filteredStats}
          loading={loading}
          isFiltered={!!(searchQuery || nodeTypeFilter || edgeTypeFilter || taxonomyKeyword || taxonomyPath)}
          onHide={() => setShowStats(false)}
        />
      )}

      <div className="flex-1 flex min-h-0">
        <GraphCanvas
          graphData={filteredGraphData}
          highlightedNodes={highlightedNodes}
          highlightedEdges={highlightedEdges}
          viewMode={viewMode}
          onNodeClick={handleNodeClick}
          onEdgeClick={handleEdgeClick}
          onNodeRightClick={handleNodeRightClick}
          onBackgroundClick={handleBackgroundClick}
          graphRef={graphRef}
          onEngineStop={onEngineStop}
          onEngineTick={onEngineTick}
          nodeTypes={nodeTypes}
          graphStable={graphStable}
          tickCount={tickCount}
          importanceThreshold={sliderValue}
        />

        <Sidebar
          isOpen={sidebarOpen}
          onClose={() => setSidebarOpen(false)}
          selectedNode={selectedNode}
          selectedEdge={selectedEdge}
          nodeDetails={nodeDetails}
          onNodeSave={handleNodeSave}
          onNodeDelete={handleNodeDelete}
          onEdgeSave={handleEdgeSave}
          onEdgeDelete={handleEdgeDelete}
          getNodeLabelById={(id: string | any) => getNodeLabelById(id, graphData)}
          getNodeId={getNodeId}
        />
      </div>

      <ContextMenu
        contextMenu={contextMenu}
        onClose={closeContextMenu}
        onViewNode={nodeId => {
          const node = graphData?.nodes.find(n => n.id === nodeId);
          if (node) handleNodeClick(node);
        }}
        onDeleteNode={handleNodeDelete}
      />

      <KeyboardHelpModal isOpen={showHelpModal} onClose={() => setShowHelpModal(false)} />

      {/* No separate full-screen LoadingScreen — the GraphCanvas's
          "Computing layout…" / "Loading graph…" dark overlay handles both
          the fetch and physics-settling phases. */}

      {error && <LoadError error={error} retryCount={0} onRetry={() => resetToHubs(sliderValue)} />}
    </div>
  );
};

export default App;
