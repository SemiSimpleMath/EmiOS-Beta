import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchDefaultSeed, fetchSeedGraph, parseChatQuery } from "./api";
import { GraphCanvas } from "./components/GraphCanvas";
import { SeedChips } from "./components/SeedChips";
import { ChatInput } from "./components/ChatInput";
import { WikiPanel } from "./components/WikiPanel";
import { LensNode } from "./types";

export default function App() {
  const [seeds, setSeeds] = useState<string[]>([]);
  const [timeMode, setTimeMode] = useState<"current" | "lifetime" | "range">(
    "current",
  );
  const [timeRange, setTimeRange] = useState<{ from?: string; to?: string }>({});
  const [openWikiNode, setOpenWikiNode] = useState<LensNode | null>(null);

  // First-time: ask backend for default seed (the user's own node).
  const { data: defaultSeed } = useQuery({
    queryKey: ["default-seed"],
    queryFn: fetchDefaultSeed,
  });

  useEffect(() => {
    if (defaultSeed && seeds.length === 0) {
      setSeeds([defaultSeed]);
    }
  }, [defaultSeed, seeds.length]);

  const { data, isLoading, error } = useQuery({
    queryKey: ["seed-graph", seeds, timeMode, timeRange.from, timeRange.to],
    queryFn: () =>
      fetchSeedGraph({
        seeds,
        limit: 50,
        timeMode,
        timeFrom: timeRange.from,
        timeTo: timeRange.to,
      }),
    enabled: seeds.length > 0,
  });

  const nodes = data?.nodes ?? [];
  const edges = data?.edges ?? [];
  const bridgeEdges = data?.bridge_edges ?? [];

  const seedNodeMap = useMemo(() => {
    const m = new Map<string, LensNode>();
    for (const n of nodes) m.set(n.id, n);
    return m;
  }, [nodes]);

  function handleNodeClick(node: LensNode, event: MouseEvent) {
    if (event.shiftKey) {
      // Shift-click: add to seed set (or remove if already a seed).
      setSeeds((prev) =>
        prev.includes(node.id)
          ? prev.filter((s) => s !== node.id)
          : [...prev, node.id],
      );
      return;
    }
    // Plain click: open the wiki side panel.
    setOpenWikiNode(node);
  }

  function handleRemoveSeed(id: string) {
    setSeeds((prev) => prev.filter((s) => s !== id));
  }

  const [lastFilterMessage, setLastFilterMessage] = useState<string>("");

  async function handleQuery(text: string) {
    try {
      const parsed = await parseChatQuery({
        text,
        currentSeeds: seeds,
        visibleNodes: nodes.map((n) => ({
          id: n.id,
          label: n.label,
          node_type: n.node_type,
        })),
      });
      setLastFilterMessage(parsed.message || "");
      switch (parsed.intent) {
        case "set_seeds":
          if (parsed.seed_node_ids.length > 0) setSeeds(parsed.seed_node_ids);
          break;
        case "add_seeds":
          if (parsed.seed_node_ids.length > 0) {
            setSeeds((prev) =>
              Array.from(new Set([...prev, ...parsed.seed_node_ids])),
            );
          }
          break;
        case "set_time_range":
          setTimeMode("range");
          setTimeRange({
            from: parsed.time_from || undefined,
            to: parsed.time_to || undefined,
          });
          break;
        case "set_time_mode":
          if (parsed.time_mode === "current" || parsed.time_mode === "lifetime") {
            setTimeMode(parsed.time_mode);
            setTimeRange({});
          }
          break;
        case "reset":
          if (defaultSeed) setSeeds([defaultSeed]);
          setTimeMode("current");
          setTimeRange({});
          break;
        case "noop":
          // Just show the message; no state mutation.
          break;
      }
    } catch (err) {
      setLastFilterMessage(
        err instanceof Error ? err.message : "Parse error",
      );
    }
  }

  return (
    <div className="relative h-full w-full bg-white">
      {/* Seed chips, top-left */}
      <SeedChips
        seeds={seeds}
        seedNodeMap={seedNodeMap}
        onRemove={handleRemoveSeed}
      />

      {/* Graph canvas, full viewport */}
      <GraphCanvas
        nodes={nodes}
        edges={edges}
        bridgeEdges={bridgeEdges}
        onNodeClick={handleNodeClick}
      />

      {/* Loading / error state */}
      {isLoading && (
        <div className="absolute top-4 right-4 text-sm text-gray-500">
          Loading…
        </div>
      )}
      {error && (
        <div className="absolute top-4 right-4 text-sm text-red-600">
          {(error as Error).message}
        </div>
      )}

      {/* Wiki side panel */}
      {openWikiNode && (
        <WikiPanel
          node={openWikiNode}
          onClose={() => setOpenWikiNode(null)}
          onAddToFocus={(id) => {
            setSeeds((prev) =>
              prev.includes(id) ? prev : [...prev, id],
            );
            setOpenWikiNode(null);
          }}
        />
      )}

      {/* Last filter message — small toast above chat input */}
      {lastFilterMessage && (
        <div className="absolute bottom-20 left-1/2 -translate-x-1/2 text-xs text-gray-600 bg-gray-50 border border-gray-200 rounded-full px-3 py-1.5 shadow-sm z-10">
          {lastFilterMessage}
        </div>
      )}

      {/* Bottom chat input */}
      <ChatInput onSubmit={handleQuery} />
    </div>
  );
}
