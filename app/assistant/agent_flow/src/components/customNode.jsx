import React, { useState } from 'react';
import { Handle, Position, useStore } from 'reactflow';

function CustomNode({ id, data }) {
  const isControl = data.type === 'control_node';
  const isReversed = ['planner', 'checklist', 'web_planner'].includes(id);
  const [hovered, setHovered] = useState(false);
  const disableHandles = data.disableHandles; // from node data
  const isEdgeUpdating = data.isEdgeUpdating || false; // from node data

  // Allow connections if edge updating OR if handles are not disabled
  const allowConnections = isEdgeUpdating || !disableHandles;

  // Retrieve current edges to determine if this node is connected
  const edges = useStore((s) => s.edges);
  const incoming = edges.some((e) => e.target === id);
  const outgoing = edges.some((e) => e.source === id);

  const bgColor = isControl ? 'bg-blue-100' : 'bg-green-100';
  const textColor = isControl ? 'text-blue-800' : 'text-green-800';
  const borderColor = isControl ? 'border-blue-300' : 'border-green-300';

  const makeHandleStyle = (type, connected) => ({
    left: '50%',
    transform: 'translateX(-50%)',
    width: 12,
    height: 12,
    background: isEdgeUpdating ? '#f97316' : (hovered ? '#fff' : 'transparent'),
    border: isEdgeUpdating ? '2px solid #fff' : (hovered ? '2px solid #999' : 'none'),
    borderRadius: '999px',
    pointerEvents: 'all',
    zIndex: isEdgeUpdating ? 10 : 5,
  });

  const topType = isReversed ? 'source' : 'target';
  const bottomType = isReversed ? 'target' : 'source';

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      className={`relative px-4 py-3 text-center border rounded-md shadow whitespace-nowrap ${bgColor} ${textColor} ${borderColor}`}
    >
      <Handle
        type={topType}
        position={Position.Top}
        className="!absolute"
        style={{
          top: 0,
          ...makeHandleStyle(topType, topType === 'source' ? outgoing : incoming),
        }}
        isConnectable={allowConnections}
        isValidConnection={() => allowConnections}
      />

      <div
        className="text-sm font-semibold"
        style={{
          maxWidth: '120px',
          whiteSpace: 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          margin: '0 auto',
        }}
        title={data.label}
      >
        {data.label}
      </div>


      <Handle
        type={bottomType}
        position={Position.Bottom}
        className="!absolute"
        style={{
          bottom: 0,
          ...makeHandleStyle(bottomType, bottomType === 'source' ? outgoing : incoming),
        }}
        isConnectable={allowConnections}
        isValidConnection={() => allowConnections}
      />
    </div>
  );
}

export default CustomNode;
