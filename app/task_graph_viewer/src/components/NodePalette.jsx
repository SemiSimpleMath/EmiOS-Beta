const PALETTE_ITEMS = [
  {
    kind: "action",
    icon: "⚡",
    label: "Action",
    desc: "Run an agent or manager",
    color: "#2d6a9f",
    border: "#4a9fd4",
  },
  {
    kind: "wait_for_event",
    icon: "⏳",
    label: "Wait Event",
    desc: "Suspend until a single event",
    color: "#5a3d7a",
    border: "#9b6ecf",
  },
  {
    kind: "wait_gate",
    icon: "🔒",
    label: "Wait Gate",
    desc: "Suspend on multiple events",
    color: "#3d5a7a",
    border: "#6e9bcf",
  },
  {
    kind: "decision",
    icon: "⟐",
    label: "Decision",
    desc: "Branch on a condition",
    color: "#7a5a2d",
    border: "#cfaa6e",
  },
  {
    kind: "end",
    icon: "✓",
    label: "End",
    desc: "Terminal step",
    color: "#2d7a4a",
    border: "#4acf87",
  },
];

const S = {
  sidebar: {
    width: 180,
    background: "#10131b",
    borderRight: "1px solid #1e2535",
    display: "flex",
    flexDirection: "column",
    flexShrink: 0,
    overflowY: "auto",
  },
  heading: {
    fontSize: 10,
    fontWeight: 700,
    color: "#3a4a5a",
    textTransform: "uppercase",
    letterSpacing: "0.1em",
    padding: "14px 14px 8px",
    fontFamily: "'Inter', 'Segoe UI', sans-serif",
  },
  item: (color, border) => ({
    margin: "0 10px 8px",
    padding: "9px 11px",
    borderRadius: 8,
    background: color,
    border: `1.5px solid ${border}`,
    cursor: "grab",
    userSelect: "none",
    fontFamily: "'Inter', 'Segoe UI', sans-serif",
  }),
  itemHeader: {
    display: "flex",
    alignItems: "center",
    gap: 7,
    marginBottom: 3,
  },
  itemIcon: { fontSize: 13 },
  itemLabel: {
    fontSize: 12,
    fontWeight: 700,
    color: "#d8e4f0",
  },
  itemDesc: {
    fontSize: 10,
    color: "#6a7a8a",
    lineHeight: 1.3,
  },
  divider: {
    borderTop: "1px solid #1e2535",
    margin: "10px 0",
  },
  sectionLabel: {
    fontSize: 10,
    fontWeight: 700,
    color: "#3a4a5a",
    textTransform: "uppercase",
    letterSpacing: "0.1em",
    padding: "4px 14px 8px",
    fontFamily: "'Inter', 'Segoe UI', sans-serif",
  },
};

export default function NodePalette({ dataBindings = [] }) {
  function onDragStart(e, kind) {
    e.dataTransfer.setData("application/task-node-kind", kind);
    e.dataTransfer.effectAllowed = "copy";
  }

  function onBindingDragStart(e, binding) {
    e.dataTransfer.setData("application/task-binding", JSON.stringify(binding));
    e.dataTransfer.effectAllowed = "copy";
  }

  return (
    <div style={S.sidebar}>
      <div style={S.heading}>Add Step</div>

      {PALETTE_ITEMS.map((item) => (
        <div
          key={item.kind}
          draggable
          onDragStart={(e) => onDragStart(e, item.kind)}
          style={S.item(item.color, item.border)}
          title={`Drag onto canvas to add a ${item.label} step`}
        >
          <div style={S.itemHeader}>
            <span style={S.itemIcon}>{item.icon}</span>
            <span style={S.itemLabel}>{item.label}</span>
          </div>
          <div style={S.itemDesc}>{item.desc}</div>
        </div>
      ))}

      {dataBindings.length > 0 && (
        <>
          <div style={S.divider} />
          <div style={S.sectionLabel}>Data Bindings</div>
          {dataBindings.map((b) => (
            <div
              key={b.data_id}
              draggable
              onDragStart={(e) => onBindingDragStart(e, b)}
              style={{
                margin: "0 10px 6px",
                padding: "7px 10px",
                borderRadius: 6,
                background: "#1a1e2a",
                border: "1.5px solid #2a3040",
                cursor: "grab",
                userSelect: "none",
                fontFamily: "'Inter', 'Segoe UI', sans-serif",
              }}
              title={`Drag onto a node to annotate consumes/produces`}
            >
              <div style={{ fontSize: 10, fontWeight: 700, color: "#6a8aaa", marginBottom: 2 }}>
                {b.data_kind === "artifact" ? "📦" : "📌"} {b.data_kind}
              </div>
              <div style={{ fontSize: 11, color: "#8899aa", wordBreak: "break-all" }}>
                {b.data_id}
              </div>
              {b.producer_step_id && (
                <div style={{ fontSize: 9, color: "#3a5060", marginTop: 2 }}>
                  from: {b.producer_step_id}
                </div>
              )}
            </div>
          ))}
        </>
      )}
    </div>
  );
}
