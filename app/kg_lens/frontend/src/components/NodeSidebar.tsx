import { ReactNode, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { editKgNodeField, fetchKgNodeDetail, setKgNodeLock } from "../api";
import {
  DateConfidence,
  EditableNodeField,
  KgEdgeRef,
  KgNodeDetail,
} from "../types";

interface Props {
  nodeId: string;
  onClose: () => void;
  // Select another node (open its sidebar) WITHOUT reseeding the graph.
  onSelectNode: (id: string) => void;
}

const GOAL_STATUS_OPTIONS = ["active", "dormant", "completed", "abandoned"];

// ---------- small helpers ----------

function shortDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const days = Math.floor((Date.now() - d.getTime()) / 86_400_000);
  if (days <= 0) return "today";
  if (days < 7) return `${days}d ago`;
  return d.toISOString().slice(0, 10);
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return text.slice(0, max - 1) + "…";
}

function confidenceChipClass(conf: DateConfidence): string {
  switch (conf) {
    case "user_set":
      return "bg-blue-50 text-blue-700 border-blue-200";
    case "actual":
      return "bg-green-50 text-green-700 border-green-200";
    case "estimated":
      return "bg-amber-50 text-amber-700 border-amber-200";
    case "inferred":
      return "bg-purple-50 text-purple-700 border-purple-200";
    default:
      return "bg-gray-50 text-gray-600 border-gray-200";
  }
}

function PencilIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
    </svg>
  );
}

function LockIcon({ locked }: { locked: boolean }) {
  return (
    <svg
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <rect x="3" y="11" width="18" height="11" rx="2" />
      {locked ? (
        <path d="M7 11V7a5 5 0 0 1 10 0v4" />
      ) : (
        <path d="M7 11V7a5 5 0 0 1 9.9-1" />
      )}
    </svg>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mb-5">
      <h3 className="text-xs uppercase tracking-wide text-gray-500 mb-1.5">
        {title}
      </h3>
      {children}
    </section>
  );
}

// ---------- per-field inline editor ----------

interface EditableFieldProps {
  nodeId: string;
  field: EditableNodeField;
  kind: "text" | "multiline" | "date" | "number" | "select";
  current: string | number | null;
  display: ReactNode;
  // Small caption above the value; omit for header-style fields.
  caption?: string;
  helper?: string;
  options?: string[];
  disabled: boolean;
  onSaved: (field: EditableNodeField) => void;
}

