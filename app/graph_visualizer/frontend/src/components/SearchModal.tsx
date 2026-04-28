import React from 'react';
import { SearchFilters } from '../types/graph';

interface SearchModalProps {
  isOpen: boolean;
  onClose: () => void;
  filters: SearchFilters;
  onFilterChange: (filters: SearchFilters) => void;
  onClearFilters: () => void;
}

export const SearchModal: React.FC<SearchModalProps> = ({
  isOpen,
  onClose,
  filters,
  onFilterChange,
  onClearFilters,
}) => {
  if (!isOpen) return null;

  const handleClearAll = () => {
    onClearFilters();
  };

  const hasActiveFilters = 
    filters.searchQuery || 
    filters.nodeTypeFilter || 
    filters.edgeTypeFilter || 
    filters.taxonomyKeyword || 
    filters.taxonomyPath;

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl w-full max-w-2xl mx-4">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-gray-200 dark:border-gray-700">
          <h2 className="text-xl font-semibold text-gray-900 dark:text-white">
            Search & Filter
          </h2>
          <button
            onClick={onClose}
            className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded text-gray-500 dark:text-gray-400 text-xl"
          >
            ✕
          </button>
        </div>

        {/* Content */}
        <div className="p-6 space-y-6">
          {/* Node Label Search */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
              Node Label Search
            </label>
            <input
              type="text"
              value={filters.searchQuery}
              onChange={(e) => onFilterChange({ ...filters, searchQuery: e.target.value })}
              placeholder="Search by node label..."
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md 
                       bg-white dark:bg-gray-700 text-gray-900 dark:text-white
                       focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Find nodes by their label (e.g., "Jukka", "robot")
            </p>
          </div>

          {/* Taxonomy Keyword Filter */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
              Taxonomy Keyword
            </label>
            <input
              type="text"
              value={filters.taxonomyKeyword}
              onChange={(e) => onFilterChange({ ...filters, taxonomyKeyword: e.target.value })}
              placeholder="e.g., robot, machine, person..."
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md 
                       bg-white dark:bg-gray-700 text-gray-900 dark:text-white
                       focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Match nodes with ANY taxonomy containing this keyword
            </p>
          </div>

          {/* Taxonomy Path Filter */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
              Taxonomy Path
            </label>
            <input
              type="text"
              value={filters.taxonomyPath}
              onChange={(e) => onFilterChange({ ...filters, taxonomyPath: e.target.value })}
              placeholder="e.g., entity > artifact > robot"
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md 
                       bg-white dark:bg-gray-700 text-gray-900 dark:text-white
                       focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Ordered partial match: "robot &gt; battlebot" matches "...competition_robot &gt; battle_robot &gt; battlebot"
            </p>
          </div>

          {/* Node Type Filter */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
              Node Type
            </label>
            <input
              type="text"
              value={filters.nodeTypeFilter}
              onChange={(e) => onFilterChange({ ...filters, nodeTypeFilter: e.target.value })}
              placeholder="e.g., Entity, Event, State..."
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md 
                       bg-white dark:bg-gray-700 text-gray-900 dark:text-white
                       focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>

          {/* Edge Type Filter */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
              Edge Type
            </label>
            <input
              type="text"
              value={filters.edgeTypeFilter}
              onChange={(e) => onFilterChange({ ...filters, edgeTypeFilter: e.target.value })}
              placeholder="e.g., Has_State, Participant..."
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md 
                       bg-white dark:bg-gray-700 text-gray-900 dark:text-white
                       focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between p-4 border-t border-gray-200 dark:border-gray-700">
          <button
            onClick={handleClearAll}
            disabled={!hasActiveFilters}
            className="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 
                     hover:bg-gray-100 dark:hover:bg-gray-700 rounded-md
                     disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Clear All Filters
          </button>
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium text-white bg-blue-600 
                     hover:bg-blue-700 rounded-md"
          >
            Apply
          </button>
        </div>
      </div>
    </div>
  );
};

