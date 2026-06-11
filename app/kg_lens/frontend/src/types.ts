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

// ---------- KG Lens focus graph (GET /api/kg-lens/focus-graph) ----------
// Full neighborhood around a focus node with precomputed FROZEN 3D
// positions — the frontend never re-layouts; it only shows/hides.

export interface FocusGraphNode {
  id: string;
  label: string;
  node_type: NodeType;
  category: string | null;
  // Hybrid importance (0-10 float) used by the τ show/hide filter.
  importance: number;
  node_importance: number;
  is_focus: boolean;
  aliases: string[];
  goal_status: string | null;
  // Server-computed frozen layout position.
  x: number;
  y: number;
  z: number;
}

export interface FocusGraphEdge {
  id: string;
  source_id: string;
  target_id: string;
  relationship_type: string;
  sentence: string;
  importance: number;
  confidence: number;
}

// How the displayed set was computed: neighborhood union of the foci,
// intersection (nodes attached to EVERY focus), or an explicit
// agent-computed node set (no foci at all).
export type FocusGraphMode = "union" | "intersection" | "explicit_set";

export interface FocusGraphPayload {
  // First focus id; null for explicit sets. focus_ids is canonical.
  focus_id: string | null;
  focus_ids: string[];
  mode: FocusGraphMode;
  total_candidates: number;
  dropped_by_cap: number;
  nodes: FocusGraphNode[];
  edges: FocusGraphEdge[];
}

// ---------- KG Lens node detail (GET /api/kg-lens/node/<id>) ----------

export type DateConfidence = "estimated" | "actual" | "user_set" | "inferred" | null;

export interface KgEdgeRef {
  edge_id: string;
  rel: string;
  sentence: string;
  other_id: string;
  other_label: string;
  other_type: NodeType;
}

export interface KgRevision {
  op: string;
  reason: string;
  created_at: string;
  agent_id: string;
}

export interface KgNodeDetail {
  id: string;
  label: string;
  node_type: NodeType;
  category: string | null;
  semantic_label: string | null;
  aliases: string[];
  description: string | null;
  original_sentence: string | null;
  start_date: string | null;
  end_date: string | null;
  start_date_prose: string | null;
  end_date_prose: string | null;
  start_date_confidence: DateConfidence;
  end_date_confidence: DateConfidence;
  // Honest-precision display strings: "2003", "2024-08", "2003-09-03", or null.
  display_start: string | null;
  display_end: string | null;
  valid_during: string | null;
  valid_currently: boolean | null;
  goal_status: string | null;
  last_pursued_at: string | null;
  importance: number | null;
  confidence: number | null;
  confidence_tier: "axiom" | "provisional" | null;
  locked: boolean;
  locked_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  wiki_url: string | null;
  evidence_count: number;
  edges_out: KgEdgeRef[];
  edges_in: KgEdgeRef[];
  // Newest first, max 10.
  revisions: KgRevision[];
}

// Fields the edit endpoint accepts.
export type EditableNodeField =
  | "label"
  | "original_sentence"
  | "description"
  | "category"
  | "semantic_label"
  | "valid_during"
  | "start_date"
  | "end_date"
  | "start_date_prose"
  | "end_date_prose"
  | "importance"
  | "goal_status";
