import React, { useState } from 'react';
import { Node, NodeDetailsData } from '../../types/graph';

interface NodePanelProps {
  node: Node;
  nodeDetails: NodeDetailsData | null;
  onSave?: (updatedNode: Partial<Node>) => void;
  onDelete?: (nodeId: string) => void;
}

const NodePanel: React.FC<NodePanelProps> = ({ node, nodeDetails, onSave, onDelete }) => {
  const [isEditing, setIsEditing] = useState(false);
  const [editedNode, setEditedNode] = useState<Partial<Node>>({});

  // Debug: Log all node data (commented out for production)
  // console.log('NodePanel - Full node data:', node);

  // Debug: Log node data for Event, State, and Goal nodes
  if (node.type === 'State' || node.type === 'Event' || node.type === 'Goal') {
    console.log('NodePanel Debug - Node data:', {
      type: node.type,
      category: node.category,
      confidence: node.confidence,
      importance: node.importance,
      source: node.source,
      hash_tags: node.hash_tags,
      semantic_label: node.semantic_label,
      goal_status: node.goal_status,
      original_sentence: node.original_sentence
    });
  }


  const handleEdit = () => {
    console.log('Edit button clicked for node:', node.label);
    setEditedNode({
      label: node.label,
      type: node.type,
      semantic_label: node.semantic_label || '',
      description: node.description || '',
      aliases: node.aliases || []
    });
    setIsEditing(true);
  };

  const handleSave = () => {
    if (onSave) {
      onSave(editedNode);
    }
    setIsEditing(false);
  };

  const handleCancel = () => {
    setEditedNode({});
    setIsEditing(false);
  };

  const handleDelete = () => {
    if (onDelete && window.confirm(`Are you sure you want to delete "${node.label}"?`)) {
      onDelete(node.id);
    }
  };

  return (
    <div className="space-y-6">
      {/* Node Basic Info */}
      <div className="bg-blue-50 p-4 rounded-lg">
        <h3 className="font-semibold text-blue-800 mb-3">Node Information</h3>
        <div className="space-y-2 text-sm">
          <div>
            <span className="font-medium">ID:</span> {node.id}
          </div>
          <div>
            <span className="font-medium">Type:</span> 
            <span className="ml-2 px-2 py-1 bg-blue-200 text-blue-800 rounded text-xs">
              {node.type}
            </span>
          </div>
        </div>
      </div>

      {/* Taxonomy Classifications */}
      {node.taxonomy_paths && node.taxonomy_paths.length > 0 && (
        <div className="bg-indigo-50 p-4 rounded-lg">
          <h3 className="font-semibold text-indigo-800 mb-3">Taxonomy Classifications</h3>
          <div className="space-y-2">
            {node.taxonomy_paths.map((path, index) => (
              <div key={index} className="text-sm bg-white p-2 rounded border border-indigo-200">
                <span className="text-indigo-700 font-mono text-xs">{path}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Editable Fields */}
      <div className="bg-gray-50 p-4 rounded-lg">
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-semibold text-gray-800">Details</h3>
          {!isEditing && (
            <button
              onClick={handleEdit}
              className="px-3 py-1 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              ✏️ Edit
            </button>
          )}
        </div>
        
        {isEditing ? (
          <div className="space-y-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Label
              </label>
              <input
                type="text"
                value={editedNode.label || ''}
                onChange={(e) => setEditedNode((prev: Partial<Node>) => ({ ...prev, label: e.target.value }))}
                className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Type
              </label>
              <select
                value={editedNode.type || node.type}
                onChange={(e) => setEditedNode((prev: Partial<Node>) => ({ ...prev, type: e.target.value }))}
                className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white"
              >
                <option value="Entity">Entity</option>
                <option value="Event">Event</option>
                <option value="State">State</option>
                <option value="Goal">Goal</option>
                <option value="Concept">Concept</option>
                <option value="Property">Property</option>
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Semantic Label
              </label>
              <input
                type="text"
                value={editedNode.semantic_label || ''}
                onChange={(e) => setEditedNode((prev: Partial<Node>) => ({ ...prev, semantic_label: e.target.value }))}
                className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                placeholder="Optional semantic label"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Description
              </label>
              <textarea
                value={editedNode.description || ''}
                onChange={(e) => setEditedNode((prev: Partial<Node>) => ({ ...prev, description: e.target.value }))}
                rows={3}
                className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Aliases <span className="text-gray-400 font-normal">(comma-separated)</span>
              </label>
              <input
                type="text"
                value={(editedNode.aliases || []).join(', ')}
                onChange={(e) => setEditedNode((prev: Partial<Node>) => ({
                  ...prev,
                  aliases: e.target.value.split(',').map((a: string) => a.trim()).filter((a: string) => a),
                }))}
                className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                placeholder="e.g. Nick, Nicholas, Nicky"
              />
            </div>
            <div className="flex space-x-2">
              <button
                onClick={handleSave}
                className="px-3 py-1 text-sm bg-blue-600 text-white rounded hover:bg-blue-700"
              >
                Save
              </button>
              <button
                onClick={handleCancel}
                className="px-3 py-1 text-sm bg-gray-300 text-gray-700 rounded hover:bg-gray-400"
              >
                Cancel
              </button>
            </div>
          </div>
        ) : (
        <div className="space-y-2 text-sm">
          <div>
            <span className="font-medium">Label:</span> {node.label}
          </div>
          {node.semantic_label && (
            <div>
              <span className="font-medium">Semantic Label:</span> {node.semantic_label}
            </div>
          )}
            {node.description && (
              <div>
                <span className="font-medium">Description:</span> {node.description}
              </div>
            )}
            {node.original_sentence && (
              <div>
                <span className="font-medium">Original Sentence:</span> 
                <div className="mt-1 p-2 bg-gray-100 rounded text-xs italic">
                  {node.original_sentence}
                </div>
              </div>
            )}
            {Boolean(node.aliases?.length) && (
              <div>
                <span className="font-medium">Aliases:</span> {node.aliases!.join(', ')}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Connections */}
      {nodeDetails && (
        <div className="space-y-4">
          
          {/* Incoming Edges */}
          {nodeDetails.incoming_edges && nodeDetails.incoming_edges.length > 0 && (
            <div className="bg-green-50 p-4 rounded-lg">
              <h3 className="font-semibold text-green-800 mb-2">
                Incoming Relationships ({nodeDetails.incoming_edges.length})
              </h3>
              <div className="space-y-2">
                {nodeDetails.incoming_edges.map((edge: any) => (
                  <div key={edge.id} className="bg-white p-2 rounded border">
                    <div className="font-medium text-sm">
                      {edge.source_label || 'Unknown Source'} 
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
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Outgoing Edges */}
          {nodeDetails.outgoing_edges && nodeDetails.outgoing_edges.length > 0 && (
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
                      {edge.target_label || 'Unknown Target'}
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
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Comprehensive Information - Always show for State, Event, and Goal nodes */}
      {(node.type === 'State' || node.type === 'Event' || node.type === 'Goal') && (
        <div className="bg-orange-50 p-4 rounded-lg">
          <h3 className="font-semibold text-orange-800 mb-3">Node Metadata</h3>
          <div className="space-y-2 text-sm">
            {/* Category */}
            {node.category && (
              <div>
                <span className="font-medium">Category:</span> {node.category}
              </div>
            )}
            
            {/* Temporal Information */}
            <div>
              <span className="font-medium">Start Date:</span> {node.start_date || 'Not set'}
            </div>
            <div>
              <span className="font-medium">End Date:</span> {node.end_date || 'Not set'}
            </div>
            {node.start_date_confidence && (
              <div>
                <span className="font-medium">Start Date Confidence:</span> {node.start_date_confidence}
              </div>
            )}
            {node.end_date_confidence && (
              <div>
                <span className="font-medium">End Date Confidence:</span> {node.end_date_confidence}
              </div>
            )}
            <div>
              <span className="font-medium">Valid During:</span> {node.valid_during || 'Not set'}
            </div>
            
            {/* Hash Tags */}
            {Boolean(node.hash_tags?.length) && (
              <div>
                <span className="font-medium">Hash Tags:</span> {node.hash_tags!.join(', ')}
              </div>
            )}
            
            {/* Semantic Label */}
            {node.semantic_label && (
              <div>
                <span className="font-medium">Semantic Label:</span> {node.semantic_label}
              </div>
            )}
            
            {/* Goal Status (for Goal nodes) */}
            {node.type === 'Goal' && node.goal_status && (
              <div>
                <span className="font-medium">Goal Status:</span> {node.goal_status}
              </div>
            )}
            
            {/* Confidence and Importance */}
            <div>
              <span className="font-medium">Confidence:</span> {node.confidence !== undefined && node.confidence !== null ? node.confidence : 'Not set'}
            </div>
            <div>
              <span className="font-medium">Importance:</span> {node.importance !== undefined && node.importance !== null ? node.importance : 'Not set'}
            </div>
            
            {/* Source */}
            <div>
              <span className="font-medium">Source:</span> {node.source || 'Not set'}
            </div>
          </div>
        </div>
      )}

      {/* Temporal Information for other node types */}
      {!(node.type === 'State' || node.type === 'Event' || node.type === 'Goal') && (node.start_date || node.end_date || node.valid_during) && (
        <div className="bg-orange-50 p-4 rounded-lg">
          <h3 className="font-semibold text-orange-800 mb-3">Temporal Information</h3>
          <div className="space-y-2 text-sm">
            <div>
              <span className="font-medium">Start Date:</span> {node.start_date || 'Not set'}
            </div>
            <div>
              <span className="font-medium">End Date:</span> {node.end_date || 'Not set'}
            </div>
            <div>
              <span className="font-medium">Valid During:</span> {node.valid_during || 'Not set'}
            </div>
          </div>
        </div>
      )}

      {/* System Timestamps */}
      <div className="bg-yellow-50 p-4 rounded-lg">
        <h3 className="font-semibold text-yellow-800 mb-3">System Timestamps</h3>
        <div className="space-y-2 text-sm">
          <div>
            <span className="font-medium">Created:</span> {node.created_at}
          </div>
          <div>
            <span className="font-medium">Updated:</span> {node.updated_at}
          </div>
        </div>
      </div>

      {/* Actions */}
      <div className="flex space-x-2">
        <button
          onClick={handleDelete}
          className="px-4 py-2 text-sm font-medium text-white bg-red-600 border border-transparent rounded-md hover:bg-red-700 focus:outline-none focus:ring-2 focus:ring-red-500"
        >
          🗑️ Delete Node
        </button>
      </div>
    </div>
  );
};

export default NodePanel;
