import React from 'react';
import { SearchFilters as SearchFiltersType } from '../types/graph';

interface SearchFiltersProps {
  filters: SearchFiltersType;
  nodeTypes: string[];
  edgeTypes: string[];
  onUpdateSearchQuery: (query: string) => void;
  onUpdateNodeTypeFilter: (filter: string) => void;
  onUpdateEdgeTypeFilter: (filter: string) => void;
  onSearch: () => void;
  onClearFilters: () => void;
  loading: boolean;
}

const SearchFilters: React.FC<SearchFiltersProps> = ({
  filters,
  nodeTypes,
  edgeTypes,
  onUpdateSearchQuery,
  onUpdateNodeTypeFilter,
  onUpdateEdgeTypeFilter,
  onSearch,
  onClearFilters,
  loading
}) => {
  const handleKeyPress = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      onSearch();
    }
  };

  return (
    <div className="search-container">
      <div className="flex flex-wrap items-center gap-4">
        {/* Search input */}
        <div className="flex-1 min-w-64">
          <input
            type="text"
            placeholder="Search nodes and edges..."
            value={filters.searchQuery}
            onChange={(e) => onUpdateSearchQuery(e.target.value)}
            onKeyPress={handleKeyPress}
            className="search-input w-full"
          />
        </div>
        
        {/* Node type filter */}
        <div className="min-w-48">
          <select
            value={filters.nodeTypeFilter}
            onChange={(e) => onUpdateNodeTypeFilter(e.target.value)}
            className="search-select w-full"
          >
            <option value="">All Node Types</option>
            {nodeTypes.map(type => (
              <option key={type} value={type}>{type}</option>
            ))}
          </select>
        </div>
        
        {/* Edge type filter */}
        <div className="min-w-48">
          <select
            value={filters.edgeTypeFilter}
            onChange={(e) => onUpdateEdgeTypeFilter(e.target.value)}
            className="search-select w-full"
          >
            <option value="">All Edge Types</option>
            {edgeTypes.map(type => (
              <option key={type} value={type}>{type}</option>
            ))}
          </select>
        </div>
        
        {/* Action buttons */}
        <div className="flex items-center space-x-2">
          <button
            onClick={onSearch}
            disabled={loading}
            className="search-button bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            🔍 Search
          </button>
          <button
            onClick={onClearFilters}
            className="search-button bg-white text-gray-700 hover:bg-gray-50"
          >
            🗑️ Clear
          </button>
        </div>
      </div>
    </div>
  );
};

export default SearchFilters;
