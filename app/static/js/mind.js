"use strict";
// /mind: what reached the brain on a day, what became of each piece, and every open concern's story.
const BASE = window.SCRIPT_NAME || "";
const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
};
const clock = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const longDay = (day) => new Date(`${day}T12:00:00`).toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
const SOURCE = { chat: "chat", email: "email", work: "work done" };
const DECISION = { act_now: "acting now", hold: "on hold", no_action: "resting" };
const ORDER = { act_now: 0, hold: 1, no_action: 2 };

let view = null;

async function load(day) {
  const r = await fetch(`${BASE}/api/mind${day ? `?day=${encodeURIComponent(day)}` : ""}`);
  if (!r.ok) throw new Error(`${r.status}`);
  view = await r.json();
  render();
}

function stage(num, label, sub) {
  const s = el("div", "mn-stage");
  s.appendChild(el("div", "mn-num", String(num)));
  s.appendChild(el("div", "mn-label", label));
  if (sub) s.appendChild(el("div", "mn-sources", sub));
  return s;
}

function renderRibbon() {
  const r = view.ribbon;
  const box = $("#mn-ribbon");
  box.textContent = "";
  const sources = Object.entries(r.by_source).map(([k, n]) => `${n} ${SOURCE[k] || k}`).join(" · ");
  const parts = [stage(r.came_in, "came in", sources), stage(r.matters, "read"), stage(r.concerns_touched, "concerns touched"),
    stage(r.handed_on, "handed on"), stage(r.settled, "settled")];
  parts.forEach((p, i) => {
    if (i) box.appendChild(el("div", `mn-flow${i > 1 ? ` d${i - 1}` : ""}`));
    box.appendChild(p);
  });
}

function renderStory() {
  const s = view.story;
  $("#mn-headline").textContent = s ? s.headline : view.story_error ? "The day's story could not be written." :
    view.ribbon.came_in ? "The day's story is being written." : "A quiet day: nothing reached the brain.";
  $("#mn-overview").textContent = s ? s.overview : "";
}

function itemRow(item) {
  const li = el("li", "mn-item");
  li.appendChild(el("span", "mn-src", SOURCE[item.source] || item.source));
  const body = el("div");
  body.appendChild(el("span", `mn-line${item.line ? "" : " pending"}`, item.line || "Being told…"));
  body.appendChild(el("span", "mn-who", [item.who, clock(item.when)].filter(Boolean).join(" · ")));
  li.appendChild(body);
  li.appendChild(item.tag ? el("span", `mn-tag ${item.tag_kind}`, item.tag) : el("span"));
  const orig = el("details", "mn-original");
  orig.appendChild(el("summary", null, "the original"));
  orig.appendChild(el("pre", null, item.text));
  li.appendChild(orig);
  return li;
}

function renderMatters() {
  const box = $("#mn-matters");
  box.textContent = "";
  if (!view.matters.length) box.appendChild(el("p", "mn-empty", "Nothing was read this day."));
  view.matters.slice().reverse().forEach((m, i) => {
    const card = el("article", "mn-matter");
    card.style.animationDelay = `${Math.min(i, 12) * 40}ms`;
    const head = el("div", "mn-matter-head");
    head.appendChild(el("span", "mn-time", clock(m.at)));
    head.appendChild(el("span", "mn-matter-title", m.headline || (m.status === "failed" ? "Could not be read" : "Being told…")));
    if (m.status === "failed") head.appendChild(el("span", "mn-tag bad", "failed"));
    card.appendChild(head);
    const list = el("ul", "mn-items");
    m.items.forEach((it) => list.appendChild(itemRow(it)));
    card.appendChild(list);
    box.appendChild(card);
  });
  const aside = $("#mn-aside");
  aside.classList.toggle("hidden", !view.set_aside.length);
  aside.querySelector("summary").textContent = `${view.set_aside.length} more came in and were set aside without a concern`;
  const list = aside.querySelector(".mn-aside-list");
  list.textContent = "";
  view.set_aside.forEach((e) => {
    const row = el("div", "mn-aside-row");
    row.appendChild(el("div", "mn-aside-text", e.text));
    row.appendChild(el("div", "mn-who", [SOURCE[e.source] || e.source, e.who, clock(e.when), e.reason].filter(Boolean).join(" · ")));
    list.appendChild(row);
  });
}

