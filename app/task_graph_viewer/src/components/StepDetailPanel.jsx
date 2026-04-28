export default function StepDetailPanel({ step, onClose, onEdit }) {
  if (!step) return null;

  const rows = buildRows(step);

  return (
    <div
      style={{
        width: 300,
        background: "#10131b",
        borderLeft: "1px solid #1e2535",
        display: "flex",
        flexDirection: "column",
        fontFamily: "'Inter', 'Segoe UI', sans-serif",
        color: "#c8d0df",
        fontSize: 13,
        flexShrink: 0,
        overflowY: "auto",
      }}
    >
      {/* Header */}
      <div style={{
        display: "flex", justifyContent: "space-between", alignItems: "flex-start",
        padding: "14px 16px 10px", borderBottom: "1px solid #1e2535", flexShrink: 0,
      }}>
        <div>
          <div style={{ fontSize: 10, color: "#4a6a8a", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 3 }}>
            {step.kind}
          </div>
          <div style={{ fontSize: 14, fontWeight: 700, color: "#e8ecf3", lineHeight: 1.3 }}>
            {step.title || step.id}
          </div>
        </div>
        <button
          onClick={onClose}
          style={{ background: "none", border: "none", color: "#556070", cursor: "pointer", fontSize: 18, lineHeight: 1, padding: "0 0 0 8px" }}
          aria-label="Close"
        >×</button>
      </div>

      {/* Fields */}
      <div style={{ flex: 1, padding: "12px 16px", overflowY: "auto" }}>
        {rows.map(({ label, value, mono, multiline }) =>
          value ? (
            <div key={label} style={{ marginBottom: 10 }}>
              <div style={{ fontSize: 10, color: "#3a5a7a", textTransform: "uppercase", letterSpacing: "0.07em", marginBottom: 2 }}>
                {label}
              </div>
              <div style={{
                fontSize: 12, color: "#8899aa",
                fontFamily: mono ? "monospace" : "inherit",
                background: mono ? "#ffffff08" : "transparent",
                borderRadius: mono ? 5 : 0,
                padding: mono ? "4px 7px" : 0,
                wordBreak: "break-word",
                whiteSpace: multiline ? "pre-wrap" : "normal",
                lineHeight: 1.5,
              }}>
                {Array.isArray(value) ? value.join(", ") || "—" : String(value)}
              </div>
            </div>
          ) : null
        )}
      </div>

      {/* Footer */}
      <div style={{ padding: "10px 16px", borderTop: "1px solid #1e2535", flexShrink: 0 }}>
        {typeof onEdit === "function" && (
          <button
            onClick={() => onEdit(step)}
            style={{
              width: "100%", background: "#1e2535", border: "1px solid #2a3a4a",
              borderRadius: 7, color: "#7a9ab8", fontSize: 13, padding: "8px",
              cursor: "pointer", fontFamily: "'Inter','Segoe UI',sans-serif",
            }}
          >
            ✎ Edit step
          </button>
        )}
      </div>
    </div>
  );
}

function buildRows(step) {
  return [
    { label: "Step ID",          value: step.id,                mono: true },
    { label: "Executor",         value: step.executor },
    { label: "Instruction",      value: step.instruction,       multiline: true },
    { label: "Condition",        value: step.condition,         mono: true },
    { label: "Event",            value: step.event_name,        mono: true },
    { label: "Subscriptions",    value: step.subscriptions },
    { label: "Release condition",value: step.release_condition, mono: true, multiline: true },
    { label: "Consumes",         value: step.consumes_data_ids },
    { label: "Produces",         value: step.produces_data_ids },
    { label: "On true →",        value: step.on_true,           mono: true },
    { label: "On false →",       value: step.on_false,          mono: true },
    { label: "Next →",           value: step.next_step,         mono: true },
  ].filter((r) => r.value && (Array.isArray(r.value) ? r.value.length > 0 : true));
}
