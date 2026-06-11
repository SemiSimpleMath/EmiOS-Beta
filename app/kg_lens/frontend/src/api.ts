import {
  EditableNodeField,
  FocusGraphPayload,
  KgNodeDetail,
  SeedGraphResponse,
} from "./types";

const API_BASE = ""; // proxied in dev, same-origin in prod

export async function fetchDemoGraph(opts: {
  seed?: string;
  n?: number;
}): Promise<SeedGraphResponse> {
  const params = new URLSearchParams();
  if (opts.seed) params.set("seed", opts.seed);
  if (opts.n) params.set("n", String(opts.n));
  const res = await fetch(`${API_BASE}/api/me/demo-graph?${params}`);
  if (!res.ok) throw new Error(`demo-graph failed: ${res.status}`);
  return res.json();
}

export async function fetchSeedGraph(opts: {
  seeds: string[];
  limit?: number;
  timeMode?: "current" | "lifetime" | "range";
  timeFrom?: string;
  timeTo?: string;
  categories?: string[];
}): Promise<SeedGraphResponse> {
  const params = new URLSearchParams();
  if (opts.seeds.length > 0) params.set("seeds", opts.seeds.join(","));
  if (opts.limit) params.set("limit", String(opts.limit));
  if (opts.timeMode) params.set("time_mode", opts.timeMode);
  if (opts.timeFrom) params.set("time_from", opts.timeFrom);
  if (opts.timeTo) params.set("time_to", opts.timeTo);
  // No category filter by default — backend returns persons, states,
  // and minor entities together, each with an lod_tier so the frontend
  // can show/hide based on zoom.
  if (opts.categories && opts.categories.length > 0) {
    params.set("categories", opts.categories.join(","));
  }

  const res = await fetch(`${API_BASE}/api/me/seed-graph?${params}`);
  if (!res.ok) throw new Error(`seed-graph failed: ${res.status}`);
  return res.json();
}

export async function fetchDefaultSeed(): Promise<string> {
  const res = await fetch(`${API_BASE}/api/me/default-seed`);
  if (!res.ok) throw new Error(`default-seed failed: ${res.status}`);
  const data = await res.json();
  return data.node_id as string;
}

// Full neighborhood of 1..3 focus nodes with frozen 3D positions.
// Several ids → INTERSECTION (only nodes attached to every focus).
// Empty nodeIds → backend defaults to the user's own node.
export async function fetchFocusGraph(opts: {
  nodeIds: string[];
  maxNodes?: number;
}): Promise<FocusGraphPayload> {
  const params = new URLSearchParams();
  if (opts.nodeIds.length > 0) params.set("node_ids", opts.nodeIds.join(","));
  if (opts.maxNodes) params.set("max_nodes", String(opts.maxNodes));
  const res = await fetch(`${API_BASE}/api/kg-lens/focus-graph?${params}`);
  if (!res.ok) throw new Error(`focus-graph failed: ${res.status}`);
  return res.json();
}

// Frozen layout for an explicit agent-computed node set (the chat
// show_nodes intent). Same payload shape, mode='explicit_set', no foci.
export async function fetchSetGraph(opts: {
  nodeIds: string[];
  maxNodes?: number;
}): Promise<FocusGraphPayload> {
  const body: Record<string, unknown> = { node_ids: opts.nodeIds };
  if (opts.maxNodes) body.max_nodes = opts.maxNodes;
  const res = await fetch(`${API_BASE}/api/kg-lens/set-graph`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`set-graph failed: ${res.status}`);
  return res.json();
}

export async function fetchKgNodeDetail(id: string): Promise<KgNodeDetail> {
  const res = await fetch(
    `${API_BASE}/api/kg-lens/node/${encodeURIComponent(id)}`,
  );
  if (!res.ok) throw new Error(`node detail failed: ${res.status}`);
  return res.json();
}

interface MutationResponse {
  ok: boolean;
  detail?: string;
  error?: string;
}

// POST helper for the kg-lens mutation endpoints. The backend answers
// {ok: true, detail} on success and {ok: false, error} on refusal (HTTP 400,
// still JSON) — surface the refusal text as the thrown Error's message so
// callers can show it inline.
async function postKgLensMutation(
  path: string,
  body: Record<string, unknown>,
): Promise<string> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data: MutationResponse | null = null;
  try {
    data = (await res.json()) as MutationResponse;
  } catch {
    // Non-JSON body — report the HTTP status below.
  }
  if (!data || data.ok !== true) {
    throw new Error(data?.error || `request failed: ${res.status}`);
  }
  return data.detail ?? "";
}

export async function editKgNodeField(opts: {
  id: string;
  field: EditableNodeField;
  value: string | number | null;
  reason: string;
}): Promise<string> {
  return postKgLensMutation(
    `/api/kg-lens/node/${encodeURIComponent(opts.id)}/edit`,
    { field: opts.field, value: opts.value, reason: opts.reason },
  );
}

export async function setKgNodeLock(opts: {
  id: string;
  locked: boolean;
  reason: string;
}): Promise<string> {
  return postKgLensMutation(
    `/api/kg-lens/node/${encodeURIComponent(opts.id)}/lock`,
    { locked: opts.locked, reason: opts.reason },
  );
}

export interface ParsedQueryResponse {
  intent:
    | "set_seeds"
    | "add_seeds"
    | "show_nodes"
    | "set_time_range"
    | "set_time_mode"
    | "reset"
    | "noop";
  seed_node_ids: string[];
  // Explicit display set for show_nodes. Absent on the deterministic
  // shortcut responses (reset / time-mode), hence optional.
  node_ids?: string[];
  time_mode?: "current" | "lifetime" | null;
  time_from?: string | null;
  time_to?: string | null;
  // Importance threshold (0..10) combinable with any intent; when set,
  // the frontend drives the τ filter to this value.
  importance_min?: number | null;
  message: string;
}

export async function parseChatQuery(opts: {
  text: string;
  currentSeeds: string[];
  visibleNodes: { id: string; label: string; node_type: string }[];
}): Promise<ParsedQueryResponse> {
  const res = await fetch(`${API_BASE}/api/me/parse-query`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      text: opts.text,
      current_seeds: opts.currentSeeds,
      visible_nodes: opts.visibleNodes,
    }),
  });
  if (!res.ok) throw new Error(`parse-query failed: ${res.status}`);
  return res.json();
}
