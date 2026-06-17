"use strict";
const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
};
let CURRENT = null;
let CURRENT_ARCHIVED = false;

function fillSelects(domains, counts, kinds) {
  const fd = $("#f-domain");
  const keep = fd.value;
  fd.length = 1;
  domains.forEach((d) => {
    const n = counts && counts[d] ? ` (${counts[d]})` : "";
    fd.appendChild(new Option(d + n, d));
  });
  fd.value = keep;

  const fk = $("#f-kind");
  const keepK = fk.value;
  fk.length = 1;
  (kinds || []).forEach((k) => fk.appendChild(new Option(k, k)));
  fk.value = keepK;

  const dd = $("#d-domain");
  dd.innerHTML = "";
  domains.forEach((d) => dd.appendChild(new Option(d, d)));
}

async function load() {
  const p = new URLSearchParams();
  const q = $("#f-q").value.trim();
  if (q) p.set("q", q);
  if ($("#f-domain").value) p.set("domain", $("#f-domain").value);
  if ($("#f-kind").value) p.set("kind", $("#f-kind").value);
  if ($("#f-status").value) p.set("status", $("#f-status").value);
  if ($("#f-archived").checked) p.set("include_archived", "1");

  const r = await fetch("/api/beliefs/list?" + p.toString());
  const data = await r.json();
  fillSelects(data.domains || window.BX_DOMAINS, data.domain_counts, data.kinds);
  $("#bx-count").textContent = data.count;
  renderList(data.beliefs || []);
}

function renderList(beliefs) {
  const list = $("#bx-list");
  list.innerHTML = "";
  if (!beliefs.length) {
    list.appendChild(el("div", "bx-empty", "No beliefs match."));
    return;
  }
  beliefs.forEach((b) => {
    const row = el("div", "bx-item" + (b.archived ? " suppressed" : ""));
    const main = el("div", "bx-item-main");
    main.appendChild(el("div", "bx-stmt", b.statement));
    const tags = el("div", "bx-tags");
    if (b.short_id) tags.appendChild(el("span", "bx-badge sid", b.short_id));
    if (b.domain) tags.appendChild(el("span", "bx-badge dom", b.domain));
    if (b.kind) tags.appendChild(el("span", "bx-badge kind", b.kind));
    if (b.tags) String(b.tags).split(",").forEach((tg) => tags.appendChild(el("span", "bx-badge tag", tg)));
    tags.appendChild(el("span", "bx-badge obs", `${b.obs_count || 0}× · net ${b.net ?? 0}`));
    if (b.last_observed) tags.appendChild(el("span", "bx-badge date", String(b.last_observed).slice(0, 10)));
    if (b.locked) tags.appendChild(el("span", "bx-badge lock", "🔒 locked"));
    if (b.status && b.status !== "active") tags.appendChild(el("span", "bx-badge supp", b.status));
    if (b.archived) tags.appendChild(el("span", "bx-badge supp", "archived"));
    main.appendChild(tags);
    row.appendChild(main);
    row.addEventListener("click", () => openDrawer(b.belief_id));
    list.appendChild(row);
  });
}

function renderTagPicker(selected, readonly) {
  const box = $("#d-tags");
  box.innerHTML = "";
  const sel = new Set(selected || []);
  (window.BX_TAGS || []).forEach((t) => {
    const chip = el("span", "bx-tagchip" + (sel.has(t) ? " on" : ""), t);
    if (!readonly) chip.addEventListener("click", () => chip.classList.toggle("on"));
    box.appendChild(chip);
  });
}

async function openDrawer(id) {
  const r = await fetch("/api/beliefs/item?belief_id=" + encodeURIComponent(id));
  const data = await r.json();
  if (data.error) { alert(data.error); return; }
  CURRENT = id;
  const b = data.belief;
  CURRENT_ARCHIVED = !!data.archived;
  const stmt = b.statement || "";
  const titleTxt = stmt.length > 64 ? stmt.slice(0, 64) + "…" : stmt;
  $("#d-title").textContent = (data.short_id ? data.short_id + "  ·  " : "") + (titleTxt || "Belief")
    + (CURRENT_ARCHIVED ? "   (archived — read only)" : "");

  $("#d-meta").innerHTML = "";
  [["kind", b.kind], ["status", b.status], ["confidence", b.current_confidence_band || b.confidence],
   ["net", b.current_net_weight], ["support", b.current_support_weight],
   ["contradiction", b.current_contradiction_weight], ["obs", b.observation_count],
   ["first", String(b.first_observed || "").slice(0, 10)],
   ["last", String(b.last_confirmed || "").slice(0, 10)]].forEach(([k, v]) => {
    const s = el("span", "bx-metaitem");
    s.innerHTML = `<b>${k}</b> ${v ?? "—"}`;
    $("#d-meta").appendChild(s);
  });

  $("#d-statement").value = stmt;
  $("#d-domain").value = data.domain || "general";
  $("#d-suppressed").checked = b.status === "deprecated";
  $("#d-locked").checked = !!b.locked;
  renderTagPicker(data.tags || [], CURRENT_ARCHIVED);

  // Archived beliefs are cold storage — read-only.
  ["d-statement", "d-domain", "d-suppressed", "d-locked", "d-save"].forEach((i) => {
    $("#" + i).disabled = CURRENT_ARCHIVED;
  });

  const ev = $("#d-evidence");
  ev.innerHTML = "";
  if (!(data.evidence || []).length) ev.appendChild(el("div", "bx-empty", "No evidence recorded."));
  (data.evidence || []).forEach((e) => {
    const row = el("div", "bx-ev");
    const head = el("div", "bx-ev-head");
    head.appendChild(el("span", "bx-ev-pol " + (e.signal_type || ""), e.signal_type || ""));
    head.appendChild(el("span", "bx-ev-src", e.source_type || ""));
    head.appendChild(el("span", "bx-ev-date", String(e.source_date || "").slice(0, 10)));
    row.appendChild(head);
    row.appendChild(el("div", "bx-ev-text", e.raw_text || e.summary || ""));
    ev.appendChild(row);
  });

  $("#d-saved").classList.add("hidden");
  $("#bx-drawer").classList.remove("hidden");
}