function concernCard(c) {
  const card = el("article", `mn-card ${c.decision || ""}`);
  card.appendChild(el("div", "mn-card-title", c.title));
  const pills = el("div", "mn-pills");
  if (c.story && c.story.state) pills.appendChild(el("span", "mn-pill state", c.story.state));
  if (c.decision) {
    const label = c.decision === "hold" && c.hold_until ? `on hold until ${new Date(c.hold_until.replace(" ", "T")).toLocaleDateString([], { month: "short", day: "numeric" })}` : DECISION[c.decision];
    pills.appendChild(el("span", `mn-pill ${c.decision}`, label));
  }
  card.appendChild(pills);
  const s = c.story;
  if (!s) {
    card.appendChild(el("p", "mn-empty", c.story_error ? "Its story could not be written." : "Its story is being written."));
  } else {
    card.appendChild(el("p", null, s.story));
    if (s.proposed && s.proposed.length) {
      const wrap = el("div");
      wrap.appendChild(el("div", "mn-sub", "Proposed now"));
      const ul = el("ul");
      s.proposed.forEach((p) => ul.appendChild(el("li", null, p)));
      wrap.appendChild(ul);
      card.appendChild(wrap);
    }
    const fate = el("div");
    fate.appendChild(el("div", "mn-sub", "Going forward"));
    fate.appendChild(el("p", null, s.fate));
    card.appendChild(fate);
  }
  const foot = el("div", "mn-card-foot");
  foot.appendChild(el("span", null, c.work ? `${c.work} piece${c.work > 1 ? "s" : ""} of work` : "no work yet"));
  const link = el("a", null, "how it was decided →");
  link.href = `${BASE}/brain/flow?concern=${encodeURIComponent(c.concern_id)}&node=concerns`;
  link.target = "_blank";
  foot.appendChild(link);
  card.appendChild(foot);
  return card;
}

function renderConcerns() {
  const sort = (a, b) => (ORDER[a.decision] ?? 3) - (ORDER[b.decision] ?? 3) || String(a.title).localeCompare(String(b.title));
  const box = $("#mn-concerns");
  box.textContent = "";
  if (!view.concerns.length) box.appendChild(el("p", "mn-empty", "No open concerns."));
  view.concerns.slice().sort(sort).forEach((c, i) => {
    const card = concernCard(c);
    card.style.animationDelay = `${Math.min(i, 12) * 40}ms`;
    box.appendChild(card);
  });
  const calendar = $("#mn-calendar");
  calendar.textContent = "";
  $("#mn-calendar-count").textContent = `(${view.calendar.length})`;
  view.calendar.forEach((line) => {
    const li = el("li");
    li.appendChild(el("b", null, line.time));
    li.appendChild(document.createTextNode(` — ${line.statement}`));
    calendar.appendChild(li);
  });
  const settled = $("#mn-settled");
  settled.textContent = "";
  $("#mn-settled-block").classList.toggle("hidden", !view.settled.length);
  view.settled.forEach((c) => {
    const li = el("li");
    li.appendChild(el("b", null, c.title));
    if (c.reason) li.appendChild(document.createTextNode(` — ${c.reason}`));
    settled.appendChild(li);
  });
}

function render() {
  $("#mn-day").textContent = view.today ? `Today · ${longDay(view.day)}` : longDay(view.day);
  const i = view.days.indexOf(view.day);
  $("#mn-prev").disabled = i < 0 || i >= view.days.length - 1;
  $("#mn-next").disabled = i <= 0;
  renderRibbon();
  renderStory();
  renderMatters();
  renderConcerns();
}

$("#mn-prev").addEventListener("click", () => load(view.days[view.days.indexOf(view.day) + 1]));
$("#mn-next").addEventListener("click", () => load(view.days[view.days.indexOf(view.day) - 1]));
setInterval(() => { if (view && view.today) load(view.day); }, 60000);
load(new URLSearchParams(location.search).get("day")).catch((e) => {
  $("#mn-headline").textContent = `Could not load: ${e}`;
});
