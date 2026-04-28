import React from 'react';
import { ViewMode } from '../types/graph';

interface HeaderBarProps {
  onRefresh: () => void;
  onExport: () => void;
  onOpenSearch: () => void;
  loading: boolean;
  showStats: boolean;
  onToggleStats: () => void;
  // Hub controls
  degreeThreshold: number;
  onDegreeChange: (value: number) => void;
  onResetToHubs: () => void;
  visibleNodeCount: number;
  visibleEdgeCount: number;
  // View mode
  viewMode: ViewMode;
  onToggleViewMode: () => void;
  // Hops for node expansion on click (0 = just the node, 1 = neighbors, 2 = 2-hop).
  expandHops: number;
  onExpandHopsChange: (value: number) => void;
}

const HeaderBar: React.FC<HeaderBarProps> = ({
  onRefresh,
  onExport,
  onOpenSearch,
  loading,
  showStats,
  onToggleStats,
  degreeThreshold,
  onDegreeChange,
  onResetToHubs,
  visibleNodeCount,
  visibleEdgeCount,
  viewMode,
  onToggleViewMode,
  expandHops,
  onExpandHopsChange,
}) => {
  return (
    <header className="bg-gray-900 border-b border-gray-700 px-4 py-2">
      <div className="flex items-center justify-between gap-4 flex-wrap">

        {/* Left: title + loading */}
        <div className="flex items-center space-x-3 shrink-0">
          <h1 className="text-lg font-bold text-white whitespace-nowrap">KG Visualizer</h1>
          {loading && (
            <div className="flex items-center space-x-1 text-xs text-gray-400">
              <div className="loading-spinner rounded-full h-3 w-3 border-b-2 border-gray-400 animate-spin" />
              <span>Loading…</span>
            </div>
          )}
        </div>

        {/* Center: degree slider + node count */}
        <div className="flex items-center gap-4 flex-1 min-w-0">
          <div className="flex items-center gap-2 min-w-0">
            <label className="text-xs text-gray-400 whitespace-nowrap">
              Min degree: <span className="text-white font-semibold">{degreeThreshold}</span>
            </label>
            <input
              type="range"
              min={1}
              max={20}
              value={degreeThreshold}
              onChange={e => onDegreeChange(Number(e.target.value))}
              className="w-32 accent-blue-500"
            />
          </div>

          <div className="text-xs text-gray-400 whitespace-nowrap">
            <span className="text-white font-semibold">{visibleNodeCount}</span> nodes /{' '}
            <span className="text-white font-semibold">{visibleEdgeCount}</span> edges
          </div>

          <div className="flex items-center gap-1 whitespace-nowrap">
            <label className="text-xs text-gray-400">Hops:</label>
            <select
              value={expandHops}
              onChange={e => onExpandHopsChange(Number(e.target.value))}
              className="bg-gray-800 text-white text-xs rounded px-1 py-0.5 border border-gray-700"
              title="How many hops to expand when you click a node. 0 = just the node; 1 = its direct neighbors; 2 = neighbors of neighbors."
            >
              <option value={0}>0</option>
              <option value={1}>1</option>
              <option value={2}>2</option>
              <option value={3}>3</option>
            </select>
          </div>

          <button
            onClick={onResetToHubs}
            disabled={loading}
            title="Reset to hub view"
            className="px-2 py-1 text-xs rounded bg-gray-700 text-gray-300 hover:bg-gray-600 disabled:opacity-40 whitespace-nowrap"
          >
            ↺ Reset
          </button>
        </div>

        {/* Right: action buttons */}
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={onToggleViewMode}
            title={`Switch to ${viewMode === '3d' ? '2D' : '3D'}`}
            className={`px-3 py-1 text-xs font-semibold rounded ${
              viewMode === '3d'
                ? 'bg-purple-600 text-white hover:bg-purple-700'
                : 'bg-blue-600 text-white hover:bg-blue-700'
            }`}
          >
            {viewMode === '3d' ? '3D' : '2D'}
          </button>

          <button
            onClick={onOpenSearch}
            className="px-3 py-1 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded"
          >
            Search
          </button>

          <button
            onClick={onToggleStats}
            className={`px-3 py-1 text-xs font-medium rounded ${
              showStats ? 'bg-green-600 text-white hover:bg-green-700' : 'bg-gray-700 text-white hover:bg-gray-600'
            }`}
          >
            Stats
          </button>

          <button
            onClick={onRefresh}
            disabled={loading}
            title="Refresh"
            className="px-3 py-1 text-xs font-medium bg-gray-700 text-white hover:bg-gray-600 disabled:opacity-40 rounded"
          >
            ↻
          </button>

          <button
            onClick={onExport}
            title="Export JSON"
            className="px-3 py-1 text-xs font-medium bg-gray-700 text-white hover:bg-gray-600 rounded"
          >
            ↓ JSON
          </button>
        </div>
      </div>
    </header>
  );
};

export default HeaderBar;
