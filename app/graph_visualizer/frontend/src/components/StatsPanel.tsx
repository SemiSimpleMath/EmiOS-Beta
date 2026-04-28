import React from 'react';
import { GraphStats } from '../types/graph';

interface StatsPanelProps {
  stats: GraphStats | null;
  loading: boolean;
  isFiltered?: boolean;
  onHide?: () => void;
}

const StatsPanel: React.FC<StatsPanelProps> = ({ stats, loading, isFiltered = false, onHide }) => {
  if (loading) {
    return (
      <div className="stats-panel">
        <div className="flex items-center space-x-2">
          <div className="loading-spinner rounded-full h-4 w-4 border-b-2 border-blue-600"></div>
          <span className="text-sm text-gray-500">Calculating statistics...</span>
        </div>
      </div>
    );
  }

  if (!stats) {
    return null;
  }

  return (
    <div className="stats-panel">
      <div className="stats-grid">
        {isFiltered && (
          <div className="text-xs text-blue-600 font-medium">
            📊 Filtered
          </div>
        )}
        <div className="stat-card">
          <div className="stat-value text-blue-600">{stats.totalNodes}</div>
          <div className="stat-label">Nodes</div>
        </div>
        <div className="stat-card">
          <div className="stat-value text-green-600">{stats.totalEdges}</div>
          <div className="stat-label">Edges</div>
        </div>
        <div className="stat-card">
          <div className="stat-value text-purple-600">{Object.keys(stats.nodeTypes).length}</div>
          <div className="stat-label">Node Types</div>
        </div>
        <div className="stat-card">
          <div className="stat-value text-orange-600">{Object.keys(stats.edgeTypes).length}</div>
          <div className="stat-label">Edge Types</div>
        </div>
        <div className="stat-card">
          <div className="stat-value text-indigo-600">{stats.avgNodeDegree.toFixed(1)}</div>
          <div className="stat-label">Avg Degree</div>
        </div>
        <div className="stat-card">
          <div className="stat-value text-pink-600">{(stats.density * 100).toFixed(2)}%</div>
          <div className="stat-label">Density</div>
        </div>
        {onHide && (
          <button
            type="button"
            onClick={onHide}
            className="stats-hide-btn"
            title="Hide stats (use the Stats button to show them again)"
            aria-label="Hide stats"
          >
            ×
          </button>
        )}
      </div>
    </div>
  );
};

export default StatsPanel;
