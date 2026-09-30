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
    head.appendChild(el("span", null, `${e.occurred_at} · ${e.room_id || "?"} · ${e.speaker || "user"} · ${e.source_ref}`));
    if (e.consumed_at) head.appendChild(el("span", "br-badge", "read by noticer"));
    box.appendChild(head);
    box.appendChild(el("div", "br-ev-text", e.text));
    if (e.replying_to) box.appendChild(el("div", "br-ev-reply", "replying to the assistant: " + e.replying_to));
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
  $("#n-status").textContent = `${data.inputs.length} inputs · ${fmtChars(total)} · ${data.reports_unconsumed} unread report(s)`;
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

function showTab(name) {
  document.querySelectorAll(".br-tab").forEach((t) => t.classList.toggle("is-active", t.dataset.tab === name));
  ["inbox", "noticer", "gate"].forEach((n) => $("#br-" + n).classList.toggle("hidden", n !== name));
}

document.querySelectorAll(".br-tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));
$("#i-route").addEventListener("change", loadInbox);
$("#i-refresh").addEventListener("click", loadInbox);
$("#n-load").addEventListener("click", loadNoticer);
$("#g-load").addEventListener("click", loadGate);
loadInbox();
