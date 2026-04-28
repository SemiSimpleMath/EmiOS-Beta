import { Node } from '../types/graph';

export const nodeColors: { [key: string]: string } = {
  // Node classifications (primary)
  'entity_node': '#4299e1',      // Blue for entities
  'property_node': '#9f7aea',    // Purple for properties
  'event_node': '#f56565',       // Bright red for events
  'goal_node': '#ed8936',        // Orange for goals
  'state_node': '#48bb78',       // Bright green for states
  
  // Specific node types from our KG pipeline
  'Entity': '#4299e1',           // Blue for entities
  'Event': '#f56565',            // Bright red for events
  'EventNode': '#f56565',        // Bright red for events
  'Property': '#9f7aea',         // Purple for properties
  'PropertyNode': '#9f7aea',     // Purple for properties
  'Concept': '#805ad5',          // Purple for concepts
  'State': '#48bb78',            // Bright green for states
  'StateNode': '#48bb78',        // Bright green for states
  'Goal': '#ed8936',             // Orange for goals
  'GoalNode': '#ed8936',         // Orange for goals
  
  // Common entity types
  'person': '#4299e1',
  'organization': '#48bb78',
  'place': '#ed8936',
  'location': '#38a169',
  'event': '#e53e3e',
  'concept': '#805ad5',
  
  // Default fallback
  'default': '#718096'           // Gray for unknown types
};

export const getNodeColorByClassification = (node: Node): string => {
  // Use node_type for coloring
  if (node.type && nodeColors[node.type]) {
    return nodeColors[node.type];
  }
  
  return nodeColors['default'];
};