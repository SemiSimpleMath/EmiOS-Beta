import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  fetchDefaultSeed,
  fetchFocusGraph,
  fetchSetGraph,
  parseChatQuery,
} from "./api";
import { Graph3D } from "./components/Graph3D";
import { ChatInput } from "./components/ChatInput";
import { ChatLog, ChatExchange } from "./components/ChatLog";
import { NodeSidebar } from "./components/NodeSidebar";
import { FocusGraphNode } from "./types";

// Two clicks on the same node within this window count as a double-click
// (refocus); a lone click selects the node and opens the sidebar.
const DOUBLE_CLICK_MS = 280;

// Importance filter bounds (node.importance is a 0..10 float).
const TAU_MIN = 0;
const TAU_MAX = 10;
const TAU_DEFAULT = 8.0;

// The backend intersects at most 3 foci.
const MAX_FOCI = 3;

// Chat log keeps only the most recent exchanges.
const MAX_EXCHANGES = 8;

// What the canvas shows: a focus neighborhood (1..3 foci, intersection
// when several) fetched from focus-graph, or an explicit agent-computed
// node set fetched from set-graph (the chat show_nodes intent).
type View =
  | { kind: "focus"; ids: string[] }
  | { kind: "set"; ids: string[] };

export default function App() {
  const [view, setView] = useState<View>({ kind: "focus", ids: [] });
  // τ — the importance threshold. Ctrl+wheel and the HUD slider both
  // drive it; the 3D view shows/hides in place (no re-layout, no refetch).
  const [tau, setTau] = useState<number>(TAU_DEFAULT);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);

  // First-time: ask backend for default focus (the user's own node).
  const { data: defaultSeed, refetch: refetchDefaultSeed } = useQuery({
    queryKey: ["default-seed"],
    queryFn: fetchDefaultSeed,
  });

  useEffect(() => {
    if (defaultSeed && view.kind === "focus" && view.ids.length === 0) {
      setView({ kind: "focus", ids: [defaultSeed] });
    }
  }, [defaultSeed, view]);

  // One fetch per view: the FULL display set with precomputed frozen
  // 3D positions. τ never appears in the query key — filtering is a
  // pure client-side show/hide.
  const { data, isLoading, error } = useQuery({
    queryKey: ["lens-graph", view.kind, view.ids.join(",")],
    queryFn: () =>
      view.kind === "set"
        ? fetchSetGraph({ nodeIds: view.ids, maxNodes: 400 })
        : fetchFocusGraph({ nodeIds: view.ids, maxNodes: 400 }),
    enabled: view.ids.length > 0,
  });

  const nodes = useMemo(() => data?.nodes ?? [], [data]);
  const edges = useMemo(() => data?.edges ?? [], [data]);

  // One chip per focus in the HUD (empty for explicit sets).
  const focusNodes = useMemo(() => nodes.filter((n) => n.is_focus), [nodes]);

  // HUD count: how many nodes the current τ reveals (focus + selected
  // are always shown — same predicate as Graph3D's nodeVisibility).
  const visibleCount = useMemo(
    () =>
      nodes.filter(
        (n) => n.is_focus || n.id === selectedNodeId || n.importance >= tau,
      ).length,
    [nodes, tau, selectedNodeId],
  );

  const handleTauDelta = useCallback((delta: number) => {
    setTau((t) => Math.min(TAU_MAX, Math.max(TAU_MIN, t + delta)));
  }, []);

  // ForceGraph3D has no native double-click event, so we detect it with a
  // short timer: the single-click action (select + sidebar) is deferred by
  // DOUBLE_CLICK_MS; a second click on the same node inside that window
  // cancels it and refocuses instead.
  const clickTimerRef = useRef<number | null>(null);
  const pendingClickNodeRef = useRef<string | null>(null);

  useEffect(() => {
    return () => {
      if (clickTimerRef.current !== null) {
        window.clearTimeout(clickTimerRef.current);
      }
    };
  }, []);

  function handleNodeClick(node: FocusGraphNode) {
    if (
      clickTimerRef.current !== null &&
      pendingClickNodeRef.current === node.id
    ) {
      // Double-click: make this node the sole focus (refetch + camera refit).
      window.clearTimeout(clickTimerRef.current);
      clickTimerRef.current = null;
      pendingClickNodeRef.current = null;
      setView({ kind: "focus", ids: [node.id] });
      return;
    }
    if (clickTimerRef.current !== null) {
      window.clearTimeout(clickTimerRef.current);
    }
    pendingClickNodeRef.current = node.id;
    clickTimerRef.current = window.setTimeout(() => {
      clickTimerRef.current = null;
      pendingClickNodeRef.current = null;
      // Single click: select the node and open its sidebar — no refocus.
      setSelectedNodeId(node.id);
    }, DOUBLE_CLICK_MS);
  }

  // Chat log: one entry per submitted query; reply stays null (rendered
  // as a "thinking…" indicator) until the parse-query request resolves.
  const [exchanges, setExchanges] = useState<ChatExchange[]>([]);
  const nextExchangeIdRef = useRef(0);

  function resolveExchange(id: number, reply: string, isError: boolean) {
    setExchanges((prev) =>
      prev.map((ex) => (ex.id === id ? { ...ex, reply, isError } : ex)),
    );
  }

  async function handleQuery(text: string) {
    const exchangeId = nextExchangeIdRef.current++;
    setExchanges((prev) =>
      [
        ...prev,
        { id: exchangeId, query: text, reply: null, isError: false },
      ].slice(-MAX_EXCHANGES),
    );
    try {
      const parsed = await parseChatQuery({
        text,
        currentSeeds: view.kind === "focus" ? view.ids : [],
        visibleNodes: nodes.map((n) => ({
          id: n.id,
          label: n.label,
          node_type: n.node_type,
        })),
      });
      // importance_min combines with ANY intent: drive the same τ state
      // the ctrl+wheel / HUD slider use.
      if (typeof parsed.importance_min === "number") {
        setTau(Math.min(TAU_MAX, Math.max(TAU_MIN, parsed.importance_min)));
      }
      let reply = parsed.message || "";
      switch (parsed.intent) {
        case "set_seeds":
          if (parsed.seed_node_ids.length > 0) {
            setView({
              kind: "focus",
              ids: parsed.seed_node_ids.slice(0, MAX_FOCI),
            });
          }
          break;
        case "add_seeds": {
          if (parsed.seed_node_ids.length === 0) break;
          // In focus view the new ids join the current foci (intersection);
          // an explicit set has no foci to join, so start a fresh focus.
          const merged =
            view.kind === "focus" ? [...view.ids] : ([] as string[]);
          for (const id of parsed.seed_node_ids) {
            if (!merged.includes(id)) merged.push(id);
          }
          setView({ kind: "focus", ids: merged.slice(0, MAX_FOCI) });
          break;
        }
        case "show_nodes": {
          // Agent-computed display set. Empty → the agent's reply in the
          // chat log is the whole response.
          const ids = parsed.node_ids ?? [];
          if (ids.length > 0) {
            setView({ kind: "set", ids });
          }
          break;
        }
        case "set_time_range":
        case "set_time_mode":
          reply =
            `${parsed.message || ""} (time filters don't apply to the 3D view yet)`.trim();
          break;
        case "reset": {
          const res = await refetchDefaultSeed();
          if (res.data) setView({ kind: "focus", ids: [res.data] });
          break;
        }
        case "noop":
          // Just show the message; no state mutation.
          break;
      }
      resolveExchange(exchangeId, reply, false);
    } catch (err) {
      resolveExchange(
        exchangeId,
        err instanceof Error ? err.message : "Parse error",
        true,
      );
    }
  }

  return (
    <div className="relative h-full w-full bg-white">
      {/* Frozen 3D graph, full viewport */}
      <Graph3D
        nodes={nodes}
        edges={edges}
        tau={tau}
        selectedNodeId={selectedNodeId}
        onTauDelta={handleTauDelta}
        onNodeClick={handleNodeClick}
        onBackgroundClick={() => setSelectedNodeId(null)}
      />

      {/* Importance HUD, top-right */}
      <div className="absolute top-4 right-4 z-10 w-64 bg-white/90 border border-gray-200 rounded-lg shadow-sm px-3 py-2.5 text-xs text-gray-700">
        {view.kind === "set" ? (
          <div className="mb-2">
            <span className="bg-accent text-accent-fg px-2.5 py-1 rounded-full font-medium">
              custom set ({nodes.length} nodes)
            </span>
          </div>
        ) : (
          focusNodes.length > 0 && (
            <div className="mb-2 flex flex-wrap gap-1.5">
              {focusNodes.map((n) => (
                <span
                  key={n.id}
                  className="bg-accent text-accent-fg px-2.5 py-1 rounded-full font-medium"
                >
                  {n.label}
                </span>
              ))}
            </div>
          )
        )}
        <div className="mb-1.5">
          importance ≥ {tau.toFixed(1)} — showing {visibleCount} /{" "}
          {nodes.length} nodes
        </div>
        <input
          type="range"
          min={TAU_MIN}
          max={TAU_MAX}
          step={0.5}
          value={tau}
          onChange={(e) => setTau(Number(e.target.value))}
          className="w-full accent-blue-600"
        />
        <div className="mt-1 text-gray-400">ctrl+scroll to reveal more</div>
      </div>

      {/* Loading / error state */}
      {isLoading && (
        <div className="absolute top-4 left-4 text-sm text-gray-500">
          Loading…
        </div>
      )}
      {error && (
        <div className="absolute top-4 left-4 text-sm text-red-600">
          {(error as Error).message}
        </div>
      )}

      {/* Node detail side panel */}
      {selectedNodeId && (
        <NodeSidebar
          nodeId={selectedNodeId}
          onClose={() => setSelectedNodeId(null)}
          onSelectNode={(id) => setSelectedNodeId(id)}
        />
      )}

      {/* Chat log — query/reply feed above the chat input */}
      <ChatLog exchanges={exchanges} />

      {/* Bottom chat input */}
      <ChatInput onSubmit={handleQuery} />
    </div>
  );
}
