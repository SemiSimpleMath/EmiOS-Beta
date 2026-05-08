// Beliefs Dashboard — fetch trends, render rows, drill-down evidence on click.
// All data comes from /api/personalize/beliefs/* — no localStorage, no auth
// state. The page is read-only today; future: pin / deprecate / edit actions.

(function () {
  "use strict";

  const API_TRENDS = "/api/personalize/beliefs/trends";
  const API_EVIDENCE = (id) => `/api/personalize/beliefs/${id}/evidence`;

  const $ = (sel, root) => (root || document).querySelector(sel);
  const el = (tag, cls) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    return n;
  };
  const text = (s) => document.createTextNode(s == null ? "" : String(s));

  function escapeHtml(s) {
    if (s == null) return "";
    return String(s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function badge(label, kind) {
    const b = el("span", `pz-badge ${kind ? "pz-badge--" + kind : ""}`);
    b.appendChild(text(label));
    return b;
  }

  function counts(b, mode) {
    // mode: 'up' = highlight confirms; 'warn' = highlight challenges
    const wrap = el("div", "pz-row__counts");
    const up = el("div", "pz-c--up");
    const dn = el("div", mode === "warn" ? "pz-c--warn" : null);
    up.appendChild(text(`+${b.confirms != null ? b.confirms : 0}`));
    dn.appendChild(text(`-${b.challenges != null ? b.challenges : 0}`));
    wrap.appendChild(up);
    wrap.appendChild(dn);
    if (b.qualifies) {
      const q = el("div", null);
      q.appendChild(text(`?${b.qualifies}`));
      wrap.appendChild(q);
    }
    return wrap;
  }

  function renderRow(b, mode) {
    const row = el("div", "pz-row");
    row.dataset.beliefId = b.id;

    if (mode === "deprecated") {
      // Deprecated rows show date instead of counts
      const c = el("div", "pz-row__counts");
      const d = el("div", null);
      d.appendChild(text(b.deprecated_on || ""));
      c.appendChild(d);
      const o = el("div", null);
      o.appendChild(text(`obs=${b.observation_count != null ? b.observation_count : "?"}`));
      c.appendChild(o);
      row.appendChild(c);
    } else {
      row.appendChild(counts(b, mode));
    }

    const body = el("div", "pz-row__body");
    const stmt = el("div", "pz-row__statement");
    stmt.appendChild(text(b.statement || "(no statement)"));
    body.appendChild(stmt);

    const meta = el("div", "pz-row__meta");
    meta.appendChild(text(b.belief_key || ""));
    body.appendChild(meta);

    if (mode === "deprecated" && b.reason) {
      const r = el("div", "pz-row__reason");
      r.appendChild(text(b.reason));
      body.appendChild(r);
    }
    row.appendChild(body);

    const badges = el("div", "pz-row__badges");
    if (b.domain) badges.appendChild(badge(b.domain));
    if (b.confidence) badges.appendChild(badge(b.confidence, b.confidence));
    if (b.scope) badges.appendChild(badge(b.scope, b.scope));
    row.appendChild(badges);

    row.addEventListener("click", () => openEvidence(b.id));
    return row;
  }

  function renderList(containerId, beliefs, mode) {
    const container = $(containerId);
    container.innerHTML = "";
    if (!beliefs || beliefs.length === 0) {
      const e = el("div", "pz-empty");
      e.appendChild(text(
        mode === "warn" ? "No active beliefs lost ground in this window. Engine is steady."
          : mode === "deprecated" ? "No deprecations in this window."
          : "No beliefs gained meaningful ground in this window."
      ));
      container.appendChild(e);
      return;
    }
    for (const b of beliefs) {
      container.appendChild(renderRow(b, mode));
    }
  }

  async function loadTrends() {
    const days = $("#pz-window-select").value;
    const url = `${API_TRENDS}?days=${encodeURIComponent(days)}`;
    try {
      const res = await fetch(url, { credentials: "same-origin" });
      const data = await res.json();
      if (!data || !data.success) {
        showError(data && data.error ? data.error : "Could not load trends.");
        return;
      }
      renderList("#pz-trending-up", data.trending_up, "up");
      renderList("#pz-challenged", data.challenged, "warn");
      renderList("#pz-deprecated", data.recently_deprecated, "deprecated");
    } catch (err) {
      showError(String(err));
    }
  }

  function showError(msg) {
    for (const id of ["#pz-trending-up", "#pz-challenged", "#pz-deprecated"]) {
      const c = $(id);
      c.innerHTML = "";
      const e = el("div", "pz-empty");
      e.appendChild(text("Error: " + msg));
      c.appendChild(e);
    }
  }

  // ── Evidence modal ──────────────────────────────────────────────────────

  async function openEvidence(beliefId) {
    const modal = $("#pz-modal");
    const body = $("#pz-modal__body");
    body.innerHTML = '<div class="pz-loading">Loading…</div>';
    modal.classList.remove("pz-modal--hidden");
    modal.setAttribute("aria-hidden", "false");

    try {
      const res = await fetch(API_EVIDENCE(beliefId) + "?limit=30", { credentials: "same-origin" });
      const data = await res.json();
      if (!data || !data.success) {
        body.innerHTML = `<div class="pz-empty">Error: ${escapeHtml(data && data.error || "could not load")}</div>`;
        return;
      }
      renderEvidenceModal(body, data.belief, data.evidence);
    } catch (err) {
      body.innerHTML = `<div class="pz-empty">Error: ${escapeHtml(String(err))}</div>`;
    }
  }

  function renderEvidenceModal(container, belief, evidence) {
    container.innerHTML = "";
    const wrap = el("div", "pz-belief-detail");

    const h = el("h3");
    h.appendChild(text(belief.belief_key || ""));
    wrap.appendChild(h);

    const meta = el("div", "pz-row__meta");
    const parts = [];
    if (belief.domain) parts.push(belief.domain);
    if (belief.confidence) parts.push(belief.confidence);
    if (belief.scope) parts.push(belief.scope);
    if (belief.status) parts.push(`status=${belief.status}`);
    if (belief.observation_count != null) parts.push(`obs=${belief.observation_count}`);
    meta.appendChild(text(parts.join(" · ")));
    wrap.appendChild(meta);

    if (belief.statement) {
      const s = el("div", "pz-detail-stmt");
      s.appendChild(text(belief.statement));
      wrap.appendChild(s);
    }

    const evHeader = el("div");
    evHeader.style.fontWeight = "600";
    evHeader.style.marginTop = "8px";
    evHeader.appendChild(text(`Recent evidence (${evidence ? evidence.length : 0})`));
    wrap.appendChild(evHeader);

    const list = el("div", "pz-evidence-list");
    if (!evidence || evidence.length === 0) {
      const e = el("div", "pz-empty");
      e.appendChild(text("No evidence rows for this belief."));
      list.appendChild(e);
    } else {
      for (const ev of evidence) {
        const row = el("div", "pz-ev");
        const head = el("div", "pz-ev__head");
        const sig = el("span", "pz-ev__signal pz-ev__signal--" + (ev.signal_type || "unknown"));
        sig.appendChild(text(ev.signal_type || "?"));
        head.appendChild(sig);
        const dateSrc = el("span");
        dateSrc.appendChild(text(`${ev.source_date || ""} · ${ev.source_type || ""}`));
        head.appendChild(dateSrc);
        if (ev.weight != null) {
          const w = el("span");
          w.appendChild(text(`weight=${ev.weight}`));
          head.appendChild(w);
        }
        row.appendChild(head);
        if (ev.summary) {
          const s = el("div", "pz-ev__summary");
          s.appendChild(text(ev.summary));
          row.appendChild(s);
        }
        if (ev.raw_text && ev.raw_text !== ev.summary) {
          const r = el("div", "pz-ev__raw");
          r.appendChild(text("user said: " + ev.raw_text));
          row.appendChild(r);
        }
        list.appendChild(row);
      }
    }
    wrap.appendChild(list);
    container.appendChild(wrap);
  }

  function closeModal() {
    const modal = $("#pz-modal");
    modal.classList.add("pz-modal--hidden");
    modal.setAttribute("aria-hidden", "true");
  }

  // ── Wire ────────────────────────────────────────────────────────────────

  document.addEventListener("DOMContentLoaded", () => {
    loadTrends();
    $("#pz-window-select").addEventListener("change", loadTrends);
    document.addEventListener("click", (e) => {
      if (e.target && e.target.dataset && e.target.dataset.close === "1") closeModal();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeModal();
    });
  });
})();
