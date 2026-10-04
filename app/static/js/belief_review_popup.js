"use strict";
// The morning belief review, in the chat UI (2026-10-03). Once a day, when anything waits in the review (calendar
// proposals, new beliefs, held contradictions), the review page opens in a modal. The page itself is
// /beliefs/review; this only frames it. "Shown today" is remembered in this browser. It checks when the chat
// loads and again every CHECK_EVERY_MS until it has shown the review that day, so a tab left open overnight, or
// open across a restart of the server, still opens it.
(function () {
  const BASE = window.SCRIPT_NAME || "";
  const KEY = "belief_review_shown_on";
  const CHECK_EVERY_MS = 10 * 60 * 1000;

  function today() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }

  function shownToday() {
    try {
      return window.localStorage.getItem(KEY) === today();
    } catch (e) {
      return false;
    }
  }

  function markShown() {
    try {
      window.localStorage.setItem(KEY, today());
    } catch (e) {
      // Storage unavailable: the review shows again on the next load, which is harmless.
    }
  }

  function open(waiting) {
    const overlay = document.createElement("div");
    overlay.className = "brp-overlay";
    const dialog = document.createElement("div");
    dialog.className = "brp-dialog";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-label", "Belief review");
    const head = document.createElement("div");
    head.className = "brp-head";
    const title = document.createElement("span");
    title.className = "brp-title";
    title.textContent = `Belief review · ${waiting} to look at`;
    const close = document.createElement("button");
    close.className = "brp-close";
    close.setAttribute("aria-label", "Close");
    close.textContent = "×";
    head.append(title, close);
    const frame = document.createElement("iframe");
    frame.className = "brp-frame";
    frame.src = BASE + "/beliefs/review";
    frame.title = "Belief review";
    dialog.append(head, frame);
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);

    const shut = () => {
      overlay.remove();
      document.removeEventListener("keydown", onKey);
    };
    const onKey = (e) => { if (e.key === "Escape") shut(); };
    close.addEventListener("click", shut);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) shut(); });
    document.addEventListener("keydown", onKey);
  }

  async function check() {
    if (shownToday()) return;
    let r;
    try {
      r = await fetch(BASE + "/api/beliefs/review");
    } catch (e) {
      console.warn("belief review: server unreachable; checking again later", e);
      return;
    }
    if (!r.ok) {
      console.warn("belief review: could not load", r.status, "; checking again later");
      return;
    }
    const data = await r.json();
    const waiting = (data.calendar || []).length + (data.new || []).length + (data.contradictions || []).length;
    if (!waiting) return;
    markShown();
    open(waiting);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", check);
  else check();
  setInterval(check, CHECK_EVERY_MS);
})();