function EditableField({
  nodeId,
  field,
  kind,
  current,
  display,
  caption,
  helper,
  options,
  disabled,
  onSaved,
}: EditableFieldProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  function startEdit() {
    const initial =
      current === null ? "" : kind === "date" ? String(current).slice(0, 10) : String(current);
    setDraft(initial);
    setReason("");
    setError(null);
    setEditing(true);
  }

  function cancel() {
    setEditing(false);
    setError(null);
  }

  async function save() {
    if (!reason.trim()) {
      setError("A reason is required.");
      return;
    }
    let value: string | number | null;
    if (kind === "number") {
      const n = Number(draft);
      if (draft.trim() === "" || Number.isNaN(n)) {
        setError("Enter a number.");
        return;
      }
      value = n;
    } else if (kind === "select") {
      if (!draft) {
        setError("Choose a value.");
        return;
      }
      value = draft;
    } else {
      value = draft.trim() === "" ? null : kind === "date" ? draft : draft.trim();
    }
    setSaving(true);
    setError(null);
    try {
      await editKgNodeField({ id: nodeId, field, value, reason: reason.trim() });
      setEditing(false);
      setReason("");
      onSaved(field);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Edit failed");
    } finally {
      setSaving(false);
    }
  }

  const inputClass =
    "w-full px-2 py-1 text-sm border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-accent";

  if (!editing) {
    return (
      <div className="py-0.5">
        {caption && (
          <span className="text-[11px] text-gray-400 block">{caption}</span>
        )}
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0 flex-1">{display}</div>
          {!disabled && (
            <button
              onClick={startEdit}
              className="text-gray-400 hover:text-gray-700 mt-1 shrink-0"
              aria-label={`edit ${field}`}
              title={`Edit ${field}`}
            >
              <PencilIcon />
            </button>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="py-1.5 px-2 -mx-2 rounded border border-gray-200 bg-gray-50">
      {caption && (
        <span className="text-[11px] text-gray-400 block mb-1">{caption}</span>
      )}
      {kind === "multiline" ? (
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={4}
          className={inputClass}
          autoFocus
        />
      ) : kind === "select" ? (
        <select
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          className={inputClass}
          autoFocus
        >
          <option value="" disabled>
            choose…
          </option>
          {(options ?? []).map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      ) : (
        <input
          type={kind === "date" ? "date" : kind === "number" ? "number" : "text"}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          min={kind === "number" ? 0 : undefined}
          max={kind === "number" ? 1 : undefined}
          step={kind === "number" ? 0.05 : undefined}
          className={inputClass}
          autoFocus
        />
      )}
      <input
        type="text"
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder="Reason (required)"
        className={`${inputClass} mt-1.5`}
      />
      <div className="flex items-center gap-2 mt-1.5">
        <button
          onClick={save}
          disabled={saving}
          className="text-xs px-2.5 py-1 rounded bg-accent text-accent-fg hover:bg-blue-700 disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          onClick={cancel}
          disabled={saving}
          className="text-xs px-2.5 py-1 rounded border border-gray-300 hover:bg-gray-100"
        >
          Cancel
        </button>
      </div>
      {error && <p className="text-xs text-red-600 mt-1">{error}</p>}
      {helper && <p className="text-[11px] text-gray-400 mt-1">{helper}</p>}
    </div>
  );
}

// ---------- read-only meta row ----------

function MetaRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-0.5">
      <span className="text-[11px] text-gray-400 shrink-0">{label}</span>
      <span className="text-sm text-gray-800 text-right min-w-0">{children}</span>
    </div>
  );
}

// ---------- date block (start or end) ----------

function DateBlock({
  which,
  detail,
  disabled,
  onSaved,
}: {
  which: "start" | "end";
  detail: KgNodeDetail;
  disabled: boolean;
  onSaved: (field: EditableNodeField) => void;
}) {
  const isStart = which === "start";
  const display = isStart ? detail.display_start : detail.display_end;
  const conf = isStart ? detail.start_date_confidence : detail.end_date_confidence;
  const iso = isStart ? detail.start_date : detail.end_date;
  const prose = isStart ? detail.start_date_prose : detail.end_date_prose;

  return (
    <div className="mb-2.5">
      <div className="flex items-center gap-2 mb-0.5">
        <span className="text-[11px] text-gray-400 w-8">{isStart ? "Start" : "End"}</span>
        <span className="text-sm font-semibold text-gray-900">
          {display ?? "—"}
        </span>
        {conf && (
          <span
            className={`text-[10px] px-1.5 py-0.5 rounded-full border ${confidenceChipClass(conf)}`}
          >
            {conf}
          </span>
        )}
      </div>
      <div className="pl-10">
        <EditableField
          nodeId={detail.id}
          field={isStart ? "start_date" : "end_date"}
          kind="date"
          current={iso}
          caption="ISO date"
          display={<span className="text-xs text-gray-600">{iso ?? "—"}</span>}
          helper="Editing a date marks it user_set. Empty clears it."
          disabled={disabled}
          onSaved={onSaved}
        />
        <EditableField
          nodeId={detail.id}
          field={isStart ? "start_date_prose" : "end_date_prose"}
          kind="text"
          current={prose}
          caption="Prose"
          display={<span className="text-xs text-gray-600">{prose ?? "—"}</span>}
          disabled={disabled}
          onSaved={onSaved}
        />
      </div>
    </div>
  );
}

// ---------- connection row ----------

function EdgeRow({
  edge,
  onSelectNode,
}: {
  edge: KgEdgeRef;
  onSelectNode: (id: string) => void;
}) {
  return (
    <button
      onClick={() => onSelectNode(edge.other_id)}
      className="w-full text-left px-2 py-1.5 rounded hover:bg-gray-50 border border-transparent hover:border-gray-200"
      title={`Open ${edge.other_label}`}
    >
      <div className="text-sm truncate">
        <span className="text-gray-500">{edge.rel}</span>{" "}
        <span className="font-medium text-gray-900">{edge.other_label}</span>{" "}
        <span className="text-[10px] text-gray-400 uppercase">{edge.other_type}</span>
      </div>
      {edge.sentence && (
        <div className="text-xs text-gray-500 truncate">{edge.sentence}</div>
      )}
    </button>
  );
}

// ---------- the sidebar ----------

export function NodeSidebar({ nodeId, onClose, onSelectNode }: Props) {
  const queryClient = useQueryClient();
  const {
    data: detail,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["kg-node-detail", nodeId],
    queryFn: () => fetchKgNodeDetail(nodeId),
  });

  const [lockPrompt, setLockPrompt] = useState(false);
  const [lockReason, setLockReason] = useState("");
  const [lockError, setLockError] = useState<string | null>(null);
  const [lockSaving, setLockSaving] = useState(false);

  function handleSaved(field: EditableNodeField) {
    queryClient.invalidateQueries({ queryKey: ["kg-node-detail", nodeId] });
    if (field === "label") {
      // Label shows on the canvas — refresh the graph cut too.
      queryClient.invalidateQueries({ queryKey: ["seed-graph"] });
    }
  }

  async function confirmLockToggle() {
    if (!detail) return;
    if (!lockReason.trim()) {
      setLockError("A reason is required.");
      return;
    }
    setLockSaving(true);
    setLockError(null);
    try {
      await setKgNodeLock({
        id: detail.id,
        locked: !detail.locked,
        reason: lockReason.trim(),
      });
      setLockPrompt(false);
      setLockReason("");
      queryClient.invalidateQueries({ queryKey: ["kg-node-detail", nodeId] });
    } catch (err) {
      setLockError(err instanceof Error ? err.message : "Lock change failed");
    } finally {
      setLockSaving(false);
    }
  }

  const locked = detail?.locked ?? false;

  return (
    <aside
      className="absolute top-0 right-0 h-full w-full md:w-[400px] bg-white shadow-2xl border-l border-gray-200 z-20 flex flex-col"
      role="dialog"
      aria-label="Node details"
    >
      {/* Header */}
      <header className="px-5 py-4 border-b border-gray-200">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0 flex-1">
            {detail ? (
              <EditableField
                nodeId={detail.id}
                field="label"
                kind="text"
                current={detail.label}
                display={
                  <h2 className="text-lg font-semibold leading-snug">
                    {detail.label}
                  </h2>
                }
                disabled={locked}
                onSaved={handleSaved}
              />
            ) : (
              <h2 className="text-lg font-semibold text-gray-400">
                {isLoading ? "Loading…" : "Node"}
              </h2>
            )}
            {detail && (
              <div className="flex flex-wrap gap-1.5 mt-1">
                <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded-full bg-gray-100 text-gray-600 border border-gray-200">
                  {detail.node_type}
                </span>
                {detail.category && (
                  <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded-full bg-gray-100 text-gray-600 border border-gray-200">
                    {detail.category}
                  </span>
                )}
              </div>
            )}
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {detail && (
              <button
                onClick={() => {
                  setLockPrompt((v) => !v);
                  setLockError(null);
                  setLockReason("");
                }}
                className={
                  locked
                    ? "text-blue-600 hover:text-blue-800"
                    : "text-gray-400 hover:text-gray-700"
                }
                aria-label={locked ? "unlock node" : "lock node"}
                title={locked ? "User-locked — click to unlock" : "Lock as user axiom"}
              >
                <LockIcon locked={locked} />
              </button>
            )}
            <button
              onClick={onClose}
              className="text-gray-500 hover:text-gray-900 text-xl leading-none"
              aria-label="close"
            >
              ×
            </button>
          </div>
        </div>

        {locked && (
          <div className="mt-2 text-xs text-blue-800 bg-blue-50 border border-blue-200 rounded px-2.5 py-1.5">
            User-locked (axiom) — unlock to edit
            {detail?.locked_at ? ` · since ${shortDate(detail.locked_at)}` : ""}
          </div>
        )}

        {lockPrompt && detail && (
          <div className="mt-2 p-2.5 rounded border border-gray-200 bg-gray-50">
            <p className="text-xs text-gray-700 mb-1.5">
              {locked
                ? "Unlock this node? Agents may revise it again."
                : "Lock this node as a user axiom? Edits will be refused until unlocked."}
            </p>
            <input
              type="text"
              value={lockReason}
              onChange={(e) => setLockReason(e.target.value)}
              placeholder="Reason (required)"
              className="w-full px-2 py-1 text-sm border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-accent"
              autoFocus
            />
            <div className="flex items-center gap-2 mt-1.5">
              <button
                onClick={confirmLockToggle}
                disabled={lockSaving}
                className="text-xs px-2.5 py-1 rounded bg-accent text-accent-fg hover:bg-blue-700 disabled:opacity-50"
              >
                {lockSaving ? "Saving…" : locked ? "Unlock" : "Lock"}
              </button>
              <button
                onClick={() => setLockPrompt(false)}
                disabled={lockSaving}
                className="text-xs px-2.5 py-1 rounded border border-gray-300 hover:bg-gray-100"
              >
                Cancel
              </button>
            </div>
            {lockError && (
              <p className="text-xs text-red-600 mt-1">{lockError}</p>
            )}
          </div>
        )}
      </header>

      {/* Body */}
      <div className="flex-1 overflow-y-auto p-5">
        {isLoading && <p className="text-sm text-gray-500">Loading…</p>}
        {error && (
          <p className="text-sm text-red-600">{(error as Error).message}</p>
        )}

        {detail && (
          <>
            <Section title="Summary">
              <EditableField
                nodeId={detail.id}
                field="description"
                kind="multiline"
                current={detail.description}
                caption="Description"
                display={
                  <p className="text-sm text-gray-800 leading-relaxed whitespace-pre-wrap">
                    {detail.description || "—"}
                  </p>
                }
                disabled={locked}
                onSaved={handleSaved}
              />
              <EditableField
                nodeId={detail.id}
                field="original_sentence"
                kind="multiline"
                current={detail.original_sentence}
                caption="Original sentence"
                display={
                  <p className="text-sm text-gray-700 leading-relaxed whitespace-pre-wrap italic">
                    {detail.original_sentence || "—"}
                  </p>
                }
                disabled={locked}
                onSaved={handleSaved}
              />
            </Section>

            <Section title="Dates">
              <DateBlock
                which="start"
                detail={detail}
                disabled={locked}
                onSaved={handleSaved}
              />
              <DateBlock
                which="end"
                detail={detail}
                disabled={locked}
                onSaved={handleSaved}
              />
              <p className="text-[11px] text-gray-400">
                Editing a date marks it user_set.
              </p>
            </Section>

            <Section title="Status">
              <MetaRow label="Currently valid">
                <span
                  className={`text-[10px] px-1.5 py-0.5 rounded-full border ${
                    detail.valid_currently === true
                      ? "bg-green-50 text-green-700 border-green-200"
                      : detail.valid_currently === false
                        ? "bg-gray-100 text-gray-600 border-gray-200"
                        : "bg-gray-50 text-gray-500 border-gray-200"
                  }`}
                >
                  {detail.valid_currently === true
                    ? "current"
                    : detail.valid_currently === false
                      ? "not current"
                      : "unknown"}
                </span>
              </MetaRow>
              <EditableField
                nodeId={detail.id}
                field="valid_during"
                kind="text"
                current={detail.valid_during}
                caption="Valid during"
                display={
                  <span className="text-sm text-gray-800">
                    {detail.valid_during || "—"}
                  </span>
                }
                disabled={locked}
                onSaved={handleSaved}
              />
              {detail.node_type === "Goal" && (
                <>
                  <EditableField
                    nodeId={detail.id}
                    field="goal_status"
                    kind="select"
                    options={GOAL_STATUS_OPTIONS}
                    current={detail.goal_status}
                    caption="Goal status"
                    display={
                      <span className="text-sm text-gray-800">
                        {detail.goal_status || "—"}
                      </span>
                    }
                    disabled={locked}
                    onSaved={handleSaved}
                  />
                  {detail.last_pursued_at && (
                    <MetaRow label="Last pursued">
                      {shortDate(detail.last_pursued_at)}
                    </MetaRow>
                  )}
                </>
              )}
            </Section>

            <Section title="Meta">
              <EditableField
                nodeId={detail.id}
                field="importance"
                kind="number"
                current={detail.importance}
                caption="Importance (0–1)"
                display={
                  <span className="text-sm text-gray-800">
                    {detail.importance ?? "—"}
                  </span>
                }
                disabled={locked}
                onSaved={handleSaved}
              />
              <EditableField
                nodeId={detail.id}
                field="category"
                kind="text"
                current={detail.category}
                caption="Category"
                display={
                  <span className="text-sm text-gray-800">
                    {detail.category || "—"}
                  </span>
                }
                disabled={locked}
                onSaved={handleSaved}
              />
              <EditableField
                nodeId={detail.id}
                field="semantic_label"
                kind="text"
                current={detail.semantic_label}
                caption="Semantic label"
                display={
                  <span className="text-sm text-gray-800">
                    {detail.semantic_label || "—"}
                  </span>
                }
                disabled={locked}
                onSaved={handleSaved}
              />
              <MetaRow label="Confidence">
                {detail.confidence ?? "—"}
                {detail.confidence_tier && (
                  <span
                    className={`ml-1.5 text-[10px] px-1.5 py-0.5 rounded-full border ${
                      detail.confidence_tier === "axiom"
                        ? "bg-blue-50 text-blue-700 border-blue-200"
                        : "bg-gray-50 text-gray-600 border-gray-200"
                    }`}
                  >
                    {detail.confidence_tier}
                  </span>
                )}
              </MetaRow>
              <MetaRow label="Evidence">{detail.evidence_count}</MetaRow>
              <MetaRow label="Created">{shortDate(detail.created_at)}</MetaRow>
              <MetaRow label="Updated">{shortDate(detail.updated_at)}</MetaRow>
              {detail.aliases.length > 0 && (
                <div className="pt-1">
                  <span className="text-[11px] text-gray-400 block mb-1">
                    Aliases
                  </span>
                  <div className="flex flex-wrap gap-1">
                    {detail.aliases.map((a) => (
                      <span
                        key={a}
                        className="text-xs px-1.5 py-0.5 rounded-full bg-gray-100 text-gray-700 border border-gray-200"
                      >
                        {a}
                      </span>
                    ))}
                  </div>
                </div>
              )}
              {detail.wiki_url && (
                <div className="pt-2">
                  <a
                    href={detail.wiki_url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-sm text-accent hover:underline"
                  >
                    Open wiki page →
                  </a>
                </div>
              )}
            </Section>

            <Section title="Connections">
              {detail.edges_out.length === 0 && detail.edges_in.length === 0 && (
                <p className="text-sm text-gray-500">No connections.</p>
              )}
              {detail.edges_out.length > 0 && (
                <div className="mb-2">
                  <span className="text-[11px] text-gray-400 block mb-0.5">
                    Outgoing
                  </span>
                  {detail.edges_out.map((e) => (
                    <EdgeRow key={e.edge_id} edge={e} onSelectNode={onSelectNode} />
                  ))}
                </div>
              )}
              {detail.edges_in.length > 0 && (
                <div>
                  <span className="text-[11px] text-gray-400 block mb-0.5">
                    Incoming
                  </span>
                  {detail.edges_in.map((e) => (
                    <EdgeRow key={e.edge_id} edge={e} onSelectNode={onSelectNode} />
                  ))}
                </div>
              )}
            </Section>

            {detail.revisions.length > 0 && (
              <Section title="History">
                <ul className="space-y-1.5">
                  {detail.revisions.map((r, i) => (
                    <li key={`${r.created_at}-${i}`} className="text-xs text-gray-600">
                      <span className="font-mono text-[11px] bg-gray-100 rounded px-1 py-0.5 mr-1.5">
                        {r.op}
                      </span>
                      <span className="text-gray-400 mr-1.5">
                        {shortDate(r.created_at)}
                      </span>
                      <span title={r.reason}>{truncate(r.reason, 90)}</span>
                    </li>
                  ))}
                </ul>
              </Section>
            )}
          </>
        )}
      </div>
    </aside>
  );
}
