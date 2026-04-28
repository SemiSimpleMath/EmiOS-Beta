import { Handle, Position } from "@xyflow/react";
import { useState, useCallback } from "react";

export const KIND_CONFIG = {
  action:        { color: "#2d6a9f", border: "#4a9fd4", icon: "⚡", label: "Action" },
  wait_for_event:{ color: "#5a3d7a", border: "#9b6ecf", icon: "⏳", label: "Wait Event" },
  wait_gate:     { color: "#3d5a7a", border: "#6e9bcf", icon: "🔒", label: "Wait Gate" },
  decision:      { color: "#7a5a2d", border: "#cfaa6e", icon: "⟐", label: "Decision" },
  end:           { color: "#2d7a4a", border: "#4acf87", icon: "✓", label: "End" },
};

const DEFAULT_CONFIG = { color: "#2a2e3a", border: "#555", icon: "·", label: "Step" };

export default function TaskNode({ data, selected }) {
  const { step, onDoubleClick, onBindingDrop } = data;
  const cfg = KIND_CONFIG[step.kind] ?? DEFAULT_CONFIG;
  const isDecision = step.kind === "decision";
  const isEnd = step.kind === "end";

  const [isDragOver, setIsDragOver] = useState(false);

  const handleDoubleClick = useCallback(
    (e) => {
      e.stopPropagation();
      if (typeof onDoubleClick === "function") onDoubleClick(step);
    },
    [step, onDoubleClick]
  );

  const handleDragOver = useCallback((e) => {
    if (e.dataTransfer.types.includes("application/task-binding")) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
      setIsDragOver(true);
    }
  }, []);

  const handleDragLeave = useCallback(() => setIsDragOver(false), []);

  const handleDrop = useCallback(
    (e) => {
      setIsDragOver(false);
      const raw = e.dataTransfer.getData("application/task-binding");
      if (!raw) return;
      e.stopPropagation();
      try {
        const binding = JSON.parse(raw);
        if (typeof onBindingDrop === "function") onBindingDrop(step.id, binding);
      } catch {
        // ignore malformed drag data
      }
    },
    [step.id, onBindingDrop]
  );

  const bindings = step._bindings ?? [];

  return (
    <div
      onDoubleClick={handleDoubleClick}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
      style={{
        background: isDragOver ? lighten(cfg.color) : cfg.color,
        border: `2px solid ${selected ? "#ffffff" : isDragOver ? "#ffffff88" : cfg.border}`,
        borderRadius: 10,
        padding: "10px 14px 12px",
        minWidth: 210,
        maxWidth: 250,
        fontFamily: "'Inter', 'Segoe UI', sans-serif",
        boxShadow: selected
          ? "0 0 0 2px #ffffff44, 0 4px 20px #00000066"
          : "0 2px 10px #00000055",
        transition: "all 0.12s",
        cursor: "default",
        userSelect: "none",
      }}
    >
      {!isEnd && <Handle type="target" position={Position.Top} style={handleStyle} />}

      {/* Kind badge row */}
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
        <span style={{ fontSize: 13 }}>{cfg.icon}</span>
        <span style={{ fontSize: 10, fontWeight: 700, color: cfg.border, textTransform: "uppercase", letterSpacing: "0.08em" }}>
          {cfg.label}
        </span>
        {step.executor && (
          <span style={{ marginLeft: "auto", fontSize: 9, color: "#8899aa", background: "#ffffff14", borderRadius: 4, padding: "1px 5px" }}>
            {shortExecutor(step.executor)}
          </span>
        )}
        <span style={{ fontSize: 10, color: "#2a3a4a", marginLeft: step.executor ? 0 : "auto" }} title="Double-click to edit">✎</span>
      </div>

      {/* Title */}
      <div style={{ fontSize: 13, fontWeight: 600, color: "#e8ecf3", lineHeight: 1.35, marginBottom: 4, wordBreak: "break-word" }}>
        {step.title || step.id}
      </div>

      {/* Kind-specific excerpt */}
      {step.instruction && (
        <div style={{ fontSize: 11, color: "#9aabb8", lineHeight: 1.4, overflow: "hidden", display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical" }}>
          {step.instruction}
        </div>
      )}
      {isDecision && step.condition && (
        <div style={{ marginTop: 4, fontSize: 10, color: "#cfaa6e", fontFamily: "monospace", background: "#ffffff0a", borderRadius: 4, padding: "3px 6px", wordBreak: "break-all" }}>
          {truncate(step.condition, 80)}
        </div>
      )}
      {step.event_name && (
        <div style={{ marginTop: 4, fontSize: 10, color: "#9b6ecf", fontFamily: "monospace", wordBreak: "break-all" }}>
          {truncate(step.event_name, 60)}
        </div>
      )}
      {step.subscriptions?.length > 0 && (
        <div style={{ marginTop: 4, fontSize: 10, color: "#6e9bcf", fontFamily: "monospace" }}>
          {step.subscriptions.length} subscription{step.subscriptions.length !== 1 ? "s" : ""}
        </div>
      )}

      {/* Data binding annotations */}
      {bindings.length > 0 && (
        <div style={{ marginTop: 7, display: "flex", flexWrap: "wrap", gap: 3 }}>
          {bindings.map((b) => (
            <span
              key={b.data_id}
              title={`${b.role}: ${b.data_id}`}
              style={{
                fontSize: 9,
                background: b.role === "produces" ? "#0a2a1a" : "#1a1a2a",
                border: `1px solid ${b.role === "produces" ? "#1a5030" : "#2a2a5a"}`,
                borderRadius: 4,
                color: b.role === "produces" ? "#4caf87" : "#7a7acf",
                padding: "1px 5px",
              }}
            >
              {b.role === "produces" ? "→" : "←"} {b.data_id}
            </span>
          ))}
        </div>
      )}

      {/* Drop hint */}
      {isDragOver && (
        <div style={{ marginTop: 6, fontSize: 10, color: "#ffffff88", textAlign: "center" }}>
          Drop to annotate binding
        </div>
      )}

      {isDecision ? (
        <>
          <Handle type="source" position={Position.Bottom} id="true"
            style={{ ...handleStyle, left: "28%", background: "#4caf87" }} />
          <Handle type="source" position={Position.Bottom} id="false"
            style={{ ...handleStyle, left: "72%", background: "#e06c75" }} />
        </>
      ) : (
        !isEnd && <Handle type="source" position={Position.Bottom} style={handleStyle} />
      )}
    </div>
  );
}

const handleStyle = {
  width: 11,
  height: 11,
  background: "#4a6a8a",
  border: "2px solid #1e2535",
};

function shortExecutor(name) {
  return name.replace(/_manager$/, "").split("_").map((p) => p[0]?.toUpperCase() ?? "").join("") || name.slice(0, 4).toUpperCase();
}

function truncate(str, len) {
  return str.length > len ? str.slice(0, len) + "…" : str;
}

function lighten(hex) {
  // Slightly brighten a hex color for drag-over highlight
  try {
    const n = parseInt(hex.slice(1), 16);
    const r = Math.min(255, ((n >> 16) & 0xff) + 30);
    const g = Math.min(255, ((n >> 8)  & 0xff) + 30);
    const b = Math.min(255,  (n        & 0xff) + 30);
    return `#${r.toString(16).padStart(2,"0")}${g.toString(16).padStart(2,"0")}${b.toString(16).padStart(2,"0")}`;
  } catch {
    return hex;
  }
}
