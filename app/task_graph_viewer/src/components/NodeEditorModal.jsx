import { useState, useEffect, useCallback } from "react";

const EXECUTORS = [
  "personal_admin_manager",
  "emi_team_manager",
  "web_manager",
  "playwright_manager",
  "fact_gate_manager",
];

const S = {
  overlay: {
    position: "fixed",
    inset: 0,
    background: "#00000088",
    zIndex: 200,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontFamily: "'Inter', 'Segoe UI', sans-serif",
  },
  modal: {
    background: "#13161f",
    border: "1px solid #2a3040",
    borderRadius: 14,
    padding: "22px 24px",
    width: 500,
    maxWidth: "95vw",
    maxHeight: "90vh",
    overflowY: "auto",
    boxShadow: "0 16px 48px #00000099",
  },
  title: {
    fontSize: 15,
    fontWeight: 700,
    color: "#c8d0df",
    marginBottom: 18,
  },
  fieldGroup: { marginBottom: 14 },
  label: {
    display: "block",
    fontSize: 10,
    fontWeight: 700,
    color: "#4a6a8a",
    textTransform: "uppercase",
    letterSpacing: "0.08em",
    marginBottom: 5,
  },
  input: {
    width: "100%",
    background: "#0f1117",
    border: "1px solid #2a3040",
    borderRadius: 7,
    color: "#c8d0df",
    fontSize: 13,
    padding: "8px 10px",
    outline: "none",
    boxSizing: "border-box",
    fontFamily: "'Inter', 'Segoe UI', sans-serif",
  },
  textarea: {
    width: "100%",
    background: "#0f1117",
    border: "1px solid #2a3040",
    borderRadius: 7,
    color: "#c8d0df",
    fontSize: 12,
    padding: "8px 10px",
    outline: "none",
    resize: "vertical",
    minHeight: 72,
    lineHeight: 1.5,
    boxSizing: "border-box",
    fontFamily: "monospace",
  },
  select: {
    width: "100%",
    background: "#0f1117",
    border: "1px solid #2a3040",
    borderRadius: 7,
    color: "#c8d0df",
    fontSize: 13,
    padding: "8px 10px",
    outline: "none",
    boxSizing: "border-box",
    fontFamily: "'Inter', 'Segoe UI', sans-serif",
  },
  tagRow: {
    display: "flex",
    flexWrap: "wrap",
    gap: 5,
    marginTop: 5,
  },
  tag: {
    background: "#1e2535",
    border: "1px solid #2a3a4a",
    borderRadius: 5,
    fontSize: 11,
    color: "#7a9ab8",
    padding: "3px 8px",
    display: "flex",
    alignItems: "center",
    gap: 5,
  },
  tagDel: {
    cursor: "pointer",
    color: "#556070",
    fontWeight: 700,
    fontSize: 13,
    lineHeight: 1,
  },
  addTag: {
    marginTop: 6,
    display: "flex",
    gap: 6,
  },
  addTagInput: {
    flex: 1,
    background: "#0f1117",
    border: "1px solid #2a3040",
    borderRadius: 6,
    color: "#c8d0df",
    fontSize: 12,
    padding: "5px 9px",
    outline: "none",
    fontFamily: "monospace",
  },
  addTagBtn: {
    background: "#1e2a38",
    border: "1px solid #2a4060",
    borderRadius: 6,
    color: "#4a9fd4",
    fontSize: 12,
    padding: "5px 10px",
    cursor: "pointer",
  },
  footer: {
    display: "flex",
    justifyContent: "flex-end",
    gap: 10,
    marginTop: 20,
    borderTop: "1px solid #1e2535",
    paddingTop: 16,
  },
  btnCancel: {
    background: "none",
    border: "1px solid #2a3040",
    borderRadius: 7,
    color: "#556070",
    fontSize: 13,
    padding: "8px 16px",
    cursor: "pointer",
  },
  btnSave: {
    background: "#2d6a9f",
    border: "none",
    borderRadius: 7,
    color: "#e8ecf3",
    fontSize: 13,
    fontWeight: 600,
    padding: "8px 18px",
    cursor: "pointer",
  },
  btnDelete: {
    background: "none",
    border: "1px solid #6a2a2a",
    borderRadius: 7,
    color: "#e06c75",
    fontSize: 13,
    padding: "8px 14px",
    cursor: "pointer",
    marginRight: "auto",
  },
};

