(function (global) {
  "use strict";

  // ─── State ────────────────────────────────────────────────────────────────

  let _panelVisible = false;
  let _compiledTaskId = null;
  let _compilingSessionId = null;
  let _currentMarkdown = "";
  let _draftId = null;         // set after first save; reused for subsequent saves
  let _saveTimeout = null;     // debounce handle
  let _currentSocketId = "";   // tracked for compile POST

  // ─── DOM helpers ──────────────────────────────────────────────────────────

  function _getPanel() {
    return document.getElementById("task-create-panel");
  }

  function _getPreviewEl() {
    return document.getElementById("task-create-preview");
  }

  function _getStatusEl() {
    return document.getElementById("task-create-status");
  }

  function _getViewBtn() {
    return document.getElementById("task-create-view-btn");
  }

  function _getSaveBtn() {
    return document.getElementById("task-create-save-btn");
  }

  function _getCompileBtn() {
    return document.getElementById("task-create-compile-btn");
  }

  // ─── Panel lifecycle ──────────────────────────────────────────────────────

  function showPanel() {
    if (_panelVisible) return;
    _panelVisible = true;
    _compiledTaskId = null;
    _compilingSessionId = null;

    let panel = _getPanel();
    if (!panel) {
      panel = _buildPanel();
      document.body.appendChild(panel);
    }

    panel.style.display = "flex";
    requestAnimationFrame(() => {
      panel.style.opacity = "1";
      panel.style.transform = "translateX(0)";
    });

    _setStatus("Drafting task spec…");
    _setMarkdown("");
    _hideViewBtn();
    _setSaveBtnState("idle");
    _setCompileBtnState("idle");
    _currentSocketId = "";
  }

  // Open panel immediately with blank template — called on /task create before server responds
  function showPanelEmpty() {
    _compiledTaskId = null;
    _compilingSessionId = null;
    _draftId = null;

    let panel = _getPanel();
    if (!panel) {
      panel = _buildPanel();
      document.body.appendChild(panel);
    }

    if (!_panelVisible) {
      _panelVisible = true;
      panel.style.display = "flex";
      requestAnimationFrame(() => {
        panel.style.opacity = "1";
        panel.style.transform = "translateX(0)";
      });
    }

    _setStatus("Waiting for first input…");
    _setMarkdown("");
    _hideViewBtn();
    _setSaveBtnState("idle");
    _setCompileBtnState("idle");
    _currentSocketId = "";
  }

  // Show list of available tasks — called on /task load (no name)
  function showTaskList() {
    let panel = _getPanel();
    if (!panel) {
      panel = _buildPanel();
      document.body.appendChild(panel);
    }
    if (!_panelVisible) {
      _panelVisible = true;
      panel.style.display = "flex";
      requestAnimationFrame(() => {
        panel.style.opacity = "1";
        panel.style.transform = "translateX(0)";
      });
    }
    _setStatus("Loading available tasks…", true);
    _setMarkdown("");

    fetch("/task-graph/spec/list")
      .then(function (res) { return res.json(); })
      .then(function (data) {
        const tasks = (data && Array.isArray(data.tasks)) ? data.tasks : [];
        if (!tasks.length) {
          _setStatus("No task specs found.");
          return;
        }
        // Render a simple list as markdown so the editor shows it
        const lines = ["# Available Tasks", ""];
        tasks.forEach(function (t) {
          const label = t.name && t.name !== t.task_id ? t.name + " (`" + t.task_id + "`)" : "`" + t.task_id + "`";
          const desc = t.description ? " — " + t.description : "";
          lines.push("- " + label + desc);
        });
        lines.push("", "_Type `/task load <name>` to load a specific task._");
        _setMarkdown(lines.join("\n"));
        _setStatus("Found " + tasks.length + " task" + (tasks.length === 1 ? "" : "s") + ". Type /task load <name> to load one.");
      })
      .catch(function (err) {
        _setStatus("Failed to load task list: " + String(err).slice(0, 60));
      });
  }

  // Load a task spec by name — called on /task load <name>
  // Opens panel in loading state, fetches spec from server, then populates it
  function loadTaskByName(taskName) {    _compiledTaskId = null;
    _compilingSessionId = null;
    _draftId = null;

    let panel = _getPanel();
    if (!panel) {
      panel = _buildPanel();
      document.body.appendChild(panel);
    }

    if (!_panelVisible) {
      _panelVisible = true;
      panel.style.display = "flex";
      requestAnimationFrame(() => {
        panel.style.opacity = "1";
        panel.style.transform = "translateX(0)";
      });
    }

    _setStatus("Loading task spec…", true);
    _setMarkdown("");
    _hideViewBtn();
    _currentSocketId = "";

    fetch("/task-graph/spec/load?name=" + encodeURIComponent(taskName))
      .then(function (res) {
        if (!res.ok) {
          return res.json().then(function (d) {
            throw new Error((d && d.error) ? d.error : ("HTTP " + res.status));
          }).catch(function () {
            throw new Error("HTTP " + res.status);
          });
        }
        return res.json();
      })
      .then(function (data) {
        const md = (data && data.spec_markdown) ? String(data.spec_markdown) : "";
        const taskId = (data && data.task_id) ? String(data.task_id) : "";
        const compiledId = (data && data.compiled_task_id) ? String(data.compiled_task_id) : "";

        if (md) {
          _setMarkdown(md);
          _setStatus("Loaded: " + (taskId || taskName));
          setTimeout(() => {
            if (_panelVisible) _setStatus("Task spec loaded.");
          }, 1500);
        } else {
          _setStatus("No spec content found for: " + taskName);
        }

        if (compiledId) {
          _showViewBtn(compiledId);
        }
      })
      .catch(function (err) {
        _setStatus("Load failed: " + String(err).slice(0, 80));
      });
  }

  function hidePanel() {
    _panelVisible = false;
    const panel = _getPanel();
    if (!panel) return;
    panel.style.opacity = "0";
    panel.style.transform = "translateX(24px)";
    setTimeout(() => {
      if (!_panelVisible) panel.style.display = "none";
    }, 220);

    // Send /end to exit task creation mode and return to normal chat.
    const formData = new FormData();
    formData.append("text", "/end");
    formData.append("elapsed_time", "0");
    formData.append("speaking_mode", "false");
    fetch("/process_request", { method: "POST", body: formData }).catch(() => {});
  }

  function _buildPanel() {
    const panel = document.createElement("div");
    panel.id = "task-create-panel";
    panel.className = "task-create-panel";
    panel.style.cssText = [
      "position: fixed",
      "top: 0",
      "right: 0",
      "width: calc(100vw / 3)",
      "height: 100vh",
      "background: #13161f",
      "border-left: 1px solid #1e2535",
      "display: none",
      "flex-direction: column",
      "z-index: 1000",
      "opacity: 0",
      "transform: translateX(24px)",
      "transition: opacity 0.2s ease, transform 0.2s ease",
      "font-family: 'Inter', 'Segoe UI', sans-serif",
    ].join("; ");

    panel.innerHTML = `
      <div style="
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 14px 16px;
        border-bottom: 1px solid #1e2535;
        flex-shrink: 0;
      ">
        <div style="display:flex;align-items:center;gap:8px">
          <span style="color:#4a9fd4;font-size:16px">◈</span>
          <span style="color:#c8d0df;font-weight:700;font-size:14px">Task Spec</span>
        </div>
        <div style="display:flex;align-items:center;gap:8px">
          <button id="task-create-reset-btn" style="
            background:none;border:1px solid #2a3a50;color:#556070;cursor:pointer;
            font-size:11px;line-height:1;padding:4px 8px;border-radius:4px;
          " aria-label="Reset to blank" title="Reset to blank">Reset</button>
          <button id="task-create-close-btn" style="
            background:none;border:none;color:#556070;cursor:pointer;
            font-size:20px;line-height:1;padding:0 2px;
          " aria-label="Close task spec panel">×</button>
        </div>
      </div>

      <div id="task-create-status" style="
        padding: 8px 16px;
        font-size: 12px;
        color: #5a7fa8;
        border-bottom: 1px solid #1e2535;
        flex-shrink: 0;
        min-height: 34px;
        display: flex;
        align-items: center;
        gap: 8px;
      "></div>

      <div id="task-create-preview" style="
        flex: 1;
        overflow-y: auto;
        padding: 16px;
        font-size: 13px;
        line-height: 1.65;
        color: #9aabbb;
        white-space: pre-wrap;
        word-break: break-word;
        font-family: 'Inter', 'Segoe UI', sans-serif;
      ">
        <span style="color:#3a4a5a;font-style:italic">Waiting for first input…</span>
      </div>

      <div style="
        padding: 12px 16px;
        border-top: 1px solid #1e2535;
        flex-shrink: 0;
        display: flex;
        flex-direction: column;
        gap: 8px;
      ">
        <button id="task-create-save-btn" style="
          display: none;
          background: #1a2535;
          color: #7a9ec0;
          border: 1px solid #2a3a50;
          border-radius: 8px;
          padding: 8px 16px;
          font-size: 12px;
          font-weight: 600;
          width: 100%;
          box-sizing: border-box;
          cursor: pointer;
          text-align: center;
          transition: background 0.15s, color 0.15s, border-color 0.15s;
          letter-spacing: 0.02em;
        ">Save Draft</button>
        <button id="task-create-compile-btn" style="
          display: none;
          background: #1a3a2a;
          color: #5abf8a;
          border: 1px solid #2a5040;
          border-radius: 8px;
          padding: 8px 16px;
          font-size: 12px;
          font-weight: 600;
          width: 100%;
          box-sizing: border-box;
          cursor: pointer;
          text-align: center;
          transition: background 0.15s, color 0.15s, border-color 0.15s;
          letter-spacing: 0.02em;
        ">⚡ Compile Task</button>
        <a id="task-create-view-btn" href="#" target="_blank" style="
          display: none;
          text-decoration: none;
          background: #2d6a9f;
          color: #e8ecf3;
          border-radius: 8px;
          padding: 9px 16px;
          font-size: 13px;
          font-weight: 600;
          width: 100%;
          box-sizing: border-box;
          text-align: center;
          transition: background 0.15s;
        ">View Task Graph →</a>
      </div>
    `;

    panel.querySelector("#task-create-close-btn").addEventListener("click", hidePanel);
    panel.querySelector("#task-create-reset-btn").addEventListener("click", _onResetClick);
    panel.querySelector("#task-create-save-btn").addEventListener("click", _onSaveClick);
    panel.querySelector("#task-create-compile-btn").addEventListener("click", _onCompileClick);

    return panel;
  }

  // ─── Save draft logic ─────────────────────────────────────────────────────

  function _onSaveClick() {
    if (!_currentMarkdown || !_currentMarkdown.trim()) return;
    _doSave();
  }

  function _doSave() {
    const md = _currentMarkdown;
    if (!md || !md.trim()) return;

    _setSaveBtnState("saving");

    fetch("/task-graph/spec/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        markdown: md,
        draft_id: _draftId || undefined,
        session_id: _compilingSessionId || undefined,
      }),
    })
      .then((res) => {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then((data) => {
        _draftId = data.draft_id || _draftId;
        _setSaveBtnState("saved");
        setTimeout(() => _setSaveBtnState("idle"), 2200);
      })
      .catch((err) => {
        _setSaveBtnState("error");
        _setStatus("Save failed: " + String(err).slice(0, 60));
        setTimeout(() => _setSaveBtnState("idle"), 3000);
      });
  }

  function _setSaveBtnState(state) {
    const btn = _getSaveBtn();
    if (!btn) return;

    // Show once we have content
    if (_currentMarkdown && _currentMarkdown.trim()) {
      btn.style.display = "block";
    }

    const styles = {
      idle:   { bg: "#1a2535", color: "#7a9ec0", border: "#2a3a50", text: "Save Draft", cursor: "pointer" },
      saving: { bg: "#152030", color: "#4a7090", border: "#1e3045", text: "Saving…",    cursor: "default" },
      saved:  { bg: "#152a20", color: "#4aaa70", border: "#1e3830", text: "✓ Saved",    cursor: "default" },
      error:  { bg: "#2a1520", color: "#aa4a50", border: "#381e20", text: "Save failed", cursor: "pointer" },
    };

    const s = styles[state] || styles.idle;
    btn.style.background = s.bg;
    btn.style.color = s.color;
    btn.style.borderColor = s.border;
    btn.textContent = s.text;
    btn.style.cursor = s.cursor;
    btn.disabled = (state === "saving");
  }

  function _onResetClick() {
    const blank = "# New Task\n\n## Goal\n[Describe the goal here]\n\n## Steps\n1. **[Step 1]** — [what happens]\n\n## Completion\n[When is this task done?]";
    _currentMarkdown = blank;
    _draftId = null;
    _compiledTaskId = null;
    _setMarkdown(blank);
    _setStatus("Reset to blank.");
    _setSaveBtnState("idle");
    _setCompileBtnState("idle");
    _hideViewBtn();
  }

  // ─── Compile logic ────────────────────────────────────────────────────────

  function _onCompileClick() {
    if (!_currentMarkdown || !_currentMarkdown.trim()) return;
    _doCompile();
  }

  function _doCompile() {
    const md = _currentMarkdown;
    if (!md || !md.trim()) return;

    // Prefer the socket_id pushed from the server via task_draft_update; fall back
    // to the live socket id so button-only compiles (no prior chat turn) still work.
    const socketId = _currentSocketId
      || (typeof window.EmiGetSocketId === "function" ? window.EmiGetSocketId() : "")
      || "";

    _setCompileBtnState("compiling");
    _setStatus("Compiling task graph…", true);
    _hideViewBtn();

    fetch("/task-graph/spec/compile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        markdown: md,
        session_id: _draftId || undefined,
        socket_id: socketId || undefined,
      }),
    })
      .then((res) => {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then((data) => {
        _compilingSessionId = data.session_id || null;
        // Status + button will be updated by handleTaskCompiled / handleTaskCompileFailed
      })
      .catch((err) => {
        _setCompileBtnState("idle");
        _setStatus("Compile request failed: " + String(err).slice(0, 60));
      });
  }

  function _setCompileBtnState(state) {
    const btn = _getCompileBtn();
    if (!btn) return;

    if (_currentMarkdown && _currentMarkdown.trim()) {
      btn.style.display = "block";
    }

    const styles = {
      idle:      { bg: "#1a3a2a", color: "#5abf8a", border: "#2a5040", text: "⚡ Compile Task", cursor: "pointer",  disabled: false },
      compiling: { bg: "#152a20", color: "#3a8a60", border: "#1e3830", text: "Compiling…",     cursor: "default",  disabled: true  },
      done:      { bg: "#152a20", color: "#3aaa70", border: "#1e3830", text: "✓ Compiled",     cursor: "default",  disabled: false },
      error:     { bg: "#2a1520", color: "#aa4a50", border: "#381e20", text: "Compile failed", cursor: "pointer",  disabled: false },
    };

    const s = styles[state] || styles.idle;
    btn.style.background = s.bg;
    btn.style.color = s.color;
    btn.style.borderColor = s.border;
    btn.textContent = s.text;
    btn.style.cursor = s.cursor;
    btn.disabled = s.disabled;
  }

  // ─── Content updates ──────────────────────────────────────────────────────

  function _setMarkdown(md) {
    _currentMarkdown = md || "";
    const el = _getPreviewEl();
    if (!el) return;
    if (!md || !md.trim()) {
      el.innerHTML = '<span style="color:#3a4a5a;font-style:italic">Waiting for first input…</span>';
      _getSaveBtn() && (_getSaveBtn().style.display = "none");
      return;
    }
    el.textContent = md;
    _applyMarkdownStyles(el);
    // Show the save button once there's content
    _setSaveBtnState("idle");
    _setCompileBtnState("idle");
  }

  function _applyMarkdownStyles(el) {
    const raw = el.textContent;
    const lines = raw.split("\n");
    const htmlParts = [];
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];

      // Detect step header: "1. **Step Name** ..."
      if (/^\d+\.\s+\*\*/.test(line)) {
        const stepHeaderHtml = _mdLine(line);
        i++;

        // Collect prose (indented text, not a bullet) and detail lines (bullets).
        let prose = "";
        const details = [];
        while (i < lines.length) {
          const next = lines[i];
          // Stop at next step header, section header, or non-indented non-empty line.
          if (/^\d+\.\s+\*\*/.test(next) || /^#{1,3}\s/.test(next)) break;
          if (next.trim() === "") { i++; break; }

          if (/^\s+- /.test(next) || /^\s{5,}/.test(next)) {
            // Detail line (bullet or deeply indented tool line).
            details.push(next);
          } else if (/^\s{2,}/.test(next) && !prose) {
            // First indented non-bullet = prose description.
            prose = next;
          } else if (/^\s{2,}/.test(next) && prose) {
            // Additional indented text after prose = detail line.
            details.push(next);
          } else {
            break;
          }
          i++;
        }

        // Build the step block.
        const stepId = "step-detail-" + Math.random().toString(36).slice(2, 8);
        const hasDetails = details.length > 0;
        const toggleBtn = hasDetails
          ? '<span class="step-toggle" data-target="' + stepId + '" title="Toggle details">&#9656;</span>'
          : '';
        htmlParts.push('<div class="task-step-block">');
        htmlParts.push('<div class="task-step-header">' + toggleBtn + stepHeaderHtml + '</div>');
        if (prose) {
          htmlParts.push('<div class="task-step-prose">' + _mdLine(prose) + '</div>');
        }
        if (hasDetails) {
          htmlParts.push('<div class="task-step-details" id="' + stepId + '">');
          for (const d of details) {
            htmlParts.push('<div class="task-step-detail-line">' + _mdLine(d) + '</div>');
          }
          htmlParts.push('</div>');
        }
        htmlParts.push('</div>');
        continue;
      }

      // Non-step lines: normal markdown rendering.
      htmlParts.push(_mdLine(line) + "<br>");
      i++;
    }

    el.innerHTML = htmlParts.join("\n");

    // Attach toggle listeners.
    el.querySelectorAll(".step-toggle").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const target = document.getElementById(btn.getAttribute("data-target"));
        if (!target) return;
        const open = target.style.display !== "none";
        target.style.display = open ? "none" : "block";
        btn.innerHTML = open ? "&#9656;" : "&#9662;";  // ▸ vs ▾
        btn.classList.toggle("step-toggle-open", !open);
      });
    });
  }

  /** Render a single markdown line to styled HTML. */
  function _mdLine(line) {
    return line
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/^# (.+)$/, '<span style="color:#c8d0df;font-size:15px;font-weight:700;display:block;margin:12px 0 6px">$1</span>')
      .replace(/^## (.+)$/, '<span style="color:#a8b8cc;font-size:13px;font-weight:700;display:block;margin:10px 0 4px;letter-spacing:.03em">$1</span>')
      .replace(/^### (.+)$/, '<span style="color:#8899aa;font-size:12px;font-weight:700;display:block;margin:8px 0 3px">$1</span>')
      .replace(/\*\*(.+?)\*\*/g, '<strong style="color:#b8c8d8">$1</strong>')
      .replace(/`([^`]+)`/g, '<code style="background:#ffffff0f;border-radius:3px;padding:1px 5px;font-family:monospace;font-size:11px;color:#9b6ecf">$1</code>');
  }

  function _setStatus(text, isSpinner = false) {
    const el = _getStatusEl();
    if (!el) return;
    const spinner = isSpinner ? '<span class="task-create-spinner"></span>' : "";
    el.innerHTML = spinner + text;
  }

  function _showViewBtn(taskId) {
    const btn = _getViewBtn();
    if (!btn) return;
    btn.href = `/task-graph/?task_id=${encodeURIComponent(taskId)}`;
    btn.style.display = "block";
  }

  function _hideViewBtn() {
    const btn = _getViewBtn();
    if (btn) btn.style.display = "none";
  }

  // ─── Socket event handlers (called from Emi.js widget dispatch) ───────────

  function handleTaskDraftUpdate(item) {
    const md = (item && item.spec_markdown) ? String(item.spec_markdown) : "";
    if (item && item.socket_id) _currentSocketId = String(item.socket_id);
    if (item && item.session_id) _compilingSessionId = String(item.session_id);
    if (!_panelVisible) showPanel();
    _setStatus("Updating spec…");
    _setMarkdown(md);
    setTimeout(() => _setStatus("Drafting task spec…"), 800);
  }

  function handleTaskCompiling(item) {
    const sessionId = (item && item.session_id) ? String(item.session_id) : "";
    _compilingSessionId = sessionId;
    if (!_panelVisible) showPanel();
    _setStatus("Compiling task graph…", true);
    _setCompileBtnState("compiling");
    _hideViewBtn();
  }

  function handleTaskCompiled(item) {
    const compiledTaskId = (item && item.compiled_task_id) ? String(item.compiled_task_id) : "";
    _compiledTaskId = compiledTaskId;
    _compilingSessionId = null;
    _setStatus("Task compiled successfully.");
    _setCompileBtnState("done");
    if (compiledTaskId) {
      _showViewBtn(compiledTaskId);
    }
  }

  function handleTaskCompileFailed(item) {
    const error = (item && item.error) ? String(item.error) : "Unknown error";
    _compilingSessionId = null;
    _setStatus("Compile failed: " + error.slice(0, 80));
    _setCompileBtnState("error");
  }

  // ─── Spinner + button CSS ─────────────────────────────────────────────────

  function _injectStyles() {
    if (document.getElementById("task-create-panel-styles")) return;
    const style = document.createElement("style");
    style.id = "task-create-panel-styles";
    style.textContent = `
      @keyframes task-spin {
        to { transform: rotate(360deg); }
      }
      .task-create-spinner {
        display: inline-block;
        width: 10px;
        height: 10px;
        border: 2px solid #2a3a4a;
        border-top-color: #4a9fd4;
        border-radius: 50%;
        animation: task-spin 0.7s linear infinite;
        margin-right: 7px;
        vertical-align: middle;
      }
      .task-create-panel::-webkit-scrollbar { width: 4px; }
      .task-create-panel::-webkit-scrollbar-track { background: #13161f; }
      .task-create-panel::-webkit-scrollbar-thumb { background: #2a3040; border-radius: 2px; }
      #task-create-view-btn:hover { background: #3a7abf !important; }
      #task-create-save-btn:hover:not(:disabled) { background: #1e2e42 !important; color: #9ab8d8 !important; border-color: #3a4e68 !important; }
      #task-create-compile-btn:hover:not(:disabled) { background: #1e4a34 !important; color: #7adfa8 !important; border-color: #3a6050 !important; }
      #task-create-preview::-webkit-scrollbar { width: 4px; }
      #task-create-preview::-webkit-scrollbar-track { background: #13161f; }
      #task-create-preview::-webkit-scrollbar-thumb { background: #2a3040; border-radius: 2px; }

      /* Step block accordion */
      .task-step-block {
        margin: 4px 0 2px;
        padding-left: 4px;
      }
      .task-step-header {
        display: flex;
        align-items: baseline;
        gap: 4px;
      }
      .task-step-prose {
        padding-left: 22px;
        color: #9aabbb;
        margin: 2px 0 1px;
      }
      .task-step-details {
        display: none;
        padding-left: 22px;
        margin: 2px 0 0;
        border-left: 2px solid #1e2535;
        padding-bottom: 2px;
      }
      .task-step-detail-line {
        color: #6a7a8a;
        font-size: 12px;
        padding: 1px 0 1px 6px;
      }
      .task-step-detail-line code {
        font-size: 10.5px;
      }
      .step-toggle {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 16px;
        height: 16px;
        flex-shrink: 0;
        cursor: pointer;
        color: #4a6a8a;
        font-size: 11px;
        border-radius: 3px;
        transition: color 0.15s, background 0.15s;
        user-select: none;
      }
      .step-toggle:hover {
        color: #7a9ec0;
        background: #ffffff0a;
      }
      .step-toggle-open {
        color: #4a9fd4;
      }
    `;
    document.head.appendChild(style);
  }

  // ─── Widget dispatch entry points ─────────────────────────────────────────

  function dispatchWidget(items) {
    if (!Array.isArray(items)) return;
    for (const item of items) {
      const type = (item && item.data_type) ? item.data_type : "";
      switch (type) {
        case "task_draft_update":
          handleTaskDraftUpdate(item);
          break;
        case "task_compiling":
          handleTaskCompiling(item);
          break;
        case "task_compiled":
          handleTaskCompiled(item);
          break;
        case "task_compile_failed":
          handleTaskCompileFailed(item);
          break;
        case "task_load_request":
          if (item.task_name) {
            loadTaskByName(item.task_name);
          } else {
            showTaskList();
          }
          break;
        default:
          break;
      }
    }
  }

  // ─── Init ─────────────────────────────────────────────────────────────────

  function init() {
    _injectStyles();
  }

  // ─── Public API ───────────────────────────────────────────────────────────

  global.EmiTaskCreatePanel = {
    init,
    showPanel,
    showPanelEmpty,
    loadTaskByName,
    showTaskList,
    hidePanel,
    dispatchWidget,
    handleTaskDraftUpdate,
    handleTaskCompiling,
    handleTaskCompiled,
    handleTaskCompileFailed,
  };
})(window);
