import React from 'react';

interface Edge {
  id: string;
  source_node: string;
  target_node: string;
  type: string;
  attributes: any;
  sentence?: string;
  original_message_id?: string;
  sentence_id?: string;
  relationship_descriptor?: string;
  original_message_timestamp?: string;
  created_at: string;
  updated_at: string;
  confidence?: number;
  importance?: number;
  source?: string;
  source_label?: string;
  target_label?: string;
}

interface EdgeDetailsProps {
  selectedEdge: Edge;
  getNodeLabelById: (idOrNode: string | any) => string;
  getNodeId: (idOrNode: string | any) => string;
}

const EdgeDetails: React.FC<EdgeDetailsProps> = ({ 
  selectedEdge, 
  getNodeLabelById, 
  getNodeId 
}) => {
  return (
    <div className="space-y-4">
      {/* Edge Basic Info */}
      <div className="bg-orange-50 p-4 rounded-lg">
        <h3 className="font-semibold text-orange-800 mb-2">Relationship</h3>
        <div className="space-y-2 text-sm">
          <div className="flex items-center">
            <span className="font-medium">Type:</span>
            <span className="ml-2 px-2 py-1 bg-orange-200 text-orange-800 rounded text-xs">
              {String(selectedEdge.type)}
            </span>
          </div>
          <div>
            <span className="font-medium">From:</span> {String(getNodeLabelById(getNodeId(selectedEdge.source_node)))}
          </div>
          <div>
            <span className="font-medium">To:</span> {String(getNodeLabelById(getNodeId(selectedEdge.target_node)))}
          </div>
        </div>
      </div>

      {/* Edge ID and Timestamps */}
      <div className="bg-gray-50 p-4 rounded-lg">
        <h3 className="font-semibold text-gray-800 mb-2">Details</h3>
        <div className="space-y-2 text-sm">
          <div>
            <span className="font-medium">ID:</span> {String(selectedEdge.id)}
          </div>
          <div>
            <span className="font-medium">Created:</span> {String(selectedEdge.created_at)}
          </div>
          <div>
            <span className="font-medium">Updated:</span> {String(selectedEdge.updated_at)}
          </div>
          {selectedEdge.confidence && (
            <div>
              <span className="font-medium">Confidence:</span> {String(selectedEdge.confidence)}
            </div>
          )}
          {selectedEdge.importance && (
            <div>
              <span className="font-medium">Importance:</span> {String(selectedEdge.importance)}
            </div>
          )}
          {selectedEdge.source && (
            <div>
              <span className="font-medium">Source:</span> {typeof selectedEdge.source === 'object' ? JSON.stringify(selectedEdge.source) : String(selectedEdge.source)}
            </div>
          )}
        </div>
      </div>

      {/* Additional Fields */}
      {(selectedEdge.sentence || selectedEdge.relationship_descriptor || selectedEdge.original_message_id || selectedEdge.sentence_id || selectedEdge.original_message_timestamp) && (
        <div className="bg-blue-50 p-4 rounded-lg">
          <h3 className="font-semibold text-blue-800 mb-2">Additional Info</h3>
          <div className="space-y-2 text-sm">
            {selectedEdge.sentence && (
              <div>
                <span className="font-medium">Sentence:</span> {String(selectedEdge.sentence)}
              </div>
            )}
            {selectedEdge.relationship_descriptor && (
              <div>
                <span className="font-medium">Relationship Descriptor:</span> {String(selectedEdge.relationship_descriptor)}
              </div>
            )}
            {selectedEdge.original_message_id && (
              <div>
                <span className="font-medium">Original Message ID:</span> {String(selectedEdge.original_message_id)}
              </div>
            )}
            {selectedEdge.sentence_id && (
              <div>
                <span className="font-medium">Sentence ID:</span> {String(selectedEdge.sentence_id)}
              </div>
            )}
            {selectedEdge.original_message_timestamp && (
              <div>
                <span className="font-medium">Message Timestamp:</span> {String(selectedEdge.original_message_timestamp)}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export default EdgeDetails;
