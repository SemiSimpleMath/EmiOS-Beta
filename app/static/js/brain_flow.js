"use strict";
// /brain/flow: the brain's stages as a diagram, replayed one wake (run) at a time. A stage opens the
// model calls and the decisions code made in it during the chosen wake, in time order.
const BASE = window.SCRIPT_NAME || "";
const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
};
const SVG = "http://www.w3.org/2000/svg";
const svg = (tag, attrs) => {
  const e = document.createElementNS(SVG, tag);
  Object.entries(attrs || {}).forEach(([k, v]) => e.setAttribute(k, v));
  return e;
};
const clock = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "");
const day = (iso) => (iso ? new Date(iso).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }) : "");
const short = (name) => name.replace(/^(subconscious|dayflow_orchestrator)::/, "");
const size = (n) => (n == null ? "" : n >= 1000 ? `${(n / 1000).toFixed(1)}k chars` : `${n} chars`);

const state = { nodes: [], edges: [], runs: [], index: -1, run: null, open: null, concern: null, focusNode: null };

async function getJSON(url) {
  const r = await fetch(BASE + url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

// ── the diagram ─────────────────────────────────────────────────────────────

function renderNodes() {
  const root = $("#bf-diagram");
  root.textContent = "";
  state.nodes.forEach((n) => {
    const b = el("button", `bf-node${n.id === "concerns" ? " hub" : ""}`);
    b.type = "button";
    b.id = `node-${n.id}`;
    b.style.gridColumn = String(n.col);
    b.style.gridRow = n.rows ? `${n.row} / span ${n.rows}` : String(n.row);
    b.appendChild(el("div", "bf-node-title", n.title));
    b.appendChild(el("div", "bf-node-role", n.role));
    if (n.agents.length) {
      const chips = el("div", "bf-chips");
      n.agents.forEach((a) => chips.appendChild(el("span", "bf-chip agent", short(a.name))));
      b.appendChild(chips);
    }
    b.appendChild(el("div", "bf-counts"));
    b.addEventListener("click", () => openNode(n.id));
    root.appendChild(b);
  });
}

function itemsOf(nodeId) {
  const run = state.run;
  if (!run) return [];
  const steps = run.steps.filter((s) => s.node === nodeId).map((s) => ({ ...s, kind: "step" }));
  const calls = run.calls.filter((c) => c.node === nodeId).map((c) => ({ ...c, kind: "call" }));
  return [...steps, ...calls].sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0));
}

const isRefused = (s) => s.what.startsWith("answer refused");
const isFailed = (s) => s.what.includes("failed");

function countsFor(nodeId) {
  const run = state.run;
  const sum = (run && run.summary) || {};
  const steps = run ? run.steps.filter((s) => s.node === nodeId) : [];
  const calls = run ? run.calls.filter((c) => c.node === nodeId) : [];
  const out = [];
  const add = (label, n, cls) => { if (n) out.push([`${n} ${label}`, cls || ""]); };
  const by = (what) => steps.filter((s) => s.what === what).length;
  switch (nodeId) {
    case "gate":
      add("ingested", sum.ingested); add("routed", sum.routed); add("passed on", sum.passed_on);
      add("failed", sum.failed, "bad"); add("waiting", sum.waiting); break;
    case "brain":
      add("matters", sum.brain_matters); add("failed", sum.brain_failed, "bad"); break;
    case "day_pass":
      if (sum.day_pass && sum.day_pass !== "unchanged") out.push([String(sum.day_pass).replace(/_/g, " "), ""]); break;
    case "door":
      add("new", by("create")); add("joined open", by("merge_open")); add("joined candidate", by("merge_candidate"));
      add("matched closed", by("suppress_closed")); break;
    case "concerns":
      add("touched", run ? Object.keys(run.concerns || {}).length : 0); break;
    case "summary":
      add("written", sum.summaries_written); add("failed", sum.summaries_failed, "bad"); break;
    case "plan":
      add("written", sum.plans_written); add("failed", sum.plans_failed, "bad"); add("tool results", by("tool result"));
      break;
    case "handoff":
      add("handed over", sum.handed_over); add("held back", sum.handoff_skipped, "warn"); break;
    case "stories":
      add("told", sum.stories_written); add("failed", sum.stories_failed, "bad"); break;
    default: break;
  }
  add("calls", calls.length);
  add("refused", steps.filter(isRefused).length, "warn");
  return { out, ran: steps.length > 0 || calls.length > 0 || out.length > 0,
    error: steps.some(isFailed) || calls.some((c) => c.error) };
}

