/**
 * Serialize the current ReactFlow graph state back into a task IR object.
 *
 * The IR is the source of truth. We reconstruct `steps` from the node data
 * plus the current edge topology, then patch next_step / on_true / on_false
 * to reflect any user edits to connections.
 */
export function serializeIR(originalIR, nodes, edges) {
  const steps = nodes.map((node) => {
    const step = { ...(node.data?.step ?? {}) };

    // Clear topology fields — we'll rebuild from edges
    delete step.next_step;
    delete step.on_true;
    delete step.on_false;

    return step;
  });

  const stepById = Object.fromEntries(steps.map((s) => [s.id, s]));

  for (const edge of edges) {
    const src = stepById[edge.source];
    if (!src) continue;
    const targetId = edge.target;

    if (src.kind === "decision") {
      const handle = edge.sourceHandle;
      if (handle === "true") src.on_true = targetId;
      else if (handle === "false") src.on_false = targetId;
      else src.on_true = targetId; // fallback
    } else {
      src.next_step = targetId;
    }
  }

  // Determine entry_step_id: prefer the original, fall back to first node
  const entry =
    originalIR?.entry_step_id ??
    (steps.length > 0 ? steps[0].id : "");

  return {
    ...(originalIR ?? {}),
    entry_step_id: entry,
    steps,
  };
}

/**
 * Generate a unique step ID that doesn't collide with existing step IDs.
 */
export function generateStepId(kind, existingIds) {
  const prefix = kindPrefix(kind);
  let i = 1;
  while (existingIds.has(`${prefix}_${i}`)) i++;
  return `${prefix}_${i}`;
}

function kindPrefix(kind) {
  switch (kind) {
    case "action": return "step";
    case "wait_for_event": return "wait";
    case "wait_gate": return "gate";
    case "decision": return "decision";
    case "end": return "end";
    default: return "step";
  }
}

/**
 * Create a blank step object for a given kind with sensible defaults.
 */
export function blankStep(kind, id) {
  const base = { id, kind, title: id };
  switch (kind) {
    case "action":
      return { ...base, executor: "personal_admin_manager", instruction: "", consumes_data_ids: [], produces_data_ids: [] };
    case "wait_for_event":
      return { ...base, event_name: "", clear_condition: "" };
    case "wait_gate":
      return { ...base, subscriptions: [], release_condition: "" };
    case "decision":
      return { ...base, condition: "" };
    case "end":
      return { ...base };
    default:
      return base;
  }
}

/**
 * Check if adding an edge from source→target would create a cycle.
 * Uses iterative DFS from target; if we can reach source, it's a cycle.
 */
export function wouldCreateCycle(nodes, edges, sourceId, targetId) {
  if (sourceId === targetId) return true;
  const adj = {};
  for (const n of nodes) adj[n.id] = [];
  for (const e of edges) {
    if (!adj[e.source]) adj[e.source] = [];
    adj[e.source].push(e.target);
  }
  // DFS from targetId — if we reach sourceId, it's a cycle
  const visited = new Set();
  const stack = [targetId];
  while (stack.length > 0) {
    const cur = stack.pop();
    if (cur === sourceId) return true;
    if (visited.has(cur)) continue;
    visited.add(cur);
    for (const next of (adj[cur] ?? [])) stack.push(next);
  }
  return false;
}
