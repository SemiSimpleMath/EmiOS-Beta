/* Pods management page — list every pod, view contents, edit importance +
 * read-permission (min_authority). Owner-only surface (server gates by
 * local-only). Vanilla JS; filtering/sorting is client-side. */
(function () {
  "use strict";

  const BANDS = Array.isArray(window.AUTHORITY_BANDS) ? window.AUTHORITY_BANDS : [];

  const els = {
    status: document.getElementById("pods-status"),
    refresh: document.getElementById("pods-refresh"),
    search: document.getElementById("pods-search"),
    kind: document.getElementById("pods-kind"),
    sort: document.getElementById("pods-sort"),
    shown: document.getElementById("pods-shown"),
    list: document.getElementById("pods-list"),
    detailEmpty: document.getElementById("pods-detail-empty"),
    detailInner: document.getElementById("pods-detail-inner"),
  };

  let allPods = [];        // headers from /list
  let selectedId = null;

  // ── helpers ──────────────────────────────────────────────────────────────
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function impBucket(v) {
    if (v == null || v === "") return { cls: "", txt: "–" };
    const n = Number(v);
    const txt = Number.isInteger(n) ? String(n) : n.toFixed(1);
    if (n >= 7) return { cls: "rated imp-hi", txt };
    if (n >= 4) return { cls: "rated imp-md", txt };
    return { cls: "rated imp-lo", txt };
  }

  function authChip(v) {
    const band = BANDS.find((b) => Number(b.value) === Number(v));
    const short = band ? band.label.split("·")[0].trim() : String(v);
    const cls = Number(v) >= 100 ? "chip auth auth-100" : "chip auth";
    return `<span class="${cls}" title="${esc(band ? band.label : "min_authority " + v)}">auth ${esc(short)}</span>`;
  }

  function fmtDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }

  function setStatus(txt, cls) {
    els.status.textContent = txt;
    els.status.className = "pods-status" + (cls ? " " + cls : "");
  }

  // ── load ─────────────────────────────────────────────────────────────────
  async function loadList() {
    setStatus("loading…");
    try {
      const r = await fetch("/api/pods/manage/list");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      allPods = data.pods || [];
      populateKinds();
      applyView();
      setStatus(data.count + " pods" + (data.capped ? " (capped)" : ""), "ready");
    } catch (e) {
      setStatus("failed to load: " + e.message, "error");
      els.list.innerHTML = `<li class="pods-empty-state">Could not load pods: ${esc(e.message)}</li>`;
    }
  }

  function populateKinds() {
    const kinds = Array.from(new Set(allPods.map((p) => p.kind))).sort();
    const cur = els.kind.value;
    els.kind.innerHTML = '<option value="">all kinds</option>' +
      kinds.map((k) => `<option value="${esc(k)}">${esc(k)}</option>`).join("");
    if (kinds.includes(cur)) els.kind.value = cur;
  }

  // ── filter + sort + render ────────────────────────────────────────────────
  function applyView() {
    const q = els.search.value.trim().toLowerCase();
    const kind = els.kind.value;
    const sort = els.sort.value;

    let view = allPods.filter((p) => {
      if (kind && p.kind !== kind) return false;
      if (!q) return true;
      const hay = (p.one_liner + " " + p.preview + " " + (p.tags || []).join(" ") + " " + p.pod_id).toLowerCase();
      return hay.includes(q);
    });

    view.sort((a, b) => {
      if (sort === "recent") return (b.created_at || "").localeCompare(a.created_at || "");
      if (sort === "kind") return a.kind.localeCompare(b.kind) || (b.created_at || "").localeCompare(a.created_at || "");
      if (sort === "authority") return (b.min_authority || 0) - (a.min_authority || 0);
      // importance: rated first (desc), unrated last, then newest
      const ai = a.importance == null ? -1 : a.importance;
      const bi = b.importance == null ? -1 : b.importance;
      if (bi !== ai) return bi - ai;
      return (b.created_at || "").localeCompare(a.created_at || "");
    });

    els.shown.textContent = view.length === allPods.length
      ? `${allPods.length}`
      : `${view.length} / ${allPods.length}`;
    renderList(view);
  }

  function renderList(view) {
    if (!view.length) {
      els.list.innerHTML = `<li class="pods-empty-state">No pods match.</li>`;
      return;
    }
    els.list.innerHTML = view.map(rowHtml).join("");
    els.list.querySelectorAll(".pod-row").forEach((el) => {
      el.addEventListener("click", () => selectPod(el.dataset.id));
    });
  }

  function rowHtml(p) {
    const imp = impBucket(p.importance);
    const tags = (p.tags || []).slice(0, 3).map((t) => `<span class="chip">${esc(t)}</span>`).join(" ");
    const sel = p.pod_id === selectedId ? " selected" : "";
    return `
      <li class="pod-row${sel}" data-id="${esc(p.pod_id)}">
        <div class="pod-imp ${imp.cls}" title="importance ${imp.txt}">${imp.txt}</div>
        <div class="pod-row-main">
          <div class="pod-row-oneliner">${esc(p.one_liner) || '<span style="color:var(--text-dim)">(no one-liner)</span>'}</div>
          <div class="pod-row-meta">
            <span class="chip kind">${esc(p.kind)}</span>
            ${authChip(p.min_authority)}
            ${p.scope_id ? `<span>${esc(p.scope_id)}</span>` : ""}
            <span>${esc(fmtDate(p.created_at))}</span>
            ${tags}
          </div>
        </div>
      </li>`;
  }

  // ── detail + edit ──────────────────────────────────────────────────────────
  async function selectPod(podId) {
    selectedId = podId;
    els.list.querySelectorAll(".pod-row").forEach((el) =>
      el.classList.toggle("selected", el.dataset.id === podId));
    els.detailEmpty.hidden = true;
    els.detailInner.hidden = false;
    els.detailInner.innerHTML = `<div class="pods-empty-state">loading…</div>`;
    try {
      const r = await fetch("/api/pods/manage/item?pod_id=" + encodeURIComponent(podId));
      if (!r.ok) throw new Error("HTTP " + r.status);
      renderDetail(await r.json());
    } catch (e) {
      els.detailInner.innerHTML = `<div class="pods-empty-state">Could not load pod: ${esc(e.message)}</div>`;
    }
  }

  function authOptions(current) {
    const opts = BANDS.map((b) =>
      `<option value="${b.value}"${Number(b.value) === Number(current) ? " selected" : ""}>${esc(b.label)}</option>`);
    if (!BANDS.some((b) => Number(b.value) === Number(current))) {
      opts.unshift(`<option value="${esc(current)}" selected>${esc(current)} · custom</option>`);
    }
    return opts.join("");
  }

  function renderDetail(pod) {
    const meta = [];
    if (pod.scope_id) meta.push(["scope", pod.scope_id]);
    meta.push(["created", pod.created_at ? new Date(pod.created_at).toLocaleString() : "—"]);
    if (pod.created_by) meta.push(["created by", pod.created_by]);
    if (pod.for_agents && pod.for_agents.length) meta.push(["for agents", pod.for_agents.join(", ")]);
    if (pod.source_refs && pod.source_refs.length)
      meta.push(["sources", pod.source_refs.map((s) => `${s.kind}:${s.id}`).join(", ")]);

    const metaHtml = meta.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
    const tagsHtml = (pod.tags || []).length
      ? `<div class="pd-taglist">${pod.tags.map((t) => `<span class="chip">${esc(t)}</span>`).join(" ")}</div>`
      : `<span class="pd-edit-hint">none</span>`;
    const hasMeta = pod.metadata && Object.keys(pod.metadata).length;
    const impVal = pod.importance == null ? "" : pod.importance;

    els.detailInner.innerHTML = `
      <div class="pd-kindline">
        <span class="chip kind">${esc(pod.kind)}</span>
        ${authChip(pod.min_authority)}
      </div>
      <div class="pd-oneliner">${esc(pod.one_liner) || "(no one-liner)"}</div>
      <div class="pd-id">${esc(pod.pod_id)}</div>

      <dl class="pd-meta-grid">${metaHtml}</dl>

      <div class="pd-section-label">tags</div>
      ${tagsHtml}

      <div class="pd-section-label">body</div>
      <div class="pd-body${pod.body ? "" : " empty"}">${pod.body ? esc(pod.body) : "(no body — content lives in source refs or projections)"}</div>

      ${hasMeta ? `<div class="pd-section-label">metadata</div><div class="pd-json">${esc(JSON.stringify(pod.metadata, null, 2))}</div>` : ""}

      <div class="pd-edit">
        <div class="pd-edit-row">
          <label for="pd-imp">Importance</label>
          <input id="pd-imp" type="number" min="0" max="10" step="0.5" value="${esc(impVal)}" placeholder="unrated">
          <span class="pd-edit-hint">0–10 priority · blank = unrated</span>
        </div>
        <div class="pd-edit-row">
          <label for="pd-auth">Read permission</label>
          <select id="pd-auth">${authOptions(pod.min_authority)}</select>
        </div>
        <div class="pd-edit-actions">
          <button id="pd-save" class="pd-save" type="button">Save</button>
          <span id="pd-save-msg" class="pd-save-msg"></span>
        </div>
      </div>`;

    document.getElementById("pd-save").addEventListener("click", () => saveCuration(pod.pod_id));
  }

  async function saveCuration(podId) {
    const impRaw = document.getElementById("pd-imp").value.trim();
    const authRaw = document.getElementById("pd-auth").value;
    const msg = document.getElementById("pd-save-msg");
    const btn = document.getElementById("pd-save");

    const payload = {
      pod_id: podId,
      importance: impRaw === "" ? null : Number(impRaw),
      min_authority: Number(authRaw),
    };

    btn.disabled = true;
    msg.textContent = "saving…";
    msg.className = "pd-save-msg";
    try {
      const r = await fetch("/api/pods/manage/update", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
      // reflect into the cached header + re-render row/list
      const hdr = allPods.find((p) => p.pod_id === podId);
      if (hdr) { hdr.importance = data.importance; hdr.min_authority = data.min_authority; }
      applyView();
      msg.textContent = "saved";
      msg.className = "pd-save-msg ok";
    } catch (e) {
      msg.textContent = e.message;
      msg.className = "pd-save-msg err";
    } finally {
      btn.disabled = false;
    }
  }

  // ── wire ──────────────────────────────────────────────────────────────────
  els.refresh.addEventListener("click", loadList);
  els.search.addEventListener("input", applyView);
  els.kind.addEventListener("change", applyView);
  els.sort.addEventListener("change", applyView);

  loadList();
})();
