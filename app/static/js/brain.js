"use strict";
// Subpath-safe: the page may be served under EMI_PROXY_SUBPATH (see api_keys_settings.js).
const BASE = window.SCRIPT_NAME || "";
const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
};
const fmtChars = (n) => `${n.toLocaleString()} chars · ~${Math.round(n / 4).toLocaleString()} tokens`;

function section(title, text, open) {
  const d = el("details", "br-section");
  if (open) d.open = true;
  const s = el("summary");
  s.appendChild(el("span", null, title));
  s.appendChild(el("span", "br-size", fmtChars(text.length)));
  d.appendChild(s);
  d.appendChild(el("pre", "br-pre", text || "(empty)"));
  return d;
}

// ── Inbox ───────────────────────────────────────────────────────────────────
async function loadInbox() {
  const route = $("#i-route").value;
  const r = await fetch(BASE + "/api/brain/inbox?limit=500" + (route ? "&route=" + encodeURIComponent(route) : ""));
  const data = await r.json();
  $("#i-counts").textContent = Object.entries(data.counts || {}).map(([k, v]) => `${k}: ${v}`).join(" · ");
  const list = $("#i-list");
  list.innerHTML = "";
  if (!(data.events || []).length) { list.appendChild(el("div", "br-empty", "No events.")); return; }
  data.events.forEach((e) => {
    const box = el("div", "br-ev");
    const head = el("div", "br-ev-head");
    const kind = e.gate_status === "routed" ? e.route : e.gate_status;
    head.appendChild(el("span", "br-badge " + kind, kind));
    head.appendChild(el("span", null, `${e.occurred_at} · ${e.room_id || e.source} · ${e.speaker || "user"} · ${e.source_ref}`));
    if (e.consumed_at) head.appendChild(el("span", "br-badge", e.brain_decision ? "read by brain" : "read by noticer"));
    box.appendChild(head);
    box.appendChild(el("div", "br-ev-text", e.text));
    const meta = el("div", "br-ev-meta");
    const line = (label, value) => {
      if (!value) return;
      const d = el("div");
      d.appendChild(el("b", null, label + " "));
      d.appendChild(document.createTextNode(value));
      meta.appendChild(d);
    };
    line("concerns:", (e.concerns || []).map((c) => `${c.title} (${c.id.slice(0, 8)})`).join("; "));
    line("gate:", e.gate_reasoning);
    line("gate error:", e.gate_error);
    line("brain:", e.brain_decision ? `${e.brain_decision} — ${e.brain_reason || ""}` : "");
    line("brain error:", e.brain_error);
    line("noticer:", e.noticer_decision ? `${e.noticer_decision} — ${e.noticer_reason || ""}` : "");
    box.appendChild(meta);
    list.appendChild(box);
  });
}

// ── Noticer ─────────────────────────────────────────────────────────────────
async function loadNoticer() {
  const body = $("#n-body");
  body.innerHTML = "";
  $("#n-status").textContent = "building…";
  const r = await fetch(BASE + "/api/brain/noticer");
  const data = await r.json();
  if (data.error || !r.ok) { $("#n-status").textContent = "failed: " + (data.error || r.status); return; }
  const total = (data.inputs || []).reduce((a, i) => a + i.chars, 0);
  $("#n-status").textContent = `${data.inputs.length} inputs · ${fmtChars(total)} · ${data.reports_unconsumed} event(s) waiting for the brain`;
  body.appendChild(el("div", "br-h", "Inputs (largest first)"));
  [...data.inputs].sort((a, b) => b.chars - a.chars).forEach((i) => body.appendChild(section(i.key, i.text, false)));
  body.appendChild(el("div", "br-h", "Rendered prompts"));
  body.appendChild(section("system prompt", data.system, false));
  body.appendChild(section("user prompt", data.user, false));
}

// ── Gate ────────────────────────────────────────────────────────────────────
async function loadGate() {
  const body = $("#g-body");
  body.innerHTML = "";
  $("#g-status").textContent = "rendering…";
  const r = await fetch(BASE + "/api/brain/gate?n=" + encodeURIComponent($("#g-n").value));
  const data = await r.json();
  if (data.error || !r.ok) { $("#g-status").textContent = "failed: " + (data.error || r.status); return; }
  $("#g-status").textContent = `events: ${data.events_source} · open concerns: ${data.concern_count}`;
  body.appendChild(section("system prompt", data.system, true));
  body.appendChild(section("user prompt", data.user, true));
}

// ── Recorded calls (shared by every view) ─────────────────────────────────────
const when = (iso) => (iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "");
const pretty = (v) => (typeof v === "string" ? v : JSON.stringify(v, null, 2));

