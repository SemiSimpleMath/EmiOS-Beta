export type NodeType =
  | "Entity"
  | "State"
  | "Event"
  | "Goal"
  | "Concept"
  | string;

export interface LensNode {
  id: string;
  label: string;
  node_type: NodeType;
  category: string | null;
  aliases: string[];
  description: string;
  start_date: string | null;
  end_date: string | null;
  importance: number;
  // LLM-rated importance (0-10). Defaults to 5.0 for unrated nodes.
  // Used for visual sizing — bigger box = more important.
  llm_importance: number;
  pagerank_score: number;
  is_seed: boolean;
  is_anchor: boolean;
  primary_anchor_id: string | null;
  // LOD tier for zoom-based visibility:
  //   0 = always (persons, seeds)
  //   1 = mid-zoom (states, events, goals — connective tissue)
  //   2 = close-zoom only (everything else)
  lod_tier: number;
  // Global stable map position.
  x: number;
  y: number;
}

export interface LensBridgeEdge {
  id: string;
  source_id: string;
  target_id: string;
  via_node_id: string;
  label: string;
}

export interface LensEdge {
  id: string;
  source_id: string;
  target_id: string;
  relationship_type: string;
  sentence: string;
  importance: number;
  confidence: number;
}

export interface SeedGraphResponse {
  nodes: LensNode[];
  edges: LensEdge[];
  bridge_edges: LensBridgeEdge[];
  seeds: string[];
  time_mode: "current" | "lifetime" | "range";
  time_from: string | null;
  time_to: string | null;
  total_candidates: number;
  timestamp: string;
}

export interface NodeDetail {
  id: string;
  label: string;
  node_type: NodeType;
  category: string | null;
  aliases: string[];
  description: string;
  original_sentence: string;
  start_date: string | null;
  end_date: string | null;
  importance: number;
  confidence: number;
  edges_in: number;
  edges_out: number;
  wiki_url: string | null;
}
