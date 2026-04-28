import React from 'react';

interface GraphStats {
  totalNodes: number;
  totalEdges: number;
  nodeTypes: { [key: string]: number };
  nodeClassifications: { [key: string]: number };
  edgeTypes: { [key: string]: number };
  avgNodeDegree: number;
  density: number;
}

interface StatsDisplayProps {
  stats: GraphStats | null;
  showStats: boolean;
  onToggleStats: () => void;
}

const StatsDisplay: React.FC<StatsDisplayProps> = ({ stats, showStats, onToggleStats }) => {
  if (!stats) return null;

  return (
    <div className="bg-white bg-opacity-90 p-4 rounded-lg shadow-lg">
      <div className="flex justify-between items-center mb-4">
        <h3 className="text-lg font-semibold text-gray-800">Graph Statistics</h3>
        <button
          onClick={onToggleStats}
          className="text-sm text-gray-600 hover:text-gray-800"
        >
          {showStats ? 'Hide Details' : 'Show Details'}
        </button>
      </div>
      
      <div className="grid grid-cols-2 gap-4 text-sm">
        <div className="bg-blue-50 p-3 rounded">
          <div className="font-medium text-blue-800">Nodes</div>
          <div className="text-2xl font-bold text-blue-600">{stats.totalNodes}</div>
        </div>
        <div className="bg-green-50 p-3 rounded">
          <div className="font-medium text-green-800">Edges</div>
          <div className="text-2xl font-bold text-green-600">{stats.totalEdges}</div>
        </div>
        <div className="bg-purple-50 p-3 rounded">
          <div className="font-medium text-purple-800">Avg Degree</div>
          <div className="text-2xl font-bold text-purple-600">{stats.avgNodeDegree.toFixed(2)}</div>
        </div>
        <div className="bg-orange-50 p-3 rounded">
          <div className="font-medium text-orange-800">Density</div>
          <div className="text-2xl font-bold text-orange-600">{(stats.density * 100).toFixed(2)}%</div>
        </div>
      </div>

      {showStats && (
        <div className="mt-4 space-y-4">
          {/* Node Types */}
          <div>
            <h4 className="font-medium text-gray-700 mb-2">Node Types</h4>
            <div className="space-y-1">
              {Object.entries(stats.nodeTypes).map(([type, count]) => (
                <div key={type} className="flex justify-between text-sm">
                  <span className="text-gray-600">{type}</span>
                  <span className="font-medium">{count}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Node Classifications */}
          <div>
            <h4 className="font-medium text-gray-700 mb-2">Node Classifications</h4>
            <div className="space-y-1">
              {Object.entries(stats.nodeClassifications).map(([classification, count]) => (
                <div key={classification} className="flex justify-between text-sm">
                  <span className="text-gray-600">{classification.replace('_', ' ')}</span>
                  <span className="font-medium">{count}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Edge Types */}
          <div>
            <h4 className="font-medium text-gray-700 mb-2">Edge Types</h4>
            <div className="space-y-1">
              {Object.entries(stats.edgeTypes).map(([type, count]) => (
                <div key={type} className="flex justify-between text-sm">
                  <span className="text-gray-600">{type}</span>
                  <span className="font-medium">{count}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default StatsDisplay;
