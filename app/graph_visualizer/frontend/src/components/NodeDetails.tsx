import React from 'react';

interface Node {
  id: string;
  label: string;
  type: string;
  description?: string;
  aliases?: string[];
  attributes: any;
  start_date?: string;
  end_date?: string;
  start_date_confidence?: string;
  end_date_confidence?: string;
  valid_during?: string;
  hash_tags?: string[];
  semantic_label?: string;
  goal_status?: string;
  confidence?: number;
  importance?: number;
  source?: string;
  created_at: string;
  updated_at: string;
}

interface NodeDetailsProps {
  selectedNode: Node;
  nodeDetails: any;
  editMode: boolean;
  showNodeJson: boolean;
  showDeleteConfirm: boolean;
  deleting: boolean;
  getNodeLabelById: (idOrNode: string | any) => string;
  getNodeId: (idOrNode: string | any) => string;
  onEdit: () => void;
  onDelete: () => void;
  onSave: (updatedNode: any) => void;
  onCancel: () => void;
  onToggleNodeJson: () => void;
  onToggleDeleteConfirm: () => void;
}

const NodeDetails: React.FC<NodeDetailsProps> = ({
  selectedNode,
  nodeDetails,
  editMode,
  showNodeJson,
  showDeleteConfirm,
  deleting,
  getNodeLabelById,
  getNodeId,
  onEdit,
  onDelete,
  onSave,
  onCancel,
  onToggleNodeJson,
  onToggleDeleteConfirm,
}) => {
  return (
    <div className="space-y-4">
      {/* Node Basic Info */}
      <div className="bg-blue-50 p-4 rounded-lg">
        <div className="flex justify-between items-start mb-2">
          <h3 className="font-semibold text-blue-800">Node Details</h3>
          <div className="flex space-x-2">
            {!editMode && (
              <button
                onClick={onEdit}
                className="px-3 py-1 bg-blue-600 text-white rounded text-sm hover:bg-blue-700"
              >
                Edit
              </button>
            )}
            {!editMode && (
              <button
                onClick={onToggleDeleteConfirm}
                className="px-3 py-1 bg-red-600 text-white rounded text-sm hover:bg-red-700"
              >
                Delete
              </button>
            )}
          </div>
        </div>
        
        {!editMode ? (
          <div className="space-y-2 text-sm">
            <div>
              <span className="font-medium">Label:</span>
              <span className="ml-2">{String(selectedNode.label)}</span>
            </div>
            <div>
              <span className="font-medium">Type:</span>
              <span className="ml-2 px-2 py-1 bg-blue-200 text-blue-800 rounded text-xs">
                {String(selectedNode.type)}
              </span>
            </div>
            {selectedNode.description && (
              <div>
                <span className="font-medium">Description:</span>
                <div className="mt-1 text-gray-700 italic">{String(selectedNode.description)}</div>
              </div>
            )}
            {Boolean(selectedNode.aliases?.length) && (
              <div>
                <span className="font-medium">Aliases:</span>
                <div className="mt-1 flex flex-wrap gap-1">
                  {selectedNode.aliases!.map((alias, idx) => (
                    <span key={idx} className="px-2 py-1 bg-gray-200 rounded text-xs">
                      {String(alias)}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>
        ) : (
          <div className="text-sm text-gray-600">
            Edit mode - form would go here
          </div>
        )}
      </div>

      {/* Attributes Card */}
      {selectedNode.attributes && Object.keys(selectedNode.attributes).length > 0 && (
        <div className="bg-gray-50 p-4 rounded-lg">
          <div className="flex items-center justify-between mb-2">
            <h3 className="font-semibold">Attributes</h3>
            <button
              onClick={onToggleNodeJson}
              className="text-sm text-blue-600 hover:text-blue-800"
            >
              {showNodeJson ? 'Hide JSON' : 'Show JSON'}
            </button>
          </div>
          
          {!showNodeJson ? (
            <div className="space-y-2 text-sm">
              {Object.entries(selectedNode.attributes).map(([key, value]) => (
                <div key={key}>
                  <span className="font-medium">{key}:</span>
                  <span className="ml-2 text-gray-700">
                    {typeof value === 'object' ? JSON.stringify(value) : String(value)}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <pre className="text-xs bg-white p-2 rounded border overflow-x-auto">
              {JSON.stringify(selectedNode.attributes, null, 2)}
            </pre>
          )}
        </div>
      )}

      {/* Relationships */}
      {nodeDetails && (
        <div className="space-y-4">
          {/* Debug info */}
          <div className="text-xs text-gray-500 bg-gray-100 p-2 rounded">
            Debug: incoming_edges: {nodeDetails.incoming_edges?.length || 0}, 
            outgoing_edges: {nodeDetails.outgoing_edges?.length || 0}
          </div>
          {/* Incoming Edges */}
          {(nodeDetails.incoming_edges || []) && (
            <div className="bg-green-50 p-4 rounded-lg">
              <h3 className="font-semibold text-green-800 mb-2">
                Incoming Relationships ({nodeDetails.incoming_edges.length})
              </h3>
              <div className="space-y-2">
                {nodeDetails.incoming_edges.map((edge: any) => (
                  <div key={edge.id} className="bg-white p-2 rounded border">
                    <div className="font-medium text-sm">
                      {edge.source_label || String(getNodeLabelById(getNodeId(edge.source)))} 
                      <span className="text-green-600 mx-1">→</span>
                      <span className="text-blue-600">{edge.type}</span>
                    </div>
                    {edge.relationship_descriptor && (
                      <div className="text-xs text-gray-600 mt-1">
                        <strong>Descriptor:</strong> {edge.relationship_descriptor}
                      </div>
                    )}
                    {edge.sentence && (
                      <div className="text-xs text-gray-600 mt-1 italic">
                        <strong>Sentence:</strong> {edge.sentence}
                      </div>
                    )}
                    <div className="mt-1 text-xs">
                      <div className="flex gap-2">
                        {edge.start_time ? (
                          <span className="px-1 py-0.5 bg-green-100 text-green-700 rounded text-xs">
                            Start: {new Date(edge.start_time).toLocaleDateString()}
                          </span>
                        ) : (
                          <span className="px-1 py-0.5 bg-gray-100 text-gray-500 rounded text-xs">
                            Start: None
                          </span>
                        )}
                        {edge.end_time ? (
                          <span className="px-1 py-0.5 bg-red-100 text-red-700 rounded text-xs">
                            End: {new Date(edge.end_time).toLocaleDateString()}
                          </span>
                        ) : (
                          <span className="px-1 py-0.5 bg-gray-100 text-gray-500 rounded text-xs">
                            End: None
                          </span>
                        )}
                      </div>
                    </div>
                    {edge.attributes && Object.keys(edge.attributes).length > 0 && (
                      <div className="mt-1 text-xs text-gray-600">
                        {Object.entries(edge.attributes).slice(0, 3).map(([k, v]) => (
                          <span key={k} className="mr-2">
                            {k}: {String(v)}
                          </span>
                        ))}
                        {Object.keys(edge.attributes).length > 3 && (
                          <span className="text-gray-400">+{Object.keys(edge.attributes).length - 3} more</span>
                        )}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Outgoing Edges */}
          {(nodeDetails.outgoing_edges || []) && (
            <div className="bg-purple-50 p-4 rounded-lg">
              <h3 className="font-semibold text-purple-800 mb-2">
                Outgoing Relationships ({nodeDetails.outgoing_edges.length})
              </h3>
              <div className="space-y-2">
                {nodeDetails.outgoing_edges.map((edge: any) => (
                  <div key={edge.id} className="bg-white p-2 rounded border">
                    <div className="font-medium text-sm">
                      <span className="text-purple-600">{edge.type}</span>
                      <span className="text-purple-600 mx-1">→</span>
                      {edge.target_label || String(getNodeLabelById(getNodeId(edge.target)))}
                    </div>
                    {edge.relationship_descriptor && (
                      <div className="text-xs text-gray-600 mt-1">
                        <strong>Descriptor:</strong> {edge.relationship_descriptor}
                      </div>
                    )}
                    {edge.sentence && (
                      <div className="text-xs text-gray-600 mt-1 italic">
                        <strong>Sentence:</strong> {edge.sentence}
                      </div>
                    )}
                    <div className="mt-1 text-xs">
                      <div className="flex gap-2">
                        {edge.start_time ? (
                          <span className="px-1 py-0.5 bg-green-100 text-green-700 rounded text-xs">
                            Start: {new Date(edge.start_time).toLocaleDateString()}
                          </span>
                        ) : (
                          <span className="px-1 py-0.5 bg-gray-100 text-gray-500 rounded text-xs">
                            Start: None
                          </span>
                        )}
                        {edge.end_time ? (
                          <span className="px-1 py-0.5 bg-red-100 text-red-700 rounded text-xs">
                            End: {new Date(edge.end_time).toLocaleDateString()}
                          </span>
                        ) : (
                          <span className="px-1 py-0.5 bg-gray-100 text-gray-500 rounded text-xs">
                            End: None
                          </span>
                        )}
                      </div>
                    </div>
                    {edge.attributes && Object.keys(edge.attributes).length > 0 && (
                      <div className="mt-1 text-xs text-gray-600">
                        {Object.entries(edge.attributes).slice(0, 3).map(([k, v]) => (
                          <span key={k} className="mr-2">
                            {k}: {String(v)}
                          </span>
                        ))}
                        {Object.keys(edge.attributes).length > 3 && (
                          <span className="text-gray-400">+{Object.keys(edge.attributes).length - 3} more</span>
                        )}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Delete Confirmation */}
      {showDeleteConfirm && (
        <div className="bg-red-50 p-4 rounded-lg">
          <h3 className="font-semibold text-red-800 mb-2">Delete Node</h3>
          {!deleting ? (
            <div className="space-y-3">
              <div className="text-sm text-red-800 bg-red-100 p-3 rounded border">
                <strong>Are you sure?</strong><br />
                This will permanently delete the node "{String(selectedNode.label)}" and all its relationships.
              </div>
              <div className="flex space-x-2">
                <button
                  onClick={onDelete}
                  className="flex-1 px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors font-medium"
                >
                  🗑️ Yes, Delete
                </button>
                <button
                  onClick={onToggleDeleteConfirm}
                  className="flex-1 px-4 py-2 bg-gray-500 text-white rounded-lg hover:bg-gray-600 transition-colors font-medium"
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div className="text-sm text-red-800 bg-red-100 p-3 rounded border">
              Deleting node...
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default NodeDetails;