function callRow(c) {
  const d = el("details", "br-call");
  const s = el("summary");
  s.appendChild(el("span", "br-badge agent", c.agent.replace("subconscious::", "").replace("dayflow_orchestrator::", "dayflow ")));
  s.appendChild(el("span", null, when(c.at)));
  if (c.trace && c.trace.stage) s.appendChild(el("span", "br-note", "stage: " + c.trace.stage));
  s.appendChild(el("span", "br-note", `${c.engine || "?"} · ${((c.duration_ms || 0) / 1000).toFixed(1)} s · ` +
    `system ${c.system_chars.toLocaleString()} · user ${c.user_chars.toLocaleString()} chars`));
  if (c.error) s.appendChild(el("span", "br-err", "error"));
  d.appendChild(s);
  const body = el("div", "br-call-body");
  d.appendChild(body);
  d.addEventListener("toggle", async () => {
    if (!d.open || body.childElementCount) return;
    body.appendChild(el("div", "br-note", "loading…"));
    const r = await fetch(BASE + "/api/brain/calls/" + encodeURIComponent(c.id));
    const full = await r.json();
    body.innerHTML = "";
    body.appendChild(section("system prompt", full.system, false));
    body.appendChild(section("user prompt", full.user, true));
    if (full.error) body.appendChild(section("error", full.error, true));
    else body.appendChild(section("result", pretty(full.result), true));
  });
  return d;
}

function card(title, badges, fill) {
  const d = el("details", "br-card");
  const s = el("summary");
  s.appendChild(el("span", "br-title", title));
  badges.filter(Boolean).forEach(([cls, text]) => s.appendChild(el("span", "br-badge " + cls, text)));
  d.appendChild(s);
  const body = el("div", "br-card-body");
  d.appendChild(body);
  fill(body);
  return d;
}

function kv(pairs) {
  const dl = el("dl", "br-kv");
  pairs.filter(([, v]) => v !== undefined && v !== null && v !== "" && !(Array.isArray(v) && !v.length)).forEach(([k, v]) => {
    dl.appendChild(el("dt", null, k));
    dl.appendChild(el("dd", null, Array.isArray(v) ? v.join("\n") : String(v)));
  });
  return dl;
}

// ── What the brain did: one matter at a time ─────────────────────────────────
async function loadMatters() {
  const list = $("#m-list");
  list.innerHTML = "";
  const r = await fetch(BASE + "/api/brain/matters?limit=50");
  const data = await r.json();
  if (!data.matters.length) { list.appendChild(el("div", "br-empty", "The brain has not handled a matter yet.")); return; }
  data.matters.forEach((m) => {
    const first = m.events[0] || {};
    const title = `${when(m.created_at)} · ${m.events.length} event(s) · ${first.source === "email" ? "email" : first.room_id || "?"}`;
    list.appendChild(card(title, [[m.status, m.status]], (body) => {
      body.appendChild(el("div", "br-h", "Events read"));
      m.events.forEach((e) => {
        const box = el("div", "br-ev");
        const head = el("div", "br-ev-head");
        head.appendChild(el("span", "br-badge " + (e.route || e.gate_status), e.route || e.gate_status));
        head.appendChild(el("span", null, `${when(e.occurred_at)} · ${e.speaker || "user"} · ${e.source_ref}`));
        box.appendChild(head);
        box.appendChild(el("div", "br-ev-text", e.text));
        if (e.gate_reasoning) box.appendChild(el("div", "br-ev-meta", "gate: " + e.gate_reasoning));
        body.appendChild(box);
      });
      body.appendChild(el("div", "br-h", "Calls"));
      if (!m.calls.length) body.appendChild(el("div", "br-note", "No recorded calls (handled before recording began)."));
      m.calls.forEach((c) => body.appendChild(callRow(c)));
      body.appendChild(el("div", "br-h", "Decided"));
      if (m.error) body.appendChild(el("div", "br-err", m.error));
      const d = m.decisions || {};
      Object.entries(d.events || {}).forEach(([ref, v]) => {
        const row = el("div", "br-decision");
        row.appendChild(el("b", null, v.decision + " "));
        row.appendChild(document.createTextNode(`${ref}: ${v.reason}`));
        body.appendChild(row);
      });
      (d.concern_updates || []).forEach((u) => body.appendChild(el("div", "br-decision", `${u.action} concern ${u.concern_id.slice(0, 8)}: ${u.note}`)));
      (d.new_concerns || []).forEach((n) => body.appendChild(el("div", "br-decision",
        `new concern ${n.label}: ${n.title}` + (m.admitted && m.admitted[n.label] ? ` → ${m.admitted[n.label].slice(0, 8)}` : " (not admitted)") +
        (n.owner_request ? ` · the owner asked: "${n.owner_request.words}"` : ""))));
    }));
  });
}

