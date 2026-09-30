"use strict";
const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
};
let CURRENT = null;

function fillSelects(tagCounts, kinds) {
  const ft = $("#f-tag");
  const keep = ft.value;
  ft.length = 1;
  (window.BX_TAGS || []).forEach((t) => {
    const n = tagCounts && tagCounts[t] ? ` (${tagCounts[t]})` : "";
    ft.appendChild(new Option(t + n, t));
  });
  ft.value = keep;

  const fk = $("#f-kind");
  const keepK = fk.value;
  fk.length = 1;
  (kinds || []).forEach((k) => fk.appendChild(new Option(k, k)));
  fk.value = keepK;
}

async function load() {
  const p = new URLSearchParams();
  const q = $("#f-q").value.trim();
  if (q) p.set("q", q);
  if ($("#f-tag").value) p.set("tag", $("#f-tag").value);
  if ($("#f-kind").value) p.set("kind", $("#f-kind").value);
  if ($("#f-status").value) p.set("status", $("#f-status").value);

  const r = await fetch("/api/beliefs/list?" + p.toString());
  const data = await r.json();
  fillSelects(data.tag_counts, data.kinds);
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
    const retired = b.status === "retired";
    const row = el("div", "bx-item" + (retired ? " suppressed" : ""));
    const main = el("div", "bx-item-main");
    main.appendChild(el("div", "bx-stmt", b.statement));
    const tags = el("div", "bx-tags");
    tags.appendChild(el("span", "bx-badge sid", b.belief_id));
    if (b.kind) tags.appendChild(el("span", "bx-badge kind", b.kind));
    if (b.tags) String(b.tags).split(",").forEach((tg) => tags.appendChild(el("span", "bx-badge tag", tg)));
    tags.appendChild(el("span", "bx-badge obs", `${b.support || 0}× support` + (b.contradict ? ` · ${b.contradict}× against` : "")));
    if (b.last_observed) tags.appendChild(el("span", "bx-badge date", b.last_observed));
    if (b.parent_id) tags.appendChild(el("span", "bx-badge dom", "refines " + b.parent_id));
    if (retired) tags.appendChild(el("span", "bx-badge supp", "retired"));
    main.appendChild(tags);
    row.appendChild(main);
    row.addEventListener("click", () => openDrawer(b.belief_id));
    list.appendChild(row);
  });
}

function renderTagPicker(selected) {
  const box = $("#d-tags");
  box.innerHTML = "";
  const sel = new Set(selected || []);
  (window.BX_TAGS || []).forEach((t) => {
    const chip = el("span", "bx-tagchip" + (sel.has(t) ? " on" : ""), t);
    chip.addEventListener("click", () => chip.classList.toggle("on"));
    box.appendChild(chip);
  });
}

function familyLink(label, b) {
  const s = el("span", "bx-metaitem bx-link");
  s.appendChild(el("b", null, label + " "));
  s.appendChild(document.createTextNode(`${b.belief_id} — ${b.statement}`));
  s.addEventListener("click", () => openDrawer(b.belief_id));
  return s;
}