function paintNodes() {
  state.nodes.forEach((n) => {
    const b = $(`#node-${n.id}`);
    const { out, ran, error } = countsFor(n.id);
    b.classList.toggle("ran", ran);
    b.classList.toggle("has-error", error);
    b.classList.toggle("is-open", state.open === n.id);
    const box = b.querySelector(".bf-counts");
    box.textContent = "";
    out.forEach(([text, cls]) => box.appendChild(el("span", `bf-count ${cls}`, text)));
  });
  drawEdges();
}

function drawEdges() {
  const canvas = $("#bf-canvas");
  const layer = $("#bf-edges");
  layer.textContent = "";
  if (window.matchMedia("(max-width:900px)").matches) return;
  const base = canvas.getBoundingClientRect();
  const box = (id) => {
    const r = $(`#node-${id}`).getBoundingClientRect();
    return { l: r.left - base.left, r: r.right - base.left, t: r.top - base.top, b: r.bottom - base.top,
      cx: (r.left + r.right) / 2 - base.left, cy: (r.top + r.bottom) / 2 - base.top };
  };
  const defs = svg("defs");
  ["", "live"].forEach((k) => {
    const m = svg("marker", { id: `arrow${k}`, viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7",
      markerHeight: "7", orient: "auto-start-reverse" });
    m.appendChild(svg("path", { d: "M0,0 L10,5 L0,10 z", class: `bf-arrowhead ${k}` }));
    defs.appendChild(m);
  });
  layer.appendChild(defs);
  const bottom = Math.max(...state.nodes.map((n) => box(n.id).b));
  const col = Object.fromEntries(state.nodes.map((n) => [n.id, n.col]));
  state.edges.forEach((e) => {
    const a = box(e.from), b = box(e.to);
    let d, lx, ly;
    if (b.l >= a.r && col[e.to] - col[e.from] > 1) {              // skips a stage: arc over it
      const top = Math.min(a.t, b.t) - 34;
      d = `M${a.cx},${a.t} C${a.cx},${top} ${b.cx},${top} ${b.cx},${b.t}`;
      lx = (a.cx + b.cx) / 2; ly = top + 4;
    } else if (b.l >= a.r) {                                      // to the right
      const y1 = a.cy, y2 = Math.min(Math.max(a.cy, b.t + 20), b.b - 20);
      const mx = (a.r + b.l) / 2;
      d = `M${a.r},${y1} C${mx},${y1} ${mx},${y2} ${b.l},${y2}`;
      lx = mx; ly = Math.min(y1, y2) - 6;
    } else if (b.r <= a.l) {                                      // back to the start: under everything
      const y = bottom + 30;
      d = `M${a.cx},${a.b} V${y} H${b.l - 16} V${b.cy} H${b.l}`;
      lx = (a.cx + b.l) / 2; ly = y - 6;
    } else if (b.t >= a.b) {                                      // straight down
      d = `M${a.cx},${a.b} L${b.cx},${b.t}`;
      lx = a.cx + 6; ly = (a.b + b.t) / 2;
    } else {                                                      // straight up
      d = `M${a.cx},${a.t} L${b.cx},${b.b}`;
      lx = a.cx + 6; ly = (a.t + b.b) / 2;
    }
    const live = $(`#node-${e.from}`).classList.contains("ran") && $(`#node-${e.to}`).classList.contains("ran");
    layer.appendChild(svg("path", { d, class: `bf-edge${e.label ? " side" : ""}${live ? " live" : ""}`,
      "marker-end": `url(#arrow${live ? "live" : ""})` }));
    if (e.label) {
      const t = svg("text", { x: lx, y: ly, class: "bf-edge-label", "text-anchor": "middle" });
      t.textContent = e.label;
      layer.appendChild(t);
    }
  });
}

// ── the scrubber ────────────────────────────────────────────────────────────

