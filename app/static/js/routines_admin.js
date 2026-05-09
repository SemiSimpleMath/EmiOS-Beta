/* Routines admin — fetches /api/routines, renders four views, posts edits. */
(function () {
  "use strict";

  const state = {
    routines: [],
    serverNowLocal: null,
    managerEnabled: true,
    activeTab: "schedule",
    editing: null, // {routine, draft}
  };

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  async function fetchRoutines() {
    const r = await fetch("/api/routines");
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }

  async function postJSON(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : "{}",
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok || data.ok === false) {
      throw new Error(data.error || ("HTTP " + r.status));
    }
    return data;
  }

  function toast(msg, kind) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.remove("hidden", "bad", "ok");
    if (kind) el.classList.add(kind);
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.add("hidden"), 3000);
  }

  function fmtDateTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString();
  }

  function relTime(iso) {
    if (!iso) return "never";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const delta = (Date.now() - d.getTime()) / 1000;
    if (delta < 60) return Math.round(delta) + "s ago";
    if (delta < 3600) return Math.round(delta / 60) + "m ago";
    if (delta < 86400) return (delta / 3600).toFixed(1) + "h ago";
    return Math.round(delta / 86400) + "d ago";
  }

  function statusPillClass(status) {
    if (status === "success") return "on";
    if (status === "failed" || status === "error") return "bad";
    return "off";
  }

  function render() {
    renderHeader();
    if (state.activeTab === "schedule") renderTimeline();
    else if (state.activeTab === "interval") renderIntervalGrid();
    else if (state.activeTab === "weekly") renderWeeklyGrid();
    else if (state.activeTab === "all") renderAllTable();
    else if (state.activeTab === "decisions") renderDecisions();
  }

  function badgeWindow(name) {
    if (!name) return "";
    return `<span class="rt-badge window">window: ${name}</span>`;
  }
  function badgeTrigger(type, topic) {
    if (type === "event") return `<span class="rt-badge event">event: ${topic || "?"}</span>`;
    return "";
  }
  function badgeWatchdog(maxRunSec) {
    if (!maxRunSec) return "";
    const label = maxRunSec >= 60 ? Math.round(maxRunSec / 60) + "m" : maxRunSec + "s";
    return `<span class="rt-badge watchdog">timeout: ${label}</span>`;
  }
  function badgeOnError(onError) {
    if (!onError) return "";
    const max = onError.max_failures || 3;
    const then = onError.then || "disable_with_ticket";
    const tag = then === "log_only" ? "log" : "auto-disable";
    return `<span class="rt-badge onerror">${tag} after ${max}</span>`;
  }
  function badgeFailureStreak(consecutive) {
    if (!consecutive || consecutive === 0) return "";
    return `<span class="rt-badge fail-streak">${consecutive} consecutive failures</span>`;
  }
  function autoDisableBanner(routine) {
    if (!routine.auto_disabled_reason) return "";
    return `<div class="rt-auto-disabled">Auto-disabled · ${escape(routine.auto_disabled_reason)}</div>`;
  }
  function escape(s) {
    return String(s || "").replace(/[&<>"]/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
    }[c]));
  }

  function renderHeader() {
    $("#now-time").textContent = state.serverNowLocal
      ? new Date(state.serverNowLocal).toLocaleTimeString()
      : "—";
    const counts = state.routines.reduce(
      (a, r) => {
        a.total++;
        if (r.enabled) a.on++;
        return a;
      },
      { total: 0, on: 0 }
    );
    $("#manager-status").textContent =
      `${counts.on}/${counts.total} enabled · manager ${state.managerEnabled ? "on" : "OFF"}`;
  }

  function renderTimeline() {
    const root = $("#timeline");
    root.innerHTML = "";
    const now = new Date(state.serverNowLocal || new Date());
    const nowHour = now.getHours();
    const events = state.routines
      .filter((r) => r.policy_type === "daily" || r.policy_type === "weekly")
      .map((r) => {
        const t = (r.run_policy && r.run_policy.time_local) || "";
        const [h, m] = (t.split(":")).map((x) => parseInt(x, 10));
        return { ...r, hour: h, minute: m };
      })
      .filter((r) => Number.isFinite(r.hour))
      .sort((a, b) => a.hour - b.hour || a.minute - b.minute);

    for (let h = 0; h < 24; h++) {
      const label = document.createElement("div");
      label.className = "tl-hour-label" + (h === nowHour ? " now-hour" : "");
      label.textContent = String(h).padStart(2, "0") + ":00";
      const row = document.createElement("div");
      row.className = "tl-hour-row" + (h === nowHour ? " now-hour" : "");
      events
        .filter((e) => e.hour === h)
        .forEach((e) => {
          const ev = document.createElement("div");
          let cls = "tl-event";
          if (!e.enabled) cls += " disabled";
          if (e.policy_type === "weekly") cls += " weekly";
          ev.className = cls;
          ev.title = e.notes || "";
          const dow = e.policy_type === "weekly" ? (e.run_policy.day_of_week || "?") + " " : "";
          ev.innerHTML =
            `<span class="tl-event-time">${dow}${String(e.hour).padStart(2, "0")}:${String(e.minute).padStart(2, "0")}</span>` +
            `<span class="tl-event-name">${e.name}</span>` +
            `<span class="tl-event-runner">${e.runner}</span>`;
          ev.addEventListener("click", () => openEdit(e));
          row.appendChild(ev);
        });
      root.appendChild(label);
      root.appendChild(row);
    }
  }

  function renderIntervalGrid() {
    const root = $("#interval-grid");
    root.innerHTML = "";
    const items = state.routines
      .filter((r) => r.policy_type === "interval")
      .sort((a, b) => (a.run_policy.min_interval_seconds || 0) - (b.run_policy.min_interval_seconds || 0));
    if (!items.length) {
      root.innerHTML = `<div class="hint">No interval routines configured.</div>`;
      return;
    }
    items.forEach((r) => {
      const card = document.createElement("div");
      card.className = "interval-card" + (r.enabled ? "" : " disabled");
      card.innerHTML =
        `<div class="card-head"><span class="card-name">${r.name}</span>` +
        `<span class="pill ${r.enabled ? "on" : "off"}">${r.enabled ? "ON" : "OFF"}</span></div>` +
        `<div class="card-cadence">every ${r.schedule_label.replace("every ", "")}</div>` +
        `<div class="card-meta">runner: ${r.runner} · last: ${relTime(r.last_finished_utc)} · runs: ${r.run_count || 0}</div>`;
      card.addEventListener("click", () => openEdit(r));
      root.appendChild(card);
    });
  }

  function renderWeeklyGrid() {
    const root = $("#weekly-grid");
    root.innerHTML = "";
    const days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
    const byDay = Object.fromEntries(days.map((d) => [d, []]));
    state.routines
      .filter((r) => r.policy_type === "weekly")
      .forEach((r) => {
        const d = (r.run_policy.day_of_week || "").trim();
        if (byDay[d]) byDay[d].push(r);
      });
    days.forEach((d) => {
      const col = document.createElement("div");
      col.className = "weekly-day";
      const head = document.createElement("div");
      head.className = "weekly-day-name";
      head.textContent = d;
      col.appendChild(head);
      byDay[d].forEach((e) => {
        const ev = document.createElement("div");
        ev.className = "tl-event weekly" + (e.enabled ? "" : " disabled");
        ev.innerHTML =
          `<span class="tl-event-time">${e.run_policy.time_local || "?"}</span>` +
          `<span class="tl-event-name">${e.name}</span>`;
        ev.title = e.notes || "";
        ev.addEventListener("click", () => openEdit(e));
        col.appendChild(ev);
      });
      if (!byDay[d].length) {
        const empty = document.createElement("div");
        empty.className = "hint";
        empty.textContent = "—";
        col.appendChild(empty);
      }
      root.appendChild(col);
    });
  }

  function renderAllTable() {
    const tb = $("#all-table tbody");
    tb.innerHTML = "";
    // Auto-disabled routines first (most attention-worthy), then enabled,
    // then plain disabled — alphabetical inside each group.
    const sorted = [...state.routines].sort((a, b) => {
      const aDis = !!a.auto_disabled_reason;
      const bDis = !!b.auto_disabled_reason;
      if (aDis !== bDis) return aDis ? -1 : 1;
      if (a.enabled !== b.enabled) return a.enabled ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    sorted.forEach((r) => {
      const tr = document.createElement("tr");
      if (!r.enabled) tr.classList.add("disabled");
      if (r.auto_disabled_reason) tr.classList.add("auto-disabled");

      const triggerBadge = badgeTrigger(r.trigger_type, r.trigger_event_topic);
      const windowBadge  = badgeWindow(r.active_window);
      const watchBadge   = badgeWatchdog(r.max_run_seconds);
      const onErrBadge   = badgeOnError(r.on_error);
      const failBadge    = badgeFailureStreak(r.consecutive_failures);
      const banner       = autoDisableBanner(r);

      tr.innerHTML =
        `<td><span class="pill ${r.enabled ? "on" : "off"}">${r.enabled ? "ON" : "OFF"}</span></td>` +
        `<td>${escape(r.name)}<div class="field-note">${escape(r.id)}</div>${banner}</td>` +
        `<td>${escape(r.runner)}<div class="rt-badges">${triggerBadge}</div></td>` +
        `<td>${escape(r.schedule_label)}</td>` +
        `<td>${windowBadge || "—"}</td>` +
        `<td><div class="rt-badges">${watchBadge}${onErrBadge}${failBadge}</div></td>` +
        `<td>${relTime(r.last_finished_utc)}</td>` +
        `<td><span class="pill ${statusPillClass(r.last_status)}">${r.last_status || "—"}</span></td>` +
        `<td>${r.run_count || 0}</td>` +
        `<td><div class="btn-row">` +
          `<button class="btn" data-act="edit" data-id="${r.id}">Edit</button>` +
          `<button class="btn" data-act="run" data-id="${r.id}">Run now</button>` +
          `<button class="btn" data-act="decisions" data-id="${r.id}">Decisions</button>` +
          `<button class="btn ${r.enabled ? "danger" : "primary"}" data-act="toggle" data-id="${r.id}">` +
          `${r.enabled ? "Disable" : "Enable"}</button>` +
        `</div></td>`;
      tb.appendChild(tr);
    });
    tb.querySelectorAll("button").forEach((b) =>
      b.addEventListener("click", (e) => onActionClick(e.currentTarget))
    );
  }

  // ---- Recent decisions tab ----
  async function renderDecisions() {
    const tb = $("#decisions-table tbody");
    tb.innerHTML = `<tr><td colspan="6" class="hint">Loading…</td></tr>`;
    const filterEl = $("#decisions-filter");
    const filter = (filterEl && filterEl.value || "").trim();
    let url = "/api/routines/decisions?limit=200&days_back=3";
    if (filter) url += "&routine_id=" + encodeURIComponent(filter);
    try {
      const r = await fetch(url);
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      const rows = data.decisions || [];
      tb.innerHTML = "";
      if (!rows.length) {
        tb.innerHTML = `<tr><td colspan="6" class="hint">No decisions in the last 3 days.</td></tr>`;
        return;
      }
      rows.forEach((d) => {
        const tr = document.createElement("tr");
        tr.classList.add("decision-" + (d.event || "unknown"));
        const reasonOrErr = d.error
          ? `<span class="decision-error">${escape(d.error)}</span>`
          : escape(d.reason || "");
        tr.innerHTML =
          `<td>${fmtDateTime(d.ts)}</td>` +
          `<td>${escape(d.routine_id || "—")}</td>` +
          `<td><span class="pill decision ${d.event || ""}">${escape(d.event || "—")}</span></td>` +
          `<td>${reasonOrErr}</td>` +
          `<td>${d.duration_s != null ? d.duration_s + "s" : "—"}</td>` +
          `<td><code>${escape(d.run_id || "")}</code></td>`;
        tb.appendChild(tr);
      });
    } catch (e) {
      tb.innerHTML = `<tr><td colspan="6" class="bad">Failed: ${escape(e.message)}</td></tr>`;
    }
  }

  function findRoutine(id) {
    return state.routines.find((r) => r.id === id);
  }

  async function onActionClick(btn) {
    const id = btn.getAttribute("data-id");
    const act = btn.getAttribute("data-act");
    const routine = findRoutine(id);
    if (!routine) return;
    if (act === "edit") openEdit(routine);
    else if (act === "run") doRunNow(routine);
    else if (act === "toggle") doToggle(routine);
    else if (act === "decisions") {
      // Switch to the decisions tab pre-filtered to this routine.
      const filterEl = $("#decisions-filter");
      if (filterEl) filterEl.value = routine.id;
      switchTab("decisions");
    }
  }

  async function doToggle(routine) {
    try {
      await postJSON(`/api/routines/${routine.id}/toggle`, { enabled: !routine.enabled });
      toast(`${routine.name} → ${routine.enabled ? "disabled" : "enabled"}`, "ok");
      await reload();
    } catch (e) {
      toast(`Toggle failed: ${e.message}`, "bad");
    }
  }

  async function doRunNow(routine) {
    try {
      await postJSON(`/api/routines/${routine.id}/run-now`);
      toast(`Started ${routine.name}`, "ok");
    } catch (e) {
      toast(`Run-now failed: ${e.message}`, "bad");
    }
  }

  function openEdit(routine) {
    const body = $("#edit-modal-body");
    body.innerHTML = "";
    state.editing = { routine, draft: { ...(routine.run_policy || {}) } };
    $("#edit-modal-title").textContent = `Edit · ${routine.name}`;

    const rp = routine.run_policy || {};
    const meta = document.createElement("div");
    meta.className = "field-meta";
    meta.innerHTML =
      `<div><strong>id:</strong> ${routine.id}</div>` +
      `<div><strong>runner:</strong> ${routine.runner}</div>` +
      `<div><strong>type:</strong> ${routine.policy_type || "—"}</div>` +
      `<div><strong>last run:</strong> ${fmtDateTime(routine.last_finished_utc)} (${relTime(routine.last_finished_utc)})</div>` +
      `<div><strong>last status:</strong> ${routine.last_status || "—"}</div>` +
      (routine.last_error ? `<div><strong>last error:</strong> ${routine.last_error}</div>` : "") +
      (routine.notes ? `<div style="margin-top:6px;color:var(--muted);">${routine.notes}</div>` : "");
    body.appendChild(meta);

    const enabledRow = document.createElement("div");
    enabledRow.className = "field-row";
    enabledRow.innerHTML =
      `<label>Enabled</label>` +
      `<select id="edit-enabled"><option value="true">enabled</option><option value="false">disabled</option></select>`;
    body.appendChild(enabledRow);
    enabledRow.querySelector("select").value = routine.enabled ? "true" : "false";

    if (routine.policy_type === "daily" || routine.policy_type === "weekly") {
      if (routine.policy_type === "weekly") {
        const dowRow = document.createElement("div");
        dowRow.className = "field-row";
        dowRow.innerHTML =
          `<label>Day of week</label>` +
          `<select id="edit-dow">` +
          ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            .map((d) => `<option value="${d}">${d}</option>`).join("") +
          `</select>`;
        body.appendChild(dowRow);
        dowRow.querySelector("select").value = rp.day_of_week || "Monday";
      }
      const tRow = document.createElement("div");
      tRow.className = "field-row";
      tRow.innerHTML =
        `<label>Time (local, HH:MM 24h)</label>` +
        `<input id="edit-time" type="time" value="${rp.time_local || "00:00"}">`;
      body.appendChild(tRow);
    } else if (routine.policy_type === "interval") {
      const sec = parseInt(rp.min_interval_seconds || 0, 10);
      const iRow = document.createElement("div");
      iRow.className = "field-row";
      iRow.innerHTML =
        `<label>Interval (seconds)</label>` +
        `<input id="edit-interval" type="number" min="30" max="604800" value="${sec}">` +
        `<div class="field-note">30s minimum, 7d maximum. Common: 60 (1m), 300 (5m), 3600 (1h).</div>`;
      body.appendChild(iRow);
    } else {
      const note = document.createElement("div");
      note.className = "field-note";
      note.textContent = `Policy type "${routine.policy_type}" has no editable schedule fields here.`;
      body.appendChild(note);
    }

    const actionRow = document.createElement("div");
    actionRow.className = "btn-row";
    actionRow.style.marginTop = "10px";
    actionRow.innerHTML = `<button class="btn" id="edit-run-now">Run now</button>`;
    body.appendChild(actionRow);
    actionRow.querySelector("#edit-run-now").addEventListener("click", () => doRunNow(routine));

    $("#edit-modal").classList.remove("hidden");
  }

  function closeEdit() {
    $("#edit-modal").classList.add("hidden");
    state.editing = null;
  }

  async function saveEdit() {
    if (!state.editing) return;
    const routine = state.editing.routine;
    const desiredEnabled = $("#edit-enabled").value === "true";

    try {
      if (desiredEnabled !== routine.enabled) {
        await postJSON(`/api/routines/${routine.id}/toggle`, { enabled: desiredEnabled });
      }
      const patch = {};
      if (routine.policy_type === "daily" || routine.policy_type === "weekly") {
        const t = $("#edit-time").value;
        if (t) patch.time_local = t;
        if (routine.policy_type === "weekly") patch.day_of_week = $("#edit-dow").value;
      } else if (routine.policy_type === "interval") {
        patch.min_interval_seconds = parseInt($("#edit-interval").value, 10);
      }
      if (Object.keys(patch).length) {
        await postJSON(`/api/routines/${routine.id}/policy`, patch);
      }
      toast(`Saved ${routine.name}`, "ok");
      closeEdit();
      await reload();
    } catch (e) {
      toast(`Save failed: ${e.message}`, "bad");
    }
  }

  async function reload() {
    try {
      const data = await fetchRoutines();
      state.routines = data.routines || [];
      state.serverNowLocal = data.now_local;
      state.managerEnabled = !!data.manager_enabled;
      render();
    } catch (e) {
      toast(`Load failed: ${e.message}`, "bad");
    }
  }

  function switchTab(name) {
    $$(".tab").forEach((x) => x.classList.toggle("active", x.getAttribute("data-tab") === name));
    state.activeTab = name;
    $$(".tab-panel").forEach((p) => p.classList.add("hidden"));
    const panel = $("#panel-" + name);
    if (panel) panel.classList.remove("hidden");
    render();
  }

  function bindTabs() {
    $$(".tab").forEach((t) =>
      t.addEventListener("click", () => switchTab(t.getAttribute("data-tab")))
    );
    const filterEl = $("#decisions-filter");
    if (filterEl) {
      filterEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter") renderDecisions();
      });
    }
    const refreshEl = $("#decisions-refresh");
    if (refreshEl) refreshEl.addEventListener("click", () => renderDecisions());
  }

  function bindModal() {
    $("#edit-modal-close").addEventListener("click", closeEdit);
    $("#edit-modal-cancel").addEventListener("click", closeEdit);
    $("#edit-modal-save").addEventListener("click", saveEdit);
    $("#edit-modal").addEventListener("click", (e) => {
      if (e.target.id === "edit-modal") closeEdit();
    });
  }

  function init() {
    bindTabs();
    bindModal();
    reload();
    setInterval(reload, 30000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
