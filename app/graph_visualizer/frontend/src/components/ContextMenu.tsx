import React from 'react';
import { ContextMenuState } from '../types/graph';

interface ContextMenuProps {
  contextMenu: ContextMenuState;
  onClose: () => void;
  onViewNode?: (nodeId: string) => void;
  onDeleteNode?: (nodeId: string) => void;
}

const ContextMenu: React.FC<ContextMenuProps> = ({
  contextMenu,
  onClose,
  onViewNode,
  onDeleteNode
}) => {
  if (!contextMenu.isOpen || !contextMenu.node) {
    return null;
  }

  const handleViewNode = () => {
    if (onViewNode && contextMenu.node) {
      onViewNode(contextMenu.node.id);
    }
    onClose();
  };

  const handleDeleteNode = () => {
    if (onDeleteNode && contextMenu.node) {
      onDeleteNode(contextMenu.node.id);
    }
    onClose();
  };

  return (
    <div
      className="fixed bg-white border border-gray-200 rounded-md shadow-lg py-1 z-50"
      style={{
        left: contextMenu.x,
        top: contextMenu.y,
      }}
    >
      <div className="px-3 py-2 text-sm text-gray-700 border-b border-gray-100">
        <strong>{contextMenu.node.label}</strong>
      </div>
      
      <div className="py-1">
        <button
          onClick={handleViewNode}
          className="w-full px-3 py-2 text-left text-sm text-gray-700 hover:bg-gray-100 focus:outline-none focus:bg-gray-100"
        >
          👁️ View Details
        </button>
        
        <button
          onClick={handleDeleteNode}
          className="w-full px-3 py-2 text-left text-sm text-red-600 hover:bg-red-50 focus:outline-none focus:bg-red-50"
        >
          🗑️ Delete Node
        </button>
      </div>
    </div>
  );
};

export default ContextMenu;