async function loadRuns(keepId) {
  const params = new URLSearchParams({ limit: "400" });
  if ($("#bf-idle").checked) params.set("idle", "1");
  if (state.concern) params.set("concern", state.concern);
  const { runs } = await getJSON(`/api/brain/runs?${params}`);
  state.runs = runs.slice().reverse();                             // oldest first, left to right
  renderStrip();
  const at = keepId ? state.runs.findIndex((r) => r.id === keepId) : -1;
  await selectRun(at >= 0 ? at : state.runs.length - 1);
}

function renderStrip() {
  const strip = $("#bf-strip");
  strip.textContent = "";
  let lastDay = null;
  state.runs.forEach((r, i) => {
    const activity = (r.steps || 0) + (r.calls || 0);
    const t = el("button", `bf-tick trigger-${r.trigger}${activity ? "" : " idle"}${r.error ? " error" : ""}`);
    t.type = "button";
    const d = day(r.started_at);
    if (lastDay && d !== lastDay) t.classList.add("day-start");
    lastDay = d;
    t.style.height = `${Math.min(42, 4 + Math.sqrt(activity) * 7)}px`;
    t.title = `${d} ${clock(r.started_at)} · ${r.trigger} · ${r.calls} calls · ${r.steps} steps${r.error ? " · error" : ""}`;
    t.addEventListener("click", () => selectRun(i));
    strip.appendChild(t);
  });
}

function renderRunHeader() {
  const box = $("#bf-run");
  box.textContent = "";
  const r = state.run;
  if (!r) {
    box.appendChild(el("span", "bf-empty", state.concern ? "no wake touched this concern yet" : "no wakes recorded yet"));
    return;
  }
  box.appendChild(el("b", null, `${day(r.started_at)} ${clock(r.started_at)}`));
  box.appendChild(el("span", `bf-tag trigger-${r.trigger}`, r.trigger));
  if (r.ended_at) {
    const secs = (new Date(r.ended_at) - new Date(r.started_at)) / 1000;
    box.appendChild(el("span", "bf-tag", `${secs < 10 ? secs.toFixed(1) : Math.round(secs)}s`));
  }
  box.appendChild(el("span", "bf-tag", `${r.calls.length} calls · ${r.steps.length} steps`));
  if (r.error) box.appendChild(el("span", "bf-tag bad", r.error));
  box.appendChild(el("span", "bf-sub", `wake ${state.index + 1} of ${state.runs.length}`));
}

async function selectRun(i) {
  if (!state.runs.length) {
    state.index = -1; state.run = null;
  } else {
    state.index = Math.max(0, Math.min(i, state.runs.length - 1));
    state.run = await getJSON(`/api/brain/runs/${encodeURIComponent(state.runs[state.index].id)}`);
  }
  document.querySelectorAll(".bf-tick").forEach((t, k) => t.classList.toggle("is-current", k === state.index));
  const cur = document.querySelectorAll(".bf-tick")[state.index];
  if (cur) cur.scrollIntoView({ block: "nearest", inline: "nearest" });
  $("#bf-prev").disabled = state.index <= 0;
  $("#bf-next").disabled = state.index >= state.runs.length - 1;
  renderRunHeader();
  paintNodes();
  if (state.open) openNode(state.open);
  saveUrl();
}

function renderFilter() {
  const box = $("#bf-filter");
  box.textContent = "";
  box.classList.toggle("hidden", !state.concern);
  if (!state.concern) return;
  const title = (state.run && state.run.concerns && state.run.concerns[state.concern] || {}).title;
  box.appendChild(el("span", null, `Showing the wakes that touched: ${title || state.concern}`));
  const clear = el("button", null, "show every wake");
  clear.type = "button";
  clear.addEventListener("click", () => setConcern(null));
  box.appendChild(clear);
}

async function setConcern(cid) {
  state.concern = cid;
  await loadRuns(state.run && state.run.id);
  renderFilter();
}

function saveUrl() {
  const p = new URLSearchParams();
  if (state.run) p.set("run", state.run.id);
  if (state.concern) p.set("concern", state.concern);
  if (state.open) p.set("node", state.open);
  history.replaceState(null, "", `${location.pathname}?${p}`);
}

// ── a stage opened ──────────────────────────────────────────────────────────

function textSection(title, text) {
  const d = el("details", "bf-section");
  const s = el("summary");
  s.appendChild(el("span", null, title));
  s.appendChild(el("span", "bf-size", size((text || "").length)));
  d.appendChild(s);
  d.appendChild(el("pre", "bf-pre", text || "(empty)"));
  return d;
}