// ── Concerns: brief, readiness, handoffs, work ────────────────────────────────
async function loadConcerns() {
  const list = $("#c-list");
  list.innerHTML = "";
  const r = await fetch(BASE + "/api/brain/concerns");
  const data = await r.json();
  if (!data.concerns.length) { list.appendChild(el("div", "br-empty", "No open concerns.")); return; }
  data.concerns.forEach((c) => {
    const b = c.brief || {};
    const ready = b.readiness || {};
    const badges = [[c.status, c.status],
      ready.decision && [ready.decision, ready.decision + (ready.hold_until ? " until " + ready.hold_until : "")],
      c.brief && !c.brief_current && ["pending", "brief out of date"]];
    list.appendChild(card(c.title, badges, (body) => {
      body.appendChild(kv([["about", c.subject], ["done when", c.done_when], ["origin", c.origin],
        ["the owner asked", c.owner_request && c.owner_request.words], ["notes", c.notes]]));
      body.appendChild(el("div", "br-h", "Brief"));
      if (c.brief_error) body.appendChild(el("div", "br-err", "brief failed: " + c.brief_error.error));
      if (c.brief) body.appendChild(kv([["what", b.what], ["why it matters", b.why_it_matters],
        ["known", (b.known || []).map((f) => `${f.fact} (${f.source})`)], ["tried", b.tried],
        ["the owner's wishes", b.owner_wishes], ["depends on it", b.depends_on_it], ["open questions", b.open_questions],
        ["recommendation", b.recommendation],
        ["readiness", ready.decision && `${ready.decision}${ready.task ? ": " + ready.task : ""} (${ready.why})`],
        ["written", when(b.written_at)]]));
      else body.appendChild(el("div", "br-note", "No brief yet."));
      body.appendChild(el("div", "br-h", "Brief writer's calls"));
      if (!c.brief_calls.length) body.appendChild(el("div", "br-note", "None recorded."));
      c.brief_calls.forEach((k) => body.appendChild(callRow(k)));
      body.appendChild(el("div", "br-h", "Handoffs to dayflow"));
      if (!c.handoffs.length) body.appendChild(el("div", "br-note", "Never handed over."));
      c.handoffs.forEach((h) => {
        const review = h.evaluator_review || {};
        const outcome = review.outcome
          ? ` · steward: ${review.outcome}${review.work_id ? " → " + review.work_id : ""}${review.reason ? " (" + review.reason + ")" : ""}`
          : " · waiting for the steward";
        body.appendChild(el("div", "br-decision", `${when(h.created_at)} · ${h.item_id} · ${h.state}${outcome}\ntask: ${h.summary}`));
        h.steward_calls.forEach((k) => body.appendChild(callRow(k)));
      });
      body.appendChild(el("div", "br-h", "Work serving it"));
      if (!c.work.length) body.appendChild(el("div", "br-note", "None."));
      c.work.forEach((w) => body.appendChild(el("div", "br-decision", `${w.work_id} [${w.status}] ${w.title}`)));
      body.appendChild(section("journal", c.journal, false));
    }));
  });
}

// ── Every recorded call ───────────────────────────────────────────────────────
async function loadCalls() {
  const list = $("#k-list");
  list.innerHTML = "";
  const agent = $("#k-agent").value;
  const r = await fetch(BASE + "/api/brain/calls?limit=300" + (agent ? "&agent=" + encodeURIComponent(agent) : ""));
  const data = await r.json();
  const select = $("#k-agent");
  if (select.options.length === 1) data.agents.forEach((a) => select.appendChild(el("option", null, a)));
  $("#k-status").textContent = `${data.calls.length} call(s)`;
  if (!data.calls.length) { list.appendChild(el("div", "br-empty", "No recorded calls yet.")); return; }
  data.calls.forEach((c) => list.appendChild(callRow(c)));
}

const loaders = { matters: loadMatters, concerns: loadConcerns, calls: loadCalls, inbox: loadInbox };
const loaded = new Set();

function showTab(name) {
  document.querySelectorAll(".br-tab").forEach((t) => t.classList.toggle("is-active", t.dataset.tab === name));
  ["matters", "concerns", "calls", "inbox", "noticer", "gate"].forEach((n) => $("#br-" + n).classList.toggle("hidden", n !== name));
  if (loaders[name] && !loaded.has(name)) { loaded.add(name); loaders[name](); }
}

document.querySelectorAll(".br-tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));
$("#m-refresh").addEventListener("click", loadMatters);
$("#c-refresh").addEventListener("click", loadConcerns);
$("#k-refresh").addEventListener("click", loadCalls);
$("#k-agent").addEventListener("change", loadCalls);
$("#i-route").addEventListener("change", loadInbox);
$("#i-refresh").addEventListener("click", loadInbox);
$("#n-load").addEventListener("click", loadNoticer);
$("#g-load").addEventListener("click", loadGate);
showTab("matters");
