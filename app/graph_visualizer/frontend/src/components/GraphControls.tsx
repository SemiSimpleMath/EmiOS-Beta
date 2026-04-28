import React from 'react';

interface GraphControlsProps {
  searchQuery: string;
  nodeTypeFilter: string;
  nodeClassificationFilter: string;
  edgeTypeFilter: string;
  nodeTypes: string[];
  nodeClassifications: string[];
  edgeTypes: string[];
  onSearchChange: (query: string) => void;
  onNodeTypeFilterChange: (filter: string) => void;
  onNodeClassificationFilterChange: (filter: string) => void;
  onEdgeTypeFilterChange: (filter: string) => void;
  onRefresh: () => void;
  onClearHighlights: () => void;
}

const GraphControls: React.FC<GraphControlsProps> = ({
  searchQuery,
  nodeTypeFilter,
  nodeClassificationFilter,
  edgeTypeFilter,
  nodeTypes,
  nodeClassifications,
  edgeTypes,
  onSearchChange,
  onNodeTypeFilterChange,
  onNodeClassificationFilterChange,
  onEdgeTypeFilterChange,
  onRefresh,
  onClearHighlights,
}) => {
  return (
    <div className="mt-4 flex flex-wrap gap-4 items-center">
      <input
        type="text"
        placeholder="🔍 Search nodes, aliases, or descriptions..."
        value={searchQuery}
        onChange={(e) => onSearchChange(e.target.value)}
        className="w-full px-4 py-2 text-black rounded-lg border-0 focus:ring-2 focus:ring-white focus:ring-opacity-50"
      />
      
      <select
        value={nodeTypeFilter}
        onChange={(e) => onNodeTypeFilterChange(e.target.value)}
        className="px-4 py-2 text-black rounded-lg border-0 focus:ring-2 focus:ring-white focus:ring-opacity-50"
      >
        <option value="">All Node Types</option>
        {nodeTypes.map(type => (
          <option key={type} value={type}>{type}</option>
        ))}
      </select>
      
      <select
        value={nodeClassificationFilter}
        onChange={(e) => onNodeClassificationFilterChange(e.target.value)}
        className="px-4 py-2 text-black rounded-lg border-0 focus:ring-2 focus:ring-white focus:ring-opacity-50"
      >
        <option value="">All Node Classifications</option>
        {nodeClassifications.map(classification => (
          <option key={classification} value={classification}>{classification.replace('_', ' ')}</option>
        ))}
      </select>
      
      <select
        value={edgeTypeFilter}
        onChange={(e) => onEdgeTypeFilterChange(e.target.value)}
        className="px-4 py-2 text-black rounded-lg border-0 focus:ring-2 focus:ring-white focus:ring-opacity-50"
      >
        <option value="">All Edge Types</option>
        {edgeTypes.map(type => (
          <option key={type} value={type}>{type}</option>
        ))}
      </select>
      
      <button 
        onClick={onRefresh}
        className="px-4 py-2 bg-white bg-opacity-20 rounded-lg hover:bg-opacity-30 transition-all"
      >
        🔄 Refresh
      </button>
      
      <button 
        onClick={onClearHighlights}
        className="px-4 py-2 bg-white bg-opacity-20 rounded-lg hover:bg-opacity-30 transition-all"
      >
        🎯 Clear Highlights
      </button>
    </div>
  );
};

export default GraphControls;