function subjectLabel(subject) {
  if (!subject) return "";
  const c = state.run && state.run.concerns && state.run.concerns[subject];
  if (c) return c.title || subject;
  if (subject.startsWith("[")) return `events ${JSON.parse(subject).join(", ")}`;
  return subject;
}

function concernLink(cid) {
  const b = el("button", "bf-link", "wakes that touched it");
  b.type = "button";
  b.addEventListener("click", (ev) => { ev.preventDefault(); setConcern(cid); });
  return b;
}

function valueNode(v) {
  if (v == null) return el("span", "bf-empty", "none");
  if (typeof v === "object") {
    const text = JSON.stringify(v, null, 2);
    if (text.length < 140) return el("span", null, text.replace(/\s+/g, " "));
    const d = el("details", "bf-section");
    const s = el("summary");
    s.appendChild(el("span", null, Array.isArray(v) ? `${v.length} items` : `${Object.keys(v).length} fields`));
    s.appendChild(el("span", "bf-size", size(text.length)));
    d.appendChild(s);
    d.appendChild(el("pre", "bf-pre", text));
    return d;
  }
  return el("span", null, String(v));
}

function stepRow(s) {
  const d = el("details", `bf-item step${isRefused(s) ? " refused" : ""}${isFailed(s) ? " failed" : ""}`);
  if (state.concern && s.subject === state.concern) d.classList.add("is-focus");
  const sum = el("summary");
  sum.appendChild(el("span", "bf-time", clock(s.at)));
  sum.appendChild(el("span", "bf-what", s.what.replace(/_/g, " ")));
  const label = subjectLabel(s.subject) || s.detail.title || s.detail.candidate;
  if (label) sum.appendChild(el("span", "bf-subject", label));
  d.appendChild(sum);
  const body = el("div", "bf-body");
  const kv = el("dl", "bf-kv");
  Object.entries(s.detail).forEach(([k, v]) => {
    kv.appendChild(el("dt", null, k.replace(/_/g, " ")));
    const dd = el("dd");
    dd.appendChild(valueNode(v));
    kv.appendChild(dd);
  });
  if (s.subject && state.run.concerns[s.subject]) {
    kv.appendChild(el("dt", null, "concern"));
    const dd = el("dd");
    dd.appendChild(el("span", null, `${s.subject} `));
    dd.appendChild(concernLink(s.subject));
    kv.appendChild(dd);
  }
  body.appendChild(kv);
  d.appendChild(body);
  if (isRefused(s) || isFailed(s)) d.open = true;
  return d;
}

function callRow(c) {
  const d = el("details", "bf-item call");
  if (state.concern && c.trace && c.trace.concern_id === state.concern) d.classList.add("is-focus");
  const s = el("summary");
  s.appendChild(el("span", "bf-time", clock(c.at)));
  s.appendChild(el("span", "bf-what", short(c.agent)));
  const about = c.trace && c.trace.concern_id ? subjectLabel(c.trace.concern_id) : "";
  if (about) s.appendChild(el("span", "bf-subject", about));
  if (c.items) s.appendChild(el("span", "bf-subject", `answers ${c.items.length} handoff${c.items.length > 1 ? "s" : ""}`));
  s.appendChild(el("span", "bf-size", `${Math.round((c.duration_ms || 0) / 100) / 10}s · ${c.engine || ""} · user ${size(c.user_chars)}`));
  if (c.error) s.appendChild(el("span", "bf-err", "error"));
  d.appendChild(s);
  const body = el("div", "bf-body");
  d.appendChild(body);
  d.addEventListener("toggle", async () => {
    if (!d.open || body.dataset.loaded) return;
    body.dataset.loaded = "1";
    body.appendChild(el("div", "bf-empty", "loading…"));
    try {
      const full = await getJSON(`/api/brain/calls/${encodeURIComponent(c.id)}`);
      body.textContent = "";
      body.appendChild(textSection("System prompt", full.system));
      body.appendChild(textSection("User prompt", full.user));
      const out = textSection(full.error ? "Error" : "Answer", full.error || JSON.stringify(full.result, null, 2));
      out.open = true;
      body.appendChild(out);
    } catch (e) {
      body.textContent = "";
      body.appendChild(el("div", "bf-err", String(e)));
    }
  });
  return d;
}

