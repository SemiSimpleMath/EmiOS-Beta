import { NodeDetail, SeedGraphResponse } from "./types";

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

export async function fetchNodeDetail(id: string): Promise<NodeDetail> {
  const res = await fetch(`${API_BASE}/api/me/node/${encodeURIComponent(id)}`);
  if (!res.ok) throw new Error(`node detail failed: ${res.status}`);
  return res.json();
}

export interface ParsedQueryResponse {
  intent:
    | "set_seeds"
    | "add_seeds"
    | "set_time_range"
    | "set_time_mode"
    | "reset"
    | "noop";
  seed_node_ids: string[];
  time_mode?: "current" | "lifetime" | null;
  time_from?: string | null;
  time_to?: string | null;
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
