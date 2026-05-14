export interface Node {
  id: string;
  label: string;
  type: string;
  category?: string;
  aliases?: string[];
  description?: string;
  original_sentence?: string;
  attributes: any;
  start_date: string | null;
  end_date: string | null;
  start_date_confidence?: string | null;
  end_date_confidence?: string | null;
  valid_during?: string | null;
  hash_tags?: string[];
  semantic_label?: string | null;
  goal_status?: string | null;
  confidence?: number;
  importance?: number;
  source?: string;
  taxonomy_paths?: string[];
  created_at: string;
  updated_at: string;
  // Pre-computed degree from /api/graph/hubs (optional — not present on subgraph nodes)
  degree?: number;
  // ForceGraph positioning — 2D and 3D
  x?: number;
  y?: number;
  z?: number;
  vx?: number;
  vy?: number;
  vz?: number;
}

export interface Edge {
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

export interface GraphData {
  nodes: Node[];
  edges: Edge[];
  timestamp: string;
}

export interface NodeDetailsData {
  node: Node;
  incoming_edges: Edge[];
  outgoing_edges: Edge[];
}

export interface GraphStats {
  totalNodes: number;
  totalEdges: number;
  nodeTypes: { [key: string]: number };
  edgeTypes: { [key: string]: number };
  avgNodeDegree: number;
  density: number;
}

export interface ContextMenuState {
  isOpen: boolean;
  x: number;
  y: number;
  node: { id: string; label: string } | null;
}

export interface SearchFilters {
  searchQuery: string;
  nodeTypeFilter: string;
  edgeTypeFilter: string;
  taxonomyKeyword: string;
  taxonomyPath: string;
}

export type ViewMode = '2d' | '3d';

export interface ExplorationState {
  /** All node IDs currently in the visible set */
  visibleNodeIds: Set<string>;
  /** Whether we are in hub mode (importance threshold) or free exploration */
  mode: 'hubs' | 'exploration';
  importanceThreshold: number;
}
