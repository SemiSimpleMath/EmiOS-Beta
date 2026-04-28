import React from 'react';
import { Node, Edge, NodeDetailsData } from '../../types/graph';
import NodePanel from './NodePanel';
import EdgePanel from './EdgePanel';

interface SidebarProps {
  isOpen: boolean;
  onClose: () => void;
  selectedNode: Node | null;
  selectedEdge: Edge | null;
  nodeDetails: NodeDetailsData | null;
  onNodeSave?: (updatedNode: Partial<Node>) => void;
  onNodeDelete?: (nodeId: string) => void;
  onEdgeSave?: (updatedEdge: Partial<Edge>) => void;
  onEdgeDelete?: (edgeId: string) => void;
  getNodeLabelById: (idOrNode: string | any) => string;
  getNodeId: (idOrNode: string | any) => string;
}

const Sidebar: React.FC<SidebarProps> = ({
  isOpen,
  onClose,
  selectedNode,
  selectedEdge,
  nodeDetails,
  onNodeSave,
  onNodeDelete,
  onEdgeSave,
  onEdgeDelete,
  getNodeLabelById,
  getNodeId
}) => {
  if (!isOpen) return null;

  return (
    <div className="sidebar fixed right-0 top-0 h-full w-96 z-40">
      <div className="flex flex-col h-full">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-gray-200">
          <h2 className="text-lg font-semibold text-gray-900">
            {selectedNode ? 'Node Details' : selectedEdge ? 'Edge Details' : 'Details'}
          </h2>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 focus:outline-none"
          >
            <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
        
        {/* Content */}
        <div className="node-details flex-1 overflow-y-auto">
          {selectedNode && (
            <NodePanel
              node={nodeDetails?.node || selectedNode}
              nodeDetails={nodeDetails}
              onSave={onNodeSave}
              onDelete={onNodeDelete}
            />
          )}
          
          {selectedEdge && (
            <EdgePanel
              edge={selectedEdge}
              onSave={onEdgeSave}
              onDelete={onEdgeDelete}
              getNodeLabelById={getNodeLabelById}
              getNodeId={getNodeId}
            />
          )}
          
          {!selectedNode && !selectedEdge && (
            <div className="text-center text-gray-500 py-8">
              <p>Select a node or edge to view details</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default Sidebar;
