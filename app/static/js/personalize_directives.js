// Personal Directives editor — load + edit + save the gitignored
// resource_orchestrator_user_prefs_personal.md overlay, with a live
// preview of the final concatenation that reaches the planner prompt.

(function () {
  "use strict";

  const API_GET = "/api/personalize/directives";
  const API_POST = "/api/personalize/directives";

  const $ = (sel) => document.querySelector(sel);
  let lastSaved = "";  // last server-known content; for dirty-state + revert

  function setStatus(msg, kind) {
    const el = $("#pz-dir-status");
    el.textContent = msg || "";
    el.className = "pz-dir-status" + (kind ? " pz-dir-status--" + kind : "");
  }

  function metaText(content, exists) {
    const lines = content ? content.split("\n").length : 0;
    const chars = content ? content.length : 0;
    if (!exists) return "(no overlay file yet — saving creates it)";
    return `${lines} lines · ${chars} chars`;
  }

  function applyData(data) {
    if (!data || !data.success) {
      setStatus("Error: " + (data && data.error ? data.error : "load failed"), "err");
      return;
    }
    lastSaved = (data.personal && data.personal.content) || "";
    $("#pz-dir-textarea").value = lastSaved;
    $("#pz-dir-personal-meta").textContent = metaText(lastSaved, data.personal && data.personal.exists);
    $("#pz-dir-preview").textContent = data.combined || "(empty)";
    $("#pz-dir-base").textContent = (data.base && data.base.content) || "(empty)";
    refreshDirty();
  }

  function refreshDirty() {
    const cur = $("#pz-dir-textarea").value;
    if (cur === lastSaved) {
      setStatus("Saved.", "ok");
      $("#pz-dir-revert").disabled = true;
    } else {
      setStatus("Unsaved changes.", "dirty");
      $("#pz-dir-revert").disabled = false;
    }
  }

  async function load() {
    setStatus("Loading…");
    try {
      const res = await fetch(API_GET, { credentials: "same-origin" });
      const data = await res.json();
      applyData(data);
    } catch (err) {
      setStatus("Error: " + String(err), "err");
    }
  }

  async function save() {
    const btn = $("#pz-dir-save");
    btn.disabled = true;
    setStatus("Saving…");
    const content = $("#pz-dir-textarea").value;
    try {
      const res = await fetch(API_POST, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      });
      const data = await res.json();
      if (!data || !data.success) {
        setStatus("Save failed: " + (data && data.error ? data.error : res.status), "err");
        return;
      }
      lastSaved = (data.personal && data.personal.content) || content;
      $("#pz-dir-personal-meta").textContent = metaText(lastSaved, true);
      $("#pz-dir-preview").textContent = data.combined || "(empty)";
      refreshDirty();
    } catch (err) {
      setStatus("Save failed: " + String(err), "err");
    } finally {
      btn.disabled = false;
    }
  }

  function revert() {
    $("#pz-dir-textarea").value = lastSaved;
    refreshDirty();
  }

  document.addEventListener("DOMContentLoaded", () => {
    load();
    $("#pz-dir-save").addEventListener("click", save);
    $("#pz-dir-revert").addEventListener("click", revert);
    $("#pz-dir-textarea").addEventListener("input", refreshDirty);
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        save();
      }
    });
  });
})();
