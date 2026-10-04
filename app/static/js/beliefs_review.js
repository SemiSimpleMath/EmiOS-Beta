"use strict";
// The morning review: beliefs proposed for the calendar, and the other new beliefs. Each decision is one POST;
// a decided card leaves its panel.
const BASE = window.SCRIPT_NAME || "";
const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
};

async function decide(beliefId, action, statement, extra) {
  const r = await fetch(BASE + "/api/beliefs/review/decide", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ belief_id: beliefId, action, statement, ...(extra || {}) }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    alert(`Could not save ${beliefId}: ${data.error || r.status}`);
    return null;
  }
  return data.belief;
}

function setCount(sel, n) {
  $(sel).textContent = `(${n})`;
}

function leave(card, listSel, countSel) {
  card.remove();
  const left = $(listSel).querySelectorAll(".rv-card").length;
  setCount(countSel, left);
  if (!left) $(listSel).appendChild(el("div", "bx-empty", "Nothing left to review here."));
}

function editInPlace(card, b, onSaved) {
  const stmt = card.querySelector(".bx-stmt");
  const actions = card.querySelector(".rv-actions");
  const box = el("textarea", "rv-edit");
  box.value = b.statement;
  box.rows = 3;
  const save = el("button", "rv-btn primary", "Save wording");
  const cancel = el("button", "rv-btn", "Cancel");
  const bar = el("div", "rv-actions");
  bar.append(save, cancel);
  stmt.replaceWith(box);
  actions.replaceWith(bar);
  cancel.addEventListener("click", () => {
    box.replaceWith(stmt);
    bar.replaceWith(actions);
  });
  save.addEventListener("click", async () => {
    const text = box.value.trim();
    if (!text) return;
    const saved = await decide(b.belief_id, "edit", text);
    if (saved) onSaved(saved, box, bar, stmt, actions);
  });
}

function card(b, buttons) {
  const c = el("div", "bx-item rv-card");
  const main = el("div", "bx-item-main");
  main.appendChild(el("div", "bx-stmt", b.statement));
  if (b.calendar_note) {
    // The tagger's note on this belief (when it would appear: why), not a belief of its own.
    const note = el("div", "rv-note");
    note.appendChild(el("span", "rv-note-label", "Why proposed · when: "));
    note.appendChild(document.createTextNode(b.calendar_note));
    main.appendChild(note);
  }
  const tags = el("div", "bx-tags");
  tags.appendChild(el("span", "bx-badge sid", b.belief_id));
  if (b.kind) tags.appendChild(el("span", "bx-badge kind", b.kind));
  tags.appendChild(el("span", "bx-badge obs", `${b.support || 0}× support`));
  if (b.last_observed) tags.appendChild(el("span", "bx-badge date", b.last_observed));
  main.appendChild(tags);
  const actions = el("div", "rv-actions");
  buttons.forEach(([label, cls, fn]) => {
    const btn = el("button", "rv-btn " + cls, label);
    btn.addEventListener("click", () => fn(c));
    actions.appendChild(btn);
  });
  main.appendChild(actions);
  c.appendChild(main);
  return c;
}

function renderCalendar(rows) {
  const list = $("#rv-calendar");
  list.textContent = "";
  setCount("#rv-cal-count", rows.length);
  if (!rows.length) list.appendChild(el("div", "bx-empty", "No beliefs waiting for a calendar decision."));
  rows.forEach((b) => {
    const go = (action) => async (c) => {
      if (await decide(b.belief_id, action)) leave(c, "#rv-calendar", "#rv-cal-count");
    };
    const c = card(b, [
      ["Add to calendar", "primary", go("add_to_calendar")],
      ["Not for the calendar", "", go("not_for_calendar")],
      // A reworded proposal stays here until its calendar decision.
      ["Edit", "", (c) => editInPlace(c, b, (saved, box, bar, stmt, actions) => {
        b.statement = saved.statement;
        stmt.textContent = saved.statement;
        box.replaceWith(stmt);
        bar.replaceWith(actions);
      })],
    ]);
    list.appendChild(c);
  });
}

