const S = {
  bar: {
    display: "flex",
    alignItems: "center",
    gap: 10,
    padding: "0 14px",
    height: "100%",
    fontFamily: "'Inter', 'Segoe UI', sans-serif",
  },
  statusDot: (state) => ({
    width: 7,
    height: 7,
    borderRadius: "50%",
    background:
      state === "saved"
        ? "#4caf87"
        : state === "saving"
        ? "#cfaa6e"
        : state === "error"
        ? "#e06c75"
        : "#3a4a5a",
    flexShrink: 0,
  }),
  statusText: (state) => ({
    fontSize: 12,
    color:
      state === "saved"
        ? "#4caf87"
        : state === "saving"
        ? "#cfaa6e"
        : state === "error"
        ? "#e06c75"
        : "#5a6a7a",
  }),
  btn: (primary) => ({
    background: primary ? "#2d6a9f" : "#1a2030",
    border: primary ? "none" : "1px solid #2a3040",
    borderRadius: 7,
    color: primary ? "#e8ecf3" : "#7a9ab8",
    fontSize: 12,
    fontWeight: primary ? 600 : 400,
    padding: "6px 14px",
    cursor: "pointer",
    fontFamily: "'Inter', 'Segoe UI', sans-serif",
    whiteSpace: "nowrap",
  }),
  btnDanger: {
    background: "none",
    border: "1px solid #4a2020",
    borderRadius: 7,
    color: "#e06c75",
    fontSize: 12,
    padding: "6px 12px",
    cursor: "pointer",
    fontFamily: "'Inter', 'Segoe UI', sans-serif",
  },
  sep: {
    width: 1,
    height: 20,
    background: "#1e2535",
    margin: "0 2px",
  },
  kbd: {
    fontSize: 10,
    color: "#3a4a5a",
    background: "#0f1117",
    border: "1px solid #1e2535",
    borderRadius: 3,
    padding: "1px 5px",
    fontFamily: "monospace",
  },
};

export default function Toolbar({
  taskId,
  stepCount,
  saveState,
  isDirty,
  onSave,
  onAutoLayout,
  onUndo,
  canUndo,
}) {
  return (
    <div style={S.bar}>
      <span style={{ fontSize: 18, color: "#4a9fd4" }}>◈</span>
      <span style={{ fontSize: 14, fontWeight: 700, color: "#c8d0df" }}>
        {taskId ?? "Task Graph"}
      </span>

      <div style={S.sep} />

      <span style={{ fontSize: 12, color: "#3a5060" }}>
        {stepCount} step{stepCount !== 1 ? "s" : ""}
      </span>

      <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8 }}>
        {canUndo && (
          <button style={S.btn(false)} onClick={onUndo} title="Undo last change (Ctrl+Z)">
            ↩ Undo
          </button>
        )}

        <button style={S.btn(false)} onClick={onAutoLayout} title="Re-apply dagre layout">
          ⊞ Layout
        </button>

        <div style={S.sep} />

        <div style={S.statusDot(saveState)} />
        <span style={S.statusText(saveState)}>
          {saveState === "saved" && "Saved"}
          {saveState === "saving" && "Saving…"}
          {saveState === "error" && "Save failed"}
          {saveState === "idle" && (isDirty ? "Unsaved changes" : "No changes")}
        </span>

        <button
          style={S.btn(true)}
          onClick={onSave}
          disabled={saveState === "saving" || !isDirty}
        >
          Save IR
        </button>
      </div>
    </div>
  );
}
