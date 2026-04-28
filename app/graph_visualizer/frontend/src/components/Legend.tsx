import React, { useState } from 'react';
import { nodeColors } from '../lib/colors';

interface LegendProps {
  nodeTypes: string[];
}

const Legend: React.FC<LegendProps> = ({ nodeTypes }) => {
  const [open, setOpen] = useState(true);
  const essentialTypes = ['Entity', 'Event', 'State', 'Goal', 'Property', 'Concept'];

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="legend-show-btn"
        title="Show legend"
        aria-label="Show legend"
      >
        Legend
      </button>
    );
  }

  return (
    <div className="legend-overlay">
      <div className="legend-header">
        <h3 className="text-sm font-medium text-gray-900">Legend</h3>
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="legend-hide-btn"
          title="Hide legend"
          aria-label="Hide legend"
        >
          ×
        </button>
      </div>

      <div className="space-y-2">
        <div className="flex flex-wrap gap-2">
          {essentialTypes.map(type => (
            <div key={type} className="flex items-center space-x-1">
              <div
                className="w-3 h-3 rounded-full"
                style={{ backgroundColor: nodeColors[type] || nodeColors.default }}
              ></div>
              <span className="text-xs text-gray-600">{type}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};

export default Legend;
