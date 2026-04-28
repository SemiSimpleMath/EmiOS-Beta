import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  addEdge,
  BackgroundVariant,
  useReactFlow,
  ReactFlowProvider,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import TaskNode from "./components/TaskNode.jsx";
import NodePalette from "./components/NodePalette.jsx";
import NodeEditorModal from "./components/NodeEditorModal.jsx";
import Toolbar from "./components/Toolbar.jsx";
import StepDetailPanel from "./components/StepDetailPanel.jsx";
import { buildGraphFromIR, applyDagreLayout, makeEdge, buildEdges } from "./utils/buildGraph.js";
import { serializeIR, generateStepId, blankStep, wouldCreateCycle } from "./utils/irUtils.js";

const nodeTypes = { taskNode: TaskNode };

// Max undo history depth
const HISTORY_LIMIT = 40;

export default function AppWrapper() {
  return (
    <ReactFlowProvider>
      <App />
    </ReactFlowProvider>
  );
}

function App() {
  const { screenToFlowPosition } = useReactFlow();

  const [ir, setIr] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);

  // Editor state
  const [editingStep, setEditingStep] = useState(null);
  const [detailStep, setDetailStep]   = useState(null);
  const [saveState, setSaveState]     = useState("idle"); // idle | saving | saved | error
  const [isDirty, setIsDirty]         = useState(false);

  // Undo history: array of { nodes, edges } snapshots
  const historyRef = useRef([]);
  const snapshotRef = useRef(null); // latest committed snapshot

  const taskId = useMemo(() => {
    const p = new URLSearchParams(window.location.search);
    return p.get("task_id") ?? null;
  }, []);

  // ─── Load IR ──────────────────────────────────────────────────────────────

  useEffect(() => {
    if (!taskId) {
      setError("No task_id provided. Open this page with ?task_id=<your_task_id>");
      setLoading(false);
      return;
    }
    fetch(`/task-graph/ir/${encodeURIComponent(taskId)}`)
      .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then((data) => {
        setIr(data);
        const { nodes: n, edges: e } = buildGraphFromIR(data);
        setNodes(n);
        setEdges(e);
        setLoading(false);
      })
      .catch((e) => { setError(e.message); setLoading(false); });
  }, [taskId, setNodes, setEdges]);

  // ─── Inject callbacks into node data ──────────────────────────────────────

  // We store callbacks in a ref so they're stable and don't re-create nodes
  const onNodeDoubleClickCb = useCallback((step) => setEditingStep(step), []);
  const onBindingDropCb = useCallback((stepId, binding) => {
    pushHistory();
    setNodes((ns) =>
      ns.map((n) => {
        if (n.id !== stepId) return n;
        const step = { ...n.data.step };
        const role = binding.data_kind === "artifact" ? "produces" : "consumes";
        const key = role === "consumes" ? "consumes_data_ids" : "produces_data_ids";
        const existing = Array.isArray(step[key]) ? step[key] : [];
        if (!existing.includes(binding.data_id)) {
          step[key] = [...existing, binding.data_id];
        }
        // Also track a _bindings display annotation
        const bindings = [...(step._bindings ?? [])];
        if (!bindings.find((b) => b.data_id === binding.data_id && b.role === role)) {
          bindings.push({ data_id: binding.data_id, role });
        }
        step._bindings = bindings;
        return { ...n, data: { ...n.data, step, onDoubleClick: onNodeDoubleClickCb, onBindingDrop: onBindingDropCb } };
      })
    );
    markDirty();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Re-inject callbacks whenever nodes are set from IR load
  useEffect(() => {
    setNodes((ns) =>
      ns.map((n) => ({
        ...n,
        data: {
          ...n.data,
          onDoubleClick: onNodeDoubleClickCb,
          onBindingDrop: onBindingDropCb,
        },
      }))
    );
  }, [loading, onNodeDoubleClickCb, onBindingDropCb, setNodes]);

  // ─── Undo ─────────────────────────────────────────────────────────────────

  function pushHistory() {
    setNodes((ns) => {
      setEdges((es) => {
        historyRef.current = [
          ...historyRef.current.slice(-HISTORY_LIMIT + 1),
          { nodes: ns, edges: es },
        ];
        return es;
      });
      return ns;
    });
  }

  function undo() {
    if (historyRef.current.length === 0) return;
    const prev = historyRef.current[historyRef.current.length - 1];
    historyRef.current = historyRef.current.slice(0, -1);
    setNodes(prev.nodes);
    setEdges(prev.edges);
    markDirty();
  }

  function markDirty() { setIsDirty(true); }

  // ─── Keyboard shortcuts ───────────────────────────────────────────────────

  useEffect(() => {
    function onKeyDown(e) {
      if ((e.ctrlKey || e.metaKey) && e.key === "z" && !editingStep) {
        e.preventDefault();
        undo();
      }
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        handleSave();
      }
      if (e.key === "Escape") {
        setEditingStep(null);
        setDetailStep(null);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [editingStep]); // eslint-disable-line react-hooks/exhaustive-deps

  // ─── Connection validation ────────────────────────────────────────────────

  const isValidConnection = useCallback(
    (connection) => {
      const { source, target } = connection;
      if (source === target) return false;
      // Prevent cycles
      if (wouldCreateCycle(nodes, edges, source, target)) return false;
      // End nodes can't have outgoing edges
      const srcNode = nodes.find((n) => n.id === source);
      if (srcNode?.data?.step?.kind === "end") return false;
      return true;
    },
    [nodes, edges]
  );

  // ─── Edge connect ─────────────────────────────────────────────────────────

  const onConnect = useCallback(
    (connection) => {
      pushHistory();
      const srcStep = nodes.find((n) => n.id === connection.source)?.data?.step;
      const variant =
        srcStep?.kind === "decision"
          ? connection.sourceHandle === "true" ? "true" : "false"
          : "default";
      const label =
        variant === "true" ? "✓ true" : variant === "false" ? "✗ false" : "";

      const newEdge = makeEdge(
        connection.source,
        connection.target,
        variant,
        connection.sourceHandle,
        label
      );
      setEdges((es) => addEdge(newEdge, es));
      markDirty();
    },
    [nodes, setEdges] // eslint-disable-line react-hooks/exhaustive-deps
  );

  // ─── Node deletion ────────────────────────────────────────────────────────

  const onNodesDelete = useCallback(
    (deleted) => {
      pushHistory();
      const deletedIds = new Set(deleted.map((n) => n.id));
      // Remove dangling edges
      setEdges((es) => es.filter((e) => !deletedIds.has(e.source) && !deletedIds.has(e.target)));
      markDirty();
    },
    [setEdges] // eslint-disable-line react-hooks/exhaustive-deps
  );

  // ─── Drag-drop from palette ───────────────────────────────────────────────

  const onDragOver = useCallback((e) => {
    if (e.dataTransfer.types.includes("application/task-node-kind")) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
    }
  }, []);

  const onDrop = useCallback(
    (e) => {
      e.preventDefault();
      const kind = e.dataTransfer.getData("application/task-node-kind");
      if (!kind) return;

      const position = screenToFlowPosition({ x: e.clientX, y: e.clientY });
      const existingIds = new Set(nodes.map((n) => n.id));
      const id = generateStepId(kind, existingIds);
      const step = blankStep(kind, id);

      pushHistory();
      setNodes((ns) => [
        ...ns,
        {
          id,
          type: "taskNode",
          position,
          data: {
            step,
            onDoubleClick: onNodeDoubleClickCb,
            onBindingDrop: onBindingDropCb,
          },
        },
      ]);
      markDirty();
    },
    [nodes, screenToFlowPosition, setNodes, onNodeDoubleClickCb, onBindingDropCb] // eslint-disable-line react-hooks/exhaustive-deps
  );

  // ─── Node editor save / delete ────────────────────────────────────────────

  function handleStepSave(updatedStep) {
    pushHistory();
    setNodes((ns) =>
      ns.map((n) =>
        n.id === updatedStep.id
          ? {
              ...n,
              data: {
                ...n.data,
                step: updatedStep,
                onDoubleClick: onNodeDoubleClickCb,
                onBindingDrop: onBindingDropCb,
              },
            }
          : n
      )
    );
    setEditingStep(null);
    markDirty();
  }

  function handleStepDelete(stepId) {
    pushHistory();
    setNodes((ns) => ns.filter((n) => n.id !== stepId));
    setEdges((es) => es.filter((e) => e.source !== stepId && e.target !== stepId));
    setEditingStep(null);
    markDirty();
  }

  // ─── Auto-layout ──────────────────────────────────────────────────────────

  function handleAutoLayout() {
    pushHistory();
    const { nodes: ln, edges: le } = applyDagreLayout(nodes, edges);
    setNodes(ln);
    setEdges(le);
    markDirty();
  }

  // ─── Save IR to disk ──────────────────────────────────────────────────────

  async function handleSave() {
    if (!taskId || !isDirty) return;
    setSaveState("saving");
    const serialized = serializeIR(ir, nodes, edges);
    try {
      const res = await fetch(`/task-graph/ir/${encodeURIComponent(taskId)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(serialized),
      });
      if (!res.ok) {
        const msg = await res.text().catch(() => res.statusText);
        throw new Error(msg);
      }
      const saved = await res.json();
      setIr(saved);
      setSaveState("saved");
      setIsDirty(false);
      setTimeout(() => setSaveState("idle"), 3000);
    } catch (err) {
      setSaveState("error");
      console.error("Save failed:", err);
      setTimeout(() => setSaveState("idle"), 5000);
    }
  }

  // ─── Node click (single = detail panel) ───────────────────────────────────

  const onNodeClick = useCallback((_, node) => {
    const step = node.data?.step ?? null;
    setDetailStep((prev) => (prev?.id === step?.id ? null : step));
  }, []);

  const onPaneClick = useCallback(() => setDetailStep(null), []);

  // ─── Render ───────────────────────────────────────────────────────────────

  const dataBindings = ir?.data_bindings ?? [];

  if (loading) return <CenteredMessage text="Loading task graph…" />;
  if (error)   return <CenteredMessage text={error} isError />;

  return (
    <div style={{ display: "flex", flexDirection: "column", width: "100vw", height: "100vh", background: "#0f1117" }}>
      {/* Top toolbar */}
      <div style={{ height: 48, flexShrink: 0, background: "#13161f", borderBottom: "1px solid #1e2535" }}>
        <Toolbar
          taskId={ir?.task_id ?? taskId}
          stepCount={nodes.length}
          saveState={saveState}
          isDirty={isDirty}
          onSave={handleSave}
          onAutoLayout={handleAutoLayout}
          onUndo={undo}
          canUndo={historyRef.current.length > 0}
        />
      </div>

      {/* Main content row */}
      <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>
        {/* Left palette */}
        <NodePalette dataBindings={dataBindings} />

        {/* Canvas */}
        <div style={{ flex: 1, position: "relative" }}
          onDragOver={onDragOver}
          onDrop={onDrop}
        >
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={(changes) => { onNodesChange(changes); setIsDirty(true); }}
            onEdgesChange={(changes) => { onEdgesChange(changes); setIsDirty(true); }}
            onConnect={onConnect}
            onNodesDelete={onNodesDelete}
            nodeTypes={nodeTypes}
            onNodeClick={onNodeClick}
            onPaneClick={onPaneClick}
            isValidConnection={isValidConnection}
            fitView
            fitViewOptions={{ padding: 0.15 }}
            minZoom={0.15}
            maxZoom={2.5}
            deleteKeyCode={["Backspace", "Delete"]}
            colorMode="dark"
          >
            <Background variant={BackgroundVariant.Dots} gap={24} size={1.5} color="#1e2535" />
            <Controls style={{ background: "#1a1e2a", border: "1px solid #2a3040", borderRadius: 8 }} />
            <MiniMap
              style={{ background: "#0f1117", border: "1px solid #2a3040", borderRadius: 8 }}
              nodeColor={minimapColor}
              maskColor="#0f111788"
            />
          </ReactFlow>

          {/* Hint text when canvas is empty */}
          {nodes.length === 0 && (
            <div style={{
              position: "absolute", inset: 0, display: "flex", alignItems: "center",
              justifyContent: "center", pointerEvents: "none",
              color: "#2a3a4a", fontSize: 14, fontFamily: "'Inter','Segoe UI',sans-serif",
            }}>
              ← Drag a step from the palette to start building
            </div>
          )}
        </div>

        {/* Right detail panel */}
        {detailStep && (
          <StepDetailPanel
            step={detailStep}
            onClose={() => setDetailStep(null)}
            onEdit={(step) => { setEditingStep(step); setDetailStep(null); }}
            style={{ position: "relative", flexShrink: 0 }}
          />
        )}
      </div>

      {/* Node editor modal */}
      {editingStep && (
        <NodeEditorModal
          step={editingStep}
          onSave={handleStepSave}
          onDelete={handleStepDelete}
          onClose={() => setEditingStep(null)}
        />
      )}
    </div>
  );
}

function CenteredMessage({ text, isError = false }) {
  return (
    <div style={{
      width: "100%", height: "100vh", display: "flex", alignItems: "center",
      justifyContent: "center", color: isError ? "#e06c75" : "#556070",
      fontFamily: "'Inter','Segoe UI',sans-serif", fontSize: 15,
      padding: 32, textAlign: "center",
    }}>
      {text}
    </div>
  );
}

function minimapColor(node) {
  switch (node.data?.step?.kind) {
    case "action":         return "#2d6a9f";
    case "wait_for_event": return "#5a3d7a";
    case "wait_gate":      return "#3d5a7a";
    case "decision":       return "#7a5a2d";
    case "end":            return "#2d7a4a";
    default:               return "#2a3040";
  }
}