function renderNew(rows) {
  const list = $("#rv-new");
  list.textContent = "";
  setCount("#rv-new-count", rows.length);
  if (!rows.length) list.appendChild(el("div", "bx-empty", "No new beliefs to review."));
  rows.forEach((b) => {
    const go = (action) => async (c) => {
      if (await decide(b.belief_id, action)) leave(c, "#rv-new", "#rv-new-count");
    };
    const c = card(b, [
      ["Approve", "primary", go("approve")],
      ["Dismiss", "danger", go("dismiss")],
      ["Edit", "", (c) => editInPlace(c, b, () => leave(c, "#rv-new", "#rv-new-count"))],
    ]);
    list.appendChild(c);
  });
}

function evidenceList(rows) {
  const box = el("div", "rv-evidence");
  (rows || []).forEach((e) => {
    const line = el("div", "rv-ev");
    line.appendChild(el("span", "rv-ev-date", e.time || e.day || ""));
    if (e.in_reply_to) line.appendChild(el("div", "rv-ev-reply", "in reply to: " + e.in_reply_to));
    line.appendChild(el("div", "rv-ev-text", e.text || ""));
    box.appendChild(line);
  });
  if (!(rows || []).length) box.appendChild(el("div", "rv-ev-text", "No evidence recorded."));
  return box;
}

function renderContradictions(rows) {
  const list = $("#rv-contradictions");
  list.textContent = "";
  $("#rv-con-panel").classList.toggle("hidden", !rows.length);
  setCount("#rv-con-count", rows.length);
  rows.forEach((b) => {
    const go = (action) => async (c) => {
      if (await decide(b.belief_id, action)) leave(c, "#rv-contradictions", "#rv-con-count");
    };
    const drafted = (b.contradicts || []).filter((t) => t.draft);
    // The drafted rewrites, as edited in their boxes, applied together.
    const applyRewrite = async (c) => {
      const rewrites = {};
      c.querySelectorAll("textarea[data-rewrite]").forEach((box) => { rewrites[box.dataset.rewrite] = box.value.trim(); });
      if (await decide(b.belief_id, "apply_rewrite", undefined, { rewrites })) leave(c, "#rv-contradictions", "#rv-con-count");
    };
    const c = card(b, [
      ...(drafted.length ? [["Apply the rewrite", "primary", applyRewrite]] : []),
      ["Keep the new one", drafted.length ? "" : "primary", go("keep_new")],
      ["Keep the current belief", "", go("keep_old")],
      ["Keep both", "", go("keep_both")],
      // A reworded contradiction stays here until it is decided.
      ["Edit the new one", "", (c) => editInPlace(c, b, (saved, box, bar, stmt, actions) => {
        b.statement = saved.statement;
        stmt.textContent = saved.statement;
        box.replaceWith(stmt);
        bar.replaceWith(actions);
      })],
    ]);
    const main = c.querySelector(".bx-item-main");
    main.insertBefore(el("div", "rv-label", "New:"), main.firstChild);
    main.insertBefore(evidenceList(b.evidence), main.querySelector(".bx-tags"));
    (b.contradicts || []).forEach((t) => {
      const old = el("div", "rv-old");
      old.appendChild(el("div", "rv-label", `Current belief (${t.belief_id}):`));
      old.appendChild(el("div", "bx-stmt", t.statement));
      old.appendChild(evidenceList(t.evidence));
      if (t.draft) {
        old.appendChild(el("div", "rv-label", "Drafted rewrite (edit before applying if needed):"));
        const box = el("textarea", "rv-edit");
        box.rows = 2;
        box.value = t.draft;
        box.dataset.rewrite = t.belief_id;
        old.appendChild(box);
      }
      main.insertBefore(old, main.querySelector(".rv-actions"));
    });
    list.appendChild(c);
  });
}

async function load() {
  const r = await fetch(BASE + "/api/beliefs/review");
  const data = await r.json();
  renderCalendar(data.calendar || []);
  renderNew(data.new || []);
  renderContradictions(data.contradictions || []);
}

load();
