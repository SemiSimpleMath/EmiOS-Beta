// Personal Directives editor — the owner's standing directives, split by role
// into pieces (each a shared template + a gitignored _personal.md overlay).
// Pick a piece, edit its overlay, and see a live preview of the final
// concatenation that reaches the agents that read it.

(function () {
  "use strict";

  const API = "/api/personalize/directives";

  const $ = (sel) => document.querySelector(sel);
  let pieces = [];        // [{id, title, read_by, base_filename, personal_filename}]
  let currentId = null;   // selected piece id
  let lastSaved = "";     // last server-known overlay content; for dirty-state + revert

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

  function readByText(piece) {
    if (!piece.read_by || !piece.read_by.length) return "No agent reads this yet.";
    return "Read by " + piece.read_by.join(", ") + ".";
  }

  function isDirty() {
    return currentId !== null && $("#pz-dir-textarea").value !== lastSaved;
  }

  function renderPicker() {
    const nav = $("#pz-dir-pieces");
    nav.textContent = "";
    pieces.forEach((piece) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "pz-dir-piece" + (piece.id === currentId ? " pz-dir-piece--active" : "");
      btn.dataset.piece = piece.id;
      const title = document.createElement("span");
      title.className = "pz-dir-piece__title";
      title.textContent = piece.title;
      const who = document.createElement("span");
      who.className = "pz-dir-piece__who" + (piece.read_by && piece.read_by.length ? "" : " pz-dir-piece__who--none");
      who.textContent = readByText(piece);
      btn.appendChild(title);
      btn.appendChild(who);
      btn.addEventListener("click", () => selectPiece(piece.id));
      nav.appendChild(btn);
    });
  }

  function applyPiece(data) {
    if (!data || !data.success) {
      setStatus("Error: " + (data && data.error ? data.error : "load failed"), "err");
      return;
    }
    const piece = data.piece;
    $("#pz-dir-piece-title").textContent = piece.title;
    $("#pz-dir-read-by").textContent = readByText(piece);
    $("#pz-dir-base-name").textContent = "File: " + ((data.base && data.base.filename) || piece.base_filename);
    lastSaved = (data.personal && data.personal.content) || "";
    $("#pz-dir-textarea").value = lastSaved;
    $("#pz-dir-personal-meta").textContent = metaText(lastSaved, data.personal && data.personal.exists);
    $("#pz-dir-preview").textContent = data.combined || "(empty)";
    $("#pz-dir-base").textContent = (data.base && data.base.content) || "(empty)";
    refreshDirty();
  }

  function refreshDirty() {
    if (!isDirty()) {
      setStatus("Saved.", "ok");
      $("#pz-dir-revert").disabled = true;
    } else {
      setStatus("Unsaved changes.", "dirty");
      $("#pz-dir-revert").disabled = false;
    }
  }

  async function selectPiece(pieceId) {
    if (pieceId === currentId) return;
    if (isDirty() && !window.confirm("Discard unsaved changes to this piece?")) return;
    currentId = pieceId;
    try { history.replaceState(null, "", "#" + pieceId); } catch (_) { /* hash is a convenience only */ }
    renderPicker();
    setStatus("Loading…");
    try {
      const res = await fetch(API + "?piece=" + encodeURIComponent(pieceId), { credentials: "same-origin" });
      const data = await res.json();
      applyPiece(data);
    } catch (err) {
      setStatus("Error: " + String(err), "err");
    }
  }

  async function loadPieces() {
    setStatus("Loading…");
    try {
      const res = await fetch(API, { credentials: "same-origin" });
      const data = await res.json();
      if (!data || !data.success) {
        setStatus("Error: " + (data && data.error ? data.error : "load failed"), "err");
        return;
      }
      pieces = data.pieces || [];
      const fromHash = window.location.hash.slice(1);  // "" or "#<id>"
      const initial = pieces.some((p) => p.id === fromHash) ? fromHash : (pieces[0] && pieces[0].id);
      renderPicker();
      if (initial) selectPiece(initial);
    } catch (err) {
      setStatus("Error: " + String(err), "err");
    }
  }

  async function save() {
    if (currentId === null) return;
    const btn = $("#pz-dir-save");
    btn.disabled = true;
    setStatus("Saving…");
    const content = $("#pz-dir-textarea").value;
    try {
      const res = await fetch(API, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ piece: currentId, content }),
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
    loadPieces();
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
