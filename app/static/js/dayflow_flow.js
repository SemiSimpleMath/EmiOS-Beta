"use strict";
// /dayflow/flow: the three passes read from their configs; a stage opens its agents' recent calls.
const BASE = window.SCRIPT_NAME || "";
const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
};
const when = (iso) => (iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "medium" }) : "");
const short = (name) => name.replace(/^dayflow_orchestrator::/, "");
const size = (n) => (n == null ? "" : n >= 1000 ? `${(n / 1000).toFixed(1)}k chars` : `${n} chars`);

const INPUTS = [
  ["The brain", "hands over concerns ready to act on, as intake items the steward must answer"],
  ["Admitted pods", "triaged intake from outside sources"],
  ["The scheduler", "starts a planning pass (intake, work progress, 30-minute ceiling) or a wake pass (a task's time)"],
];
const OUTPUTS = [
  ["The user", "tickets: questions and notices, answered in the popup"],
  ["The world", "the worker's actions: email, calendar, devices, research"],
  ["Concerns and the brain", "every finalizer judgment and each ending of work attached to a concern"],
];

async function getJSON(url) {
  const r = await fetch(BASE + url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

function edge(label, items) {
  const row = el("div", "df-edge");
  row.appendChild(el("div", "df-edge-label", label));
  items.forEach(([title, text]) => {
    const box = el("div", "df-io");
    box.appendChild(el("b", null, title));
    box.appendChild(el("span", null, text));
    row.appendChild(box);
  });
  return row;
}

function stageButton(stage, lane) {
  const calls = stage.agents.length > 0 || stage.work_calls;
  const b = el("button", `df-stage ${stage.kind}${stage.kind === "control" && calls ? " calls" : ""}`);
  b.type = "button";
  b.appendChild(el("div", "df-stage-name", stage.kind === "agent" ? short(stage.name) : stage.name));
  if (stage.kind === "agent" && stage.description) b.appendChild(el("div", "df-stage-desc", stage.description));
  const chips = el("div", "df-chips");
  stage.agents.forEach((a) => {
    if (stage.kind === "control") chips.appendChild(el("span", "df-chip", `calls ${short(a.name)}`));
    if (a.engine) chips.appendChild(el("span", "df-chip engine", a.engine));
  });
  if (stage.work_calls) chips.appendChild(el("span", "df-chip", "worker / ticket builder"));
  if (chips.childNodes.length) b.appendChild(chips);
  if (stage.link) b.appendChild(el("div", "df-stage-link", "↳ " + stage.link));
  b.addEventListener("click", () => openStage(stage, lane, b));
  return b;
}

function renderDiagram(data) {
  const root = $("#df-diagram");
  root.textContent = "";
  root.appendChild(edge("Into dayflow", INPUTS));
  const lanes = el("div", "df-lanes");
  data.managers.forEach((m) => {
    const lane = el("section", "df-lane");
    lane.appendChild(el("h2", null, m.title));
    lane.appendChild(el("div", "df-lane-name", m.name));
    lane.appendChild(el("div", "df-lane-about", m.about));
    const stages = el("div", "df-stages");
    m.stages.forEach((s, i) => {
      if (i) stages.appendChild(el("div", "df-arrow", "↓"));
      stages.appendChild(stageButton(s, m));
    });
    lane.appendChild(stages);
    lanes.appendChild(lane);
  });
  root.appendChild(lanes);
  root.appendChild(edge("Out of dayflow", OUTPUTS));
}

function textSection(title, text) {
  const d = el("details", "df-section");
  const s = el("summary");
  s.appendChild(el("span", null, title));
  s.appendChild(el("span", "df-size", size((text || "").length)));
  d.appendChild(s);
  d.appendChild(el("pre", "df-pre", text || "(empty)"));
  return d;
}

function callRow(c, showAgent) {
  const d = el("details", "df-call");
  const s = el("summary");
  s.appendChild(el("span", null, when(c.at)));
  if (showAgent) s.appendChild(el("span", "df-chip", short(c.agent)));
  if (c.trace && c.trace.work_id) s.appendChild(el("span", "df-chip engine", c.trace.work_id));
  s.appendChild(el("span", "df-size", `${Math.round((c.duration_ms || 0) / 100) / 10}s · user ${size(c.user_chars)}`));
  if (c.error) s.appendChild(el("span", "df-err", "error"));
  d.appendChild(s);
  const body = el("div", "df-call-body");
  d.appendChild(body);
  d.addEventListener("toggle", async () => {
    if (!d.open || body.dataset.loaded) return;
    body.dataset.loaded = "1";
    body.appendChild(el("div", "df-empty", "loading…"));
    try {
      const full = await getJSON(`/api/brain/calls/${encodeURIComponent(c.id)}`);
      body.textContent = "";
      body.appendChild(textSection("System prompt", full.system));
      body.appendChild(textSection("User prompt", full.user));
      body.appendChild(textSection(full.error ? "Error" : "Result",
        full.error || JSON.stringify(full.result, null, 2)));
      body.querySelectorAll(".df-section")[2].open = true;
    } catch (e) {
      body.textContent = "";
      body.appendChild(el("div", "df-err", String(e)));
    }
  });
  return d;
}

async function callsBlock(title, url, showAgent) {
  const box = el("div");
  box.appendChild(el("div", "df-h", title));
  const list = el("div");
  list.appendChild(el("div", "df-empty", "loading…"));
  box.appendChild(list);
  getJSON(url).then(({ calls }) => {
    list.textContent = "";
    if (!calls.length) list.appendChild(el("div", "df-empty", "no calls recorded yet"));
    calls.forEach((c, i) => {
      const row = callRow(c, showAgent);
      list.appendChild(row);
      if (i === 0) row.open = true;
    });
  }).catch((e) => { list.textContent = ""; list.appendChild(el("div", "df-err", String(e))); });
  return box;
}

async function openStage(stage, lane, button) {
  document.querySelectorAll(".df-stage.is-open").forEach((b) => b.classList.remove("is-open"));
  button.classList.add("is-open");
  const panel = $("#df-detail");
  panel.classList.remove("hidden");
  panel.textContent = "";
  const close = el("button", "df-close", "close");
  close.type = "button";
  close.addEventListener("click", () => { panel.classList.add("hidden"); button.classList.remove("is-open"); });
  panel.appendChild(close);
  panel.appendChild(el("h3", null, stage.name));
  panel.appendChild(el("div", "df-kind", `${stage.kind === "agent" ? "agent (a model call)" : "control node (code)"} · ${lane.title}`));
  if (stage.description) panel.appendChild(el("p", "df-para", stage.description));
  if (stage.link) panel.appendChild(el("p", "df-para", "↳ " + stage.link));
  for (const a of stage.agents) {
    if (stage.kind === "control" && a.role) panel.appendChild(el("p", "df-para", `${short(a.name)}: ${a.role}`));
    panel.appendChild(await callsBlock(`Recent calls · ${short(a.name)}${a.engine ? " · " + a.engine : ""}`,
      `/api/dayflow/calls?agent=${encodeURIComponent(a.name)}&limit=10`, false));
  }
  if (stage.work_calls) {
    panel.appendChild(await callsBlock("Recent calls inside work attempts (the worker, the ticket builder)",
      "/api/dayflow/calls?work=1&limit=20", true));
  }
  if (!stage.agents.length && !stage.work_calls) {
    panel.appendChild(el("p", "df-empty", "This stage is code: it makes no model call."));
  }
  panel.scrollTop = 0;
  if (window.matchMedia("(max-width:1100px)").matches) panel.scrollIntoView({ behavior: "smooth" });
}

(async () => {
  try {
    renderDiagram(await getJSON("/api/dayflow/flow"));
  } catch (e) {
    $("#df-diagram").textContent = "";
    $("#df-diagram").appendChild(el("div", "df-status df-err", `Could not load: ${e}`));
  }
})();