function concernsPanel(panel) {
  const run = state.run;
  const ids = Object.keys((run && run.concerns) || {});
  if (!ids.length) {
    panel.appendChild(el("p", "bf-empty", "No concern was touched in this wake."));
    return;
  }
  ids.forEach((cid) => {
    const c = run.concerns[cid];
    panel.appendChild(el("div", "bf-h", `${c.title || cid} · ${c.status}${c.kind === "standing" ? " · standing" : ""}`));
    const head = el("div");
    head.appendChild(concernLink(cid));
    panel.appendChild(head);
    const about = [...run.steps.filter((s) => s.subject === cid).map((s) => ({ ...s, kind: "step" })),
      ...run.calls.filter((x) => x.trace && x.trace.concern_id === cid).map((x) => ({ ...x, kind: "call" }))]
      .sort((a, b) => (a.at < b.at ? -1 : 1));
    about.forEach((x) => panel.appendChild(x.kind === "step" ? stepRow(x) : callRow(x)));
  });
}

function openNode(id) {
  state.open = id;
  const node = state.nodes.find((n) => n.id === id);
  document.querySelectorAll(".bf-node").forEach((b) => b.classList.toggle("is-open", b.id === `node-${id}`));
  const panel = $("#bf-detail");
  panel.classList.remove("hidden");
  panel.textContent = "";
  const close = el("button", "bf-close", "close");
  close.type = "button";
  close.addEventListener("click", () => {
    panel.classList.add("hidden");
    state.open = null;
    document.querySelectorAll(".bf-node").forEach((b) => b.classList.remove("is-open"));
    saveUrl();
    drawEdges();
  });
  panel.appendChild(close);
  panel.appendChild(el("h3", null, node.title));
  panel.appendChild(el("p", "bf-para", node.role));
  if (node.agents.length) {
    const chips = el("div", "bf-chips");
    node.agents.forEach((a) => chips.appendChild(el("span", "bf-chip agent", `${short(a.name)}${a.engine ? " · " + a.engine : ""}`)));
    panel.appendChild(chips);
  }
  if (!state.run) {
    panel.appendChild(el("p", "bf-empty", "No wake chosen."));
  } else if (id === "concerns") {
    panel.appendChild(el("div", "bf-h", `Concerns touched in the wake at ${clock(state.run.started_at)}`));
    concernsPanel(panel);
  } else {
    panel.appendChild(el("div", "bf-h", `In the wake at ${clock(state.run.started_at)}`));
    const items = itemsOf(id);
    if (!items.length) panel.appendChild(el("p", "bf-empty", "Nothing passed through this stage in this wake."));
    items.forEach((x) => panel.appendChild(x.kind === "step" ? stepRow(x) : callRow(x)));
  }
  panel.scrollTop = 0;
  saveUrl();
  drawEdges();
}

// ── start ───────────────────────────────────────────────────────────────────

$("#bf-prev").addEventListener("click", () => selectRun(state.index - 1));
$("#bf-next").addEventListener("click", () => selectRun(state.index + 1));
$("#bf-latest").addEventListener("click", () => loadRuns(null));
$("#bf-idle").addEventListener("change", () => loadRuns(state.run && state.run.id));
document.addEventListener("keydown", (e) => {
  if (e.target.closest("input, textarea, select")) return;
  if (e.key === "ArrowLeft") selectRun(state.index - 1);
  if (e.key === "ArrowRight") selectRun(state.index + 1);
});
window.addEventListener("resize", drawEdges);
setInterval(() => {
  if ($("#bf-follow").checked) loadRuns(null);
}, 15000);

(async () => {
  try {
    const flow = await getJSON("/api/brain/flow");
    state.nodes = flow.nodes;
    state.edges = flow.edges;
    renderNodes();
    const p = new URLSearchParams(location.search);
    state.concern = p.get("concern");
    state.open = p.get("node");
    await loadRuns(p.get("run"));
    renderFilter();
  } catch (e) {
    $("#bf-run").textContent = "";
    $("#bf-run").appendChild(el("span", "bf-err", `Could not load: ${e}`));
  }
})();