export default function NodeEditorModal({ step, onSave, onDelete, onClose }) {
  const [draft, setDraft] = useState(() => deepClone(step));
  const [newConsumes, setNewConsumes] = useState("");
  const [newProduces, setNewProduces] = useState("");
  const [newSubscription, setNewSubscription] = useState("");

  useEffect(() => {
    setDraft(deepClone(step));
  }, [step]);

  const set = useCallback((key, val) => {
    setDraft((prev) => ({ ...prev, [key]: val }));
  }, []);

  function addToList(key, val, clearFn) {
    const v = val.trim();
    if (!v) return;
    setDraft((prev) => {
      const arr = Array.isArray(prev[key]) ? prev[key] : [];
      if (arr.includes(v)) return prev;
      return { ...prev, [key]: [...arr, v] };
    });
    clearFn("");
  }

  function removeFromList(key, val) {
    setDraft((prev) => ({
      ...prev,
      [key]: (prev[key] ?? []).filter((x) => x !== val),
    }));
  }

  function handleKeyDown(e) {
    if (e.key === "Escape") onClose();
  }

  return (
    <div style={S.overlay} onClick={(e) => e.target === e.currentTarget && onClose()} onKeyDown={handleKeyDown}>
      <div style={S.modal}>
        <div style={S.title}>Edit Step — {draft.kind}</div>

        {/* Common: title */}
        <Field label="Title">
          <input
            style={S.input}
            value={draft.title ?? ""}
            onChange={(e) => set("title", e.target.value)}
            placeholder="Step title"
          />
        </Field>

        {/* Action-specific */}
        {draft.kind === "action" && (
          <>
            <Field label="Executor">
              <select style={S.select} value={draft.executor ?? ""} onChange={(e) => set("executor", e.target.value)}>
                {EXECUTORS.map((ex) => (
                  <option key={ex} value={ex}>{ex}</option>
                ))}
                {!EXECUTORS.includes(draft.executor ?? "") && draft.executor && (
                  <option value={draft.executor}>{draft.executor}</option>
                )}
              </select>
            </Field>
            <Field label="Instruction">
              <textarea
                style={S.textarea}
                value={draft.instruction ?? ""}
                onChange={(e) => set("instruction", e.target.value)}
                placeholder="What this step should do…"
              />
            </Field>
            <Field label="Consumes (data IDs)">
              <TagList
                items={draft.consumes_data_ids ?? []}
                onRemove={(v) => removeFromList("consumes_data_ids", v)}
              />
              <AddTagRow
                value={newConsumes}
                onChange={setNewConsumes}
                onAdd={() => addToList("consumes_data_ids", newConsumes, setNewConsumes)}
                placeholder="fact_* or artifact_*"
              />
            </Field>
            <Field label="Produces (data IDs)">
              <TagList
                items={draft.produces_data_ids ?? []}
                onRemove={(v) => removeFromList("produces_data_ids", v)}
              />
              <AddTagRow
                value={newProduces}
                onChange={setNewProduces}
                onAdd={() => addToList("produces_data_ids", newProduces, setNewProduces)}
                placeholder="fact_* or artifact_*"
              />
            </Field>
          </>
        )}

        {/* Wait for event */}
        {draft.kind === "wait_for_event" && (
          <>
            <Field label="Event name">
              <input
                style={{ ...S.input, fontFamily: "monospace", fontSize: 12 }}
                value={draft.event_name ?? ""}
                onChange={(e) => set("event_name", e.target.value)}
                placeholder="signal_router.watch.email_from_x"
              />
            </Field>
            <Field label="Clear condition (optional)">
              <textarea
                style={S.textarea}
                value={draft.clear_condition ?? ""}
                onChange={(e) => set("clear_condition", e.target.value)}
                placeholder='task_state.facts.flag == True'
                rows={2}
              />
            </Field>
          </>
        )}

        {/* Wait gate */}
        {draft.kind === "wait_gate" && (
          <>
            <Field label="Subscriptions (event names)">
              <TagList
                items={draft.subscriptions ?? []}
                onRemove={(v) => removeFromList("subscriptions", v)}
              />
              <AddTagRow
                value={newSubscription}
                onChange={setNewSubscription}
                onAdd={() => addToList("subscriptions", newSubscription, setNewSubscription)}
                placeholder="signal_router.watch.*"
              />
            </Field>
            <Field label="Release condition">
              <textarea
                style={S.textarea}
                value={draft.release_condition ?? ""}
                onChange={(e) => set("release_condition", e.target.value)}
                placeholder='"event.a" in task_state.facts.events_observed'
              />
            </Field>
          </>
        )}

        {/* Decision */}
        {draft.kind === "decision" && (
          <Field label="Condition">
            <textarea
              style={S.textarea}
              value={draft.condition ?? ""}
              onChange={(e) => set("condition", e.target.value)}
              placeholder="task_state.facts.my_flag == True"
              rows={3}
            />
          </Field>
        )}

        <div style={S.footer}>
          <button style={S.btnDelete} onClick={() => onDelete(step.id)}>
            Delete step
          </button>
          <button style={S.btnCancel} onClick={onClose}>
            Cancel
          </button>
          <button
            style={S.btnSave}
            onClick={() => {
              if (!draft.title?.trim()) {
                alert("Title is required.");
                return;
              }
              onSave(draft);
            }}
          >
            Save
          </button>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }) {
  return (
    <div style={S.fieldGroup}>
      <label style={S.label}>{label}</label>
      {children}
    </div>
  );
}

function TagList({ items, onRemove }) {
  if (!items.length) return null;
  return (
    <div style={S.tagRow}>
      {items.map((item) => (
        <span key={item} style={S.tag}>
          {item}
          <span style={S.tagDel} onClick={() => onRemove(item)} title="Remove">×</span>
        </span>
      ))}
    </div>
  );
}

function AddTagRow({ value, onChange, onAdd, placeholder }) {
  return (
    <div style={S.addTag}>
      <input
        style={S.addTagInput}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        onKeyDown={(e) => e.key === "Enter" && onAdd()}
      />
      <button style={S.addTagBtn} onClick={onAdd}>+ Add</button>
    </div>
  );
}

function deepClone(obj) {
  return JSON.parse(JSON.stringify(obj ?? {}));
}
