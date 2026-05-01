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
  pagerank_score: number;
  is_seed: boolean;
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
