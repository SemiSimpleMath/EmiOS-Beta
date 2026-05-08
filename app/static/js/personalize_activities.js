// Tracked Activities editor — read/write config_tracked_activities.json
// (the base cadence) plus a read-only view of the cron-tickets agent
// prompt (the judgement layer).

(function () {
  "use strict";

  const API = "/api/personalize/activities";

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    return n;
  };
  const text = (s) => document.createTextNode(s == null ? "" : String(s));

  // Reset-trigger options recognized by the activity_recorder. "afk" is
  // sometimes encoded as a separate `reset_on_afk: true` flag rather than
  // an entry in reset_triggers, so we surface it as its own checkbox in
  // the row but write back to the right shape on save.
  const RESET_TRIGGERS = ["chat", "ticket", "calendar"];

  let lastSaved = null; // last-known canonical config from server (deep-clone for revert)

  function setStatus(msg, kind) {
    const el = $("#pz-act-status");
    el.textContent = msg || "";
    el.className = "pz-dir-status" + (kind ? " pz-dir-status--" + kind : "");
  }

  function deepClone(o) {
    return JSON.parse(JSON.stringify(o));
  }

  function field(label, input) {
    const wrap = el("div", "pz-act-field");
    const lbl = el("div", "pz-act-field__label");
    lbl.appendChild(text(label));
    wrap.appendChild(lbl);
    wrap.appendChild(input);
    return wrap;
  }

  function inputText(value, placeholder) {
    const i = el("input", "pz-act-field__input");
    i.type = "text";
    i.value = value == null ? "" : String(value);
    if (placeholder) i.placeholder = placeholder;
    return i;
  }

  function inputNumber(value, min, max, placeholder) {
    const i = el("input", "pz-act-field__input");
    i.type = "number";
    i.min = String(min);
    i.max = String(max);
    if (value != null) i.value = String(value);
    if (placeholder) i.placeholder = placeholder;
    return i;
  }

  function checkbox(label, checked) {
    const wrap = el("label");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = !!checked;
    wrap.appendChild(cb);
    wrap.appendChild(text(" " + label));
    return { wrap, cb };
  }

  function renderRow(key, act) {
    const row = el("div", "pz-act-row");
    row.dataset.key = key;

    const head = el("div", "pz-act-row__head");
    const title = el("div", "pz-act-row__title");
    title.appendChild(text(act.display_name || key));
    head.appendChild(title);
    const k = el("div", "pz-act-row__key");
    k.appendChild(text(key));
    head.appendChild(k);
    row.appendChild(head);

    const grid = el("div", "pz-act-row__grid");

    // display_name
    const nameInput = inputText(act.display_name || "");
    grid.appendChild(field("Display name", nameInput));
    row._name = nameInput;

    // threshold minutes (optional — meals are calendar-driven)
    const minsInput = inputNumber(
      act.threshold && typeof act.threshold.minutes === "number" ? act.threshold.minutes : null,
      1, 1440, "calendar-driven"
    );
    grid.appendChild(field("Threshold (minutes)", minsInput));
    row._threshold = minsInput;

    // calendar keywords (comma-separated)
    const kwInput = inputText(
      Array.isArray(act.calendar_keywords) ? act.calendar_keywords.join(", ") : "",
      "Breakfast, Lunch, Dinner"
    );
    grid.appendChild(field("Calendar keywords", kwInput));
    row._kw = kwInput;

    // guidance
    const guideInput = inputText(act.guidance || "", "e.g. Typical windows: 7-9am, 11am-1pm");
    grid.appendChild(field("Guidance", guideInput));
    row._guidance = guideInput;

    // Boolean flags + reset triggers — collected in one row of checkboxes
    const flagsField = el("div", "pz-act-field");
    const flagsLbl = el("div", "pz-act-field__label");
    flagsLbl.appendChild(text("Flags & reset triggers"));
    flagsField.appendChild(flagsLbl);
    const cbs = el("div", "pz-act-checkboxes");

    const init = checkbox("Init on cold start", !!act.init_on_cold_start);
    cbs.appendChild(init.wrap);
    row._init = init.cb;

    const resetAfk = checkbox("Reset on AFK", !!act.reset_on_afk);
    cbs.appendChild(resetAfk.wrap);
    row._resetAfk = resetAfk.cb;

    const showCount = checkbox("Show daily count", !!act.show_daily_count);
    cbs.appendChild(showCount.wrap);
    row._showCount = showCount.cb;

    row._triggers = {};
    const existingTriggers = Array.isArray(act.reset_triggers) ? act.reset_triggers : [];
    for (const t of RESET_TRIGGERS) {
      const cb = checkbox(`Reset on ${t}`, existingTriggers.indexOf(t) >= 0);
      cbs.appendChild(cb.wrap);
      row._triggers[t] = cb.cb;
    }

    flagsField.appendChild(cbs);
    grid.appendChild(flagsField);

    row.appendChild(grid);

    // Wire dirty detection
    const inputs = [nameInput, minsInput, kwInput, guideInput, init.cb, resetAfk.cb, showCount.cb,
                    ...Object.values(row._triggers)];
    inputs.forEach(i => i.addEventListener("input", refreshDirty));
    inputs.forEach(i => i.addEventListener("change", refreshDirty));

    return row;
  }

  function readCurrent() {
    // Walk DOM, build new config object
    const rows = document.querySelectorAll(".pz-act-row");
    const activities = {};
    for (const row of rows) {
      const key = row.dataset.key;
      const act = {
        display_name: row._name.value.trim(),
        field_name: key,
      };
      const minsRaw = row._threshold.value.trim();
      if (minsRaw !== "") {
        const mins = parseInt(minsRaw, 10);
        if (!isNaN(mins)) {
          act.threshold = {
            minutes: mins,
            label: minutesToLabel(mins),
          };
        }
      }
      const kwRaw = row._kw.value.trim();
      if (kwRaw) {
        act.calendar_keywords = kwRaw.split(",").map(s => s.trim()).filter(Boolean);
      }
      if (row._guidance.value.trim()) {
        act.guidance = row._guidance.value.trim();
      }
      if (row._init.checked) act.init_on_cold_start = true;
      if (row._resetAfk.checked) act.reset_on_afk = true;
      if (row._showCount.checked) act.show_daily_count = true;
      const triggers = RESET_TRIGGERS.filter(t => row._triggers[t].checked);
      if (triggers.length) act.reset_triggers = triggers;
      activities[key] = act;
    }
    return { ...lastSaved, activities };
  }

  function minutesToLabel(mins) {
    if (mins % 60 === 0) {
      const h = mins / 60;
      return h === 1 ? "Every hour" : `Every ${h} hours`;
    }
    return `Every ${mins} minutes`;
  }

  function refreshDirty() {
    const cur = JSON.stringify(readCurrent());
    const saved = JSON.stringify(lastSaved);
    if (cur === saved) {
      setStatus("Saved.", "ok");
      $("#pz-act-revert").disabled = true;
    } else {
      setStatus("Unsaved changes.", "dirty");
      $("#pz-act-revert").disabled = false;
    }
  }

  function applyData(data) {
    if (!data || !data.success) {
      setStatus("Error: " + (data && data.error ? data.error : "load failed"), "err");
      return;
    }
    lastSaved = deepClone(data.config);

    $("#pz-act-config-path").textContent = data.config_path || "";
    $("#pz-act-prompt-path").textContent = data.agent_prompt_path || "";
    $("#pz-act-prompt").textContent = data.agent_prompt || "(prompt not found)";

    const container = $("#pz-act-rows");
    container.innerHTML = "";
    const acts = (data.config && data.config.activities) || {};
    const keys = Object.keys(acts);
    if (keys.length === 0) {
      const e = el("div", "pz-empty");
      e.appendChild(text("No activities configured. Edit the JSON file directly to add new ones."));
      container.appendChild(e);
    } else {
      for (const key of keys) {
        container.appendChild(renderRow(key, acts[key]));
      }
    }
    refreshDirty();
  }

  async function load() {
    setStatus("Loading…");
    try {
      const res = await fetch(API, { credentials: "same-origin" });
      const data = await res.json();
      applyData(data);
    } catch (err) {
      setStatus("Error: " + String(err), "err");
    }
  }

  async function save() {
    const btn = $("#pz-act-save");
    btn.disabled = true;
    setStatus("Saving…");
    const config = readCurrent();
    try {
      const res = await fetch(API, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config }),
      });
      const data = await res.json();
      if (!data || !data.success) {
        setStatus("Save failed: " + (data && data.error ? data.error : res.status), "err");
        return;
      }
      lastSaved = deepClone(data.config);
      refreshDirty();
    } catch (err) {
      setStatus("Save failed: " + String(err), "err");
    } finally {
      btn.disabled = false;
    }
  }

  function revert() {
    // Re-render from last-saved snapshot
    applyData({ success: true, config: lastSaved,
                config_path: $("#pz-act-config-path").textContent,
                agent_prompt: $("#pz-act-prompt").textContent,
                agent_prompt_path: $("#pz-act-prompt-path").textContent });
  }

  document.addEventListener("DOMContentLoaded", () => {
    load();
    $("#pz-act-save").addEventListener("click", save);
    $("#pz-act-revert").addEventListener("click", revert);
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        save();
      }
    });
  });
})();
