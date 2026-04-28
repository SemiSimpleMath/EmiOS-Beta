import React, { useState } from 'react';
import { Edge } from '../../types/graph';

interface EdgePanelProps {
  edge: Edge;
  onSave?: (updatedEdge: Partial<Edge>) => void;
  onDelete?: (edgeId: string) => void;
  getNodeLabelById: (idOrNode: string | any) => string;
  getNodeId: (idOrNode: string | any) => string;
}

const EdgePanel: React.FC<EdgePanelProps> = ({ 
  edge, 
  onSave,
  onDelete, 
  getNodeLabelById, 
  getNodeId 
}) => {
  const [isEditing, setIsEditing] = useState(false);
  const [editedEdge, setEditedEdge] = useState<Partial<Edge>>({});

  const handleEdit = () => {
    setEditedEdge({
      type: edge.type,
      confidence: edge.confidence,
      importance: edge.importance,
      source: edge.source,
      sentence: edge.sentence
    });
    setIsEditing(true);
  };

  const handleSave = () => {
    if (onSave) {
      // Only send the fields that were actually edited, not the entire edge object
      const changesToSave: Partial<Edge> = {};
      
      if (editedEdge.type !== edge.type) {
        changesToSave.type = editedEdge.type;
      }
      if (editedEdge.confidence !== edge.confidence) {
        changesToSave.confidence = editedEdge.confidence;
      }
      if (editedEdge.importance !== edge.importance) {
        changesToSave.importance = editedEdge.importance;
      }
      if (editedEdge.source !== edge.source) {
        changesToSave.source = editedEdge.source;
      }
      if (editedEdge.sentence !== edge.sentence) {
        changesToSave.sentence = editedEdge.sentence;
      }
      
      console.log('EdgePanel: Sending only changed fields:', changesToSave);
      onSave(changesToSave);
    }
    setIsEditing(false);
  };

  const handleCancel = () => {
    setEditedEdge({});
    setIsEditing(false);
  };

  const handleDelete = () => {
    if (onDelete && window.confirm(`Are you sure you want to delete this edge?`)) {
      onDelete(edge.id);
    }
  };

  return (
    <div className="space-y-4">
      {/* Edge Basic Info */}
      <div className="bg-orange-50 p-4 rounded-lg">
        <h3 className="font-semibold text-orange-800 mb-2">Relationship</h3>
        <div className="space-y-2 text-sm">
          <div className="flex items-center">
            <span className="font-medium">Type:</span>
            <span className="ml-2 px-2 py-1 bg-orange-200 text-orange-800 rounded text-xs">
              {String(edge.type)}
            </span>
          </div>
          <div>
            <span className="font-medium">From:</span> 
            <span className="ml-1 font-semibold text-blue-600">{String(getNodeLabelById(getNodeId(edge.source_node)))}</span>
            <div className="text-xs text-gray-500 ml-16">ID: {String(getNodeId(edge.source_node))}</div>
          </div>
          <div>
            <span className="font-medium">To:</span> 
            <span className="ml-1 font-semibold text-blue-600">{String(getNodeLabelById(getNodeId(edge.target_node)))}</span>
            <div className="text-xs text-gray-500 ml-16">ID: {String(getNodeId(edge.target_node))}</div>
          </div>
        </div>
      </div>

      {/* Edge ID and Timestamps */}
      <div className="bg-gray-50 p-4 rounded-lg">
        <div className="flex items-center justify-between mb-2">
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
                Relationship Type
              </label>
              <input
                type="text"
                value={editedEdge.type || ''}
                onChange={(e) => setEditedEdge((prev: Partial<Edge>) => ({ ...prev, type: e.target.value }))}
                className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Confidence
              </label>
              <input
                type="number"
                min="0"
                max="1"
                step="0.1"
                value={editedEdge.confidence || ''}
                onChange={(e) => setEditedEdge((prev: Partial<Edge>) => ({ ...prev, confidence: parseFloat(e.target.value) }))}
                className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Importance
              </label>
              <input
                type="number"
                min="0"
                max="1"
                step="0.1"
                value={editedEdge.importance || ''}
                onChange={(e) => setEditedEdge((prev: Partial<Edge>) => ({ ...prev, importance: parseFloat(e.target.value) }))}
                className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Source
              </label>
              <input
                type="text"
                value={editedEdge.source || ''}
                onChange={(e) => setEditedEdge((prev: Partial<Edge>) => ({ ...prev, source: e.target.value }))}
                className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Sentence
              </label>
              <textarea
                value={editedEdge.sentence || ''}
                onChange={(e) => setEditedEdge((prev: Partial<Edge>) => ({ ...prev, sentence: e.target.value }))}
                rows={3}
                className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
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
              <span className="font-medium">ID:</span> {String(edge.id)}
            </div>
            <div>
              <span className="font-medium">Created:</span> {String(edge.created_at)}
            </div>
            <div>
              <span className="font-medium">Updated:</span> {String(edge.updated_at)}
            </div>
            {edge.confidence && (
              <div>
                <span className="font-medium">Confidence:</span> {String(edge.confidence)}
              </div>
            )}
            {edge.importance && (
              <div>
                <span className="font-medium">Importance:</span> {String(edge.importance)}
              </div>
            )}
            {edge.source && typeof edge.source === 'string' && (
              <div>
                <span className="font-medium">Source:</span> {String(edge.source)}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Additional Fields */}
      {(edge.sentence || edge.relationship_descriptor || edge.original_message_id || edge.sentence_id || edge.original_message_timestamp) && (
        <div className="bg-blue-50 p-4 rounded-lg">
          <h3 className="font-semibold text-blue-800 mb-2">Additional Info</h3>
          <div className="space-y-2 text-sm">
            {edge.sentence && (
              <div>
                <span className="font-medium">Sentence:</span> {String(edge.sentence)}
              </div>
            )}
            {edge.relationship_descriptor && (
              <div>
                <span className="font-medium">Relationship Descriptor:</span> {String(edge.relationship_descriptor)}
              </div>
            )}
            {edge.original_message_id && (
              <div>
                <span className="font-medium">Original Message ID:</span> {String(edge.original_message_id)}
              </div>
            )}
            {edge.sentence_id && (
              <div>
                <span className="font-medium">Sentence ID:</span> {String(edge.sentence_id)}
              </div>
            )}
            {edge.original_message_timestamp && (
              <div>
                <span className="font-medium">Message Timestamp:</span> {String(edge.original_message_timestamp)}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Actions */}
      <div className="flex space-x-2">
        <button
          onClick={handleDelete}
          className="px-4 py-2 text-sm font-medium text-white bg-red-600 border border-transparent rounded-md hover:bg-red-700 focus:outline-none focus:ring-2 focus:ring-red-500"
        >
          🗑️ Delete Edge
        </button>
      </div>
    </div>
  );
};

export default EdgePanel;