async function save() {
  if (!CURRENT || CURRENT_ARCHIVED) return;
  const body = {
    belief_id: CURRENT,
    domain: $("#d-domain").value,
    suppressed: $("#d-suppressed").checked,
    locked: $("#d-locked").checked,
    statement: $("#d-statement").value,
    tags: [...document.querySelectorAll("#d-tags .bx-tagchip.on")].map((c) => c.textContent),
  };
  const r = await fetch("/api/beliefs/update", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json();
  if (data.error) { alert(data.error); return; }
  $("#d-saved").classList.remove("hidden");
  load();
}

// ── Trends tab ──────────────────────────────────────────────────────────────
let TRENDS_LOADED = false;

function showTab(name) {
  document.querySelectorAll(".bx-tab").forEach((t) =>
    t.classList.toggle("is-active", t.dataset.tab === name));
  $("#bx-browse").classList.toggle("hidden", name !== "browse");
  $("#bx-trends").classList.toggle("hidden", name !== "trends");
  if (name === "trends" && !TRENDS_LOADED) loadTrends();
}

async function loadTrends() {
  const days = $("#t-window").value;
  const r = await fetch("/api/beliefs/trends?days=" + encodeURIComponent(days));
  const data = await r.json();
  TRENDS_LOADED = true;
  $("#t-window-label").textContent = `over the last ${data.window_days} days`;
  renderTrend("#t-up", data.trending_up, "up");
  renderTrend("#t-challenged", data.challenged, "warn");
  renderTrend("#t-changed", data.recently_changed, "gone");
}

function renderTrend(sel, rows, mode) {
  const c = $(sel);
  c.innerHTML = "";
  if (!rows || !rows.length) {
    const msg = mode === "up" ? "Nothing gaining ground in this window."
      : mode === "warn" ? "Nothing losing ground — steady."
      : "Nothing recently faded.";
    c.appendChild(el("div", "bx-empty", msg));
    return;
  }
  rows.forEach((b) => {
    const row = el("div", "bx-item");
    const main = el("div", "bx-item-main");
    main.appendChild(el("div", "bx-stmt", b.statement));
    const tags = el("div", "bx-tags");
    if (b.domain) tags.appendChild(el("span", "bx-badge dom", b.domain));
    if (mode === "gone") {
      tags.appendChild(el("span", "bx-badge", "faded"));
      if (b.changed_on) tags.appendChild(el("span", "bx-badge date", String(b.changed_on).slice(0, 10)));
    } else {
      tags.appendChild(el("span", "bx-badge " + (mode === "warn" ? "netdown" : "netup"),
        `+${b.confirms || 0} / -${b.challenges || 0}`));
      tags.appendChild(el("span", "bx-badge obs", `${b.obs_count || 0}× total`));
    }
    main.appendChild(tags);
    row.appendChild(main);
    row.addEventListener("click", () => openDrawer(b.belief_id));
    c.appendChild(row);
  });
}

$("#f-refresh").addEventListener("click", load);
$("#f-q").addEventListener("keydown", (e) => { if (e.key === "Enter") load(); });
["f-domain", "f-kind", "f-status", "f-archived"].forEach((id) =>
  $("#" + id).addEventListener("change", load));
$("#bx-close").addEventListener("click", () => $("#bx-drawer").classList.add("hidden"));
$("#d-save").addEventListener("click", save);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#bx-drawer").classList.add("hidden"); });

document.querySelectorAll(".bx-tab").forEach((t) =>
  t.addEventListener("click", () => showTab(t.dataset.tab)));
$("#t-refresh").addEventListener("click", loadTrends);
$("#t-window").addEventListener("change", loadTrends);

load();