async function openDrawer(id) {
  const r = await fetch("/api/beliefs/item?belief_id=" + encodeURIComponent(id));
  const data = await r.json();
  if (data.error) { alert(data.error); return; }
  CURRENT = id;
  const b = data.belief;
  const stmt = b.statement || "";
  const titleTxt = stmt.length > 64 ? stmt.slice(0, 64) + "…" : stmt;
  $("#d-title").textContent = `${b.belief_id}  ·  ${titleTxt || "Belief"}`;

  $("#d-meta").innerHTML = "";
  [["kind", b.kind], ["status", b.status], ["support", b.support], ["against", b.contradict],
   ["created", b.created_day], ["last seen", b.last_observed]].forEach(([k, v]) => {
    const s = el("span", "bx-metaitem");
    s.appendChild(el("b", null, k + " "));
    s.appendChild(document.createTextNode(v ?? "—"));
    $("#d-meta").appendChild(s);
  });

  const fam = $("#d-family");
  fam.innerHTML = "";
  if (data.parent) fam.appendChild(familyLink("refines", data.parent));
  (data.children || []).forEach((c) => fam.appendChild(familyLink("refined by", c)));

  $("#d-statement").value = stmt;
  $("#d-retired").checked = b.status === "retired";
  renderTagPicker(data.tags || []);

  const ev = $("#d-evidence");
  ev.innerHTML = "";
  if (!(data.evidence || []).length) ev.appendChild(el("div", "bx-empty", "No evidence recorded."));
  (data.evidence || []).forEach((e) => {
    const row = el("div", "bx-ev");
    const head = el("div", "bx-ev-head");
    head.appendChild(el("span", "bx-ev-pol " + (e.relation || ""), e.relation || ""));
    head.appendChild(el("span", "bx-ev-src", [e.kind, e.via, e.source_ref].filter(Boolean).join(" · ")));
    head.appendChild(el("span", "bx-ev-date", e.time || e.day || ""));
    row.appendChild(head);
    if (e.in_reply_to) row.appendChild(el("div", "bx-ev-reply", "in reply to: " + e.in_reply_to));
    row.appendChild(el("div", "bx-ev-text", e.text || ""));
    ev.appendChild(row);
  });

  const rv = $("#d-revisions");
  rv.innerHTML = "";
  if (!(data.revisions || []).length) rv.appendChild(el("div", "bx-empty", "Never revised."));
  (data.revisions || []).forEach((x) => {
    const row = el("div", "bx-ev");
    const head = el("div", "bx-ev-head");
    head.appendChild(el("span", "bx-ev-date", x.day));
    head.appendChild(el("span", "bx-ev-src", x.reasoning || ""));
    row.appendChild(head);
    if (x.old_statement !== x.new_statement) {
      row.appendChild(el("div", "bx-ev-reply", "was: " + x.old_statement));
      row.appendChild(el("div", "bx-ev-text", "now: " + x.new_statement));
    }
    rv.appendChild(row);
  });

  $("#d-saved").classList.add("hidden");
  $("#bx-drawer").classList.remove("hidden");
}

async function save() {
  if (!CURRENT) return;
  const body = {
    belief_id: CURRENT,
    retired: $("#d-retired").checked,
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
  openDrawer(CURRENT);
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
      : "Nothing revised in this window.";
    c.appendChild(el("div", "bx-empty", msg));
    return;
  }
  rows.forEach((b) => {
    const row = el("div", "bx-item");
    const main = el("div", "bx-item-main");
    main.appendChild(el("div", "bx-stmt", b.statement));
    const tags = el("div", "bx-tags");
    tags.appendChild(el("span", "bx-badge sid", b.belief_id));
    if (mode === "gone") {
      tags.appendChild(el("span", "bx-badge", b.reasoning || ""));
      if (b.changed_on) tags.appendChild(el("span", "bx-badge date", b.changed_on));
    } else {
      tags.appendChild(el("span", "bx-badge " + (mode === "warn" ? "netdown" : "netup"),
        `+${b.confirms || 0} / -${b.challenges || 0}`));
    }
    main.appendChild(tags);
    row.appendChild(main);
    row.addEventListener("click", () => openDrawer(b.belief_id));
    c.appendChild(row);
  });
}

$("#f-refresh").addEventListener("click", load);
$("#f-q").addEventListener("keydown", (e) => { if (e.key === "Enter") load(); });
["f-tag", "f-kind", "f-status"].forEach((id) =>
  $("#" + id).addEventListener("change", load));
$("#bx-close").addEventListener("click", () => $("#bx-drawer").classList.add("hidden"));
$("#d-save").addEventListener("click", save);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#bx-drawer").classList.add("hidden"); });

document.querySelectorAll(".bx-tab").forEach((t) =>
  t.addEventListener("click", () => showTab(t.dataset.tab)));
$("#t-refresh").addEventListener("click", loadTrends);
$("#t-window").addEventListener("change", loadTrends);

load();
