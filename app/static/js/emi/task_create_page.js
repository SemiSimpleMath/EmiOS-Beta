(function () {
  "use strict";

  // ─── State ────────────────────────────────────────────────────────────────

  let _socket = null;
  let _socketId = "";
  let _currentMarkdown = "";
  let _draftId = null;
  let _sessionId = null;
  let _taskId = "";
  let _roomId = "task_create";  // Default, updated after init
  // Guards: don't let the user send or interact with the backend until the
  // load/init HTTP roundtrip has finalized _roomId AND the socket has emitted
  // register_chat_client for the final room_id. Without this, early messages
  // route to the default "task_create" binding while the UI thinks it's in
  // task_spec::<id>, and the server has no way to deliver the reply back.
  let _roomReady = false;
  const _pendingOutbound = [];

  const BLANK_SPEC = "## Title\n[New Task]\n\n## Description\n[TBD]\n\n## Steps\n[No steps defined yet]\n\n## Completion\n[TBD]";

  // ─── DOM ──────────────────────────────────────────────────────────────────

  function _el(id) { return document.getElementById(id); }

  function _addMessage(text, role) {
    const container = _el("tc-chat-messages");
    // Remove placeholder
    const ph = container.querySelector(".tc-chat__placeholder");
    if (ph) ph.remove();

    const div = document.createElement("div");
    div.className = "tc-msg tc-msg--" + role;
    div.textContent = text;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
  }

  function _setSpec(markdown) {
    _currentMarkdown = markdown;
    const el = _el("tc-spec-content");
    if (!markdown || !markdown.trim()) {
      el.innerHTML = '<div class="tc-spec__placeholder">Your task spec will appear here as you describe it.</div>';
      return;
    }
    el.innerHTML = _renderMarkdown(markdown);
  }

  function _setStatus(text, html) {
    const el = _el("tc-spec-status");
    if (!el) return;
    if (html) {
      el.innerHTML = text;
    } else {
      el.textContent = text;
    }
  }

  // ─── Simple markdown renderer ─────────────────────────────────────────────

  function _renderMarkdown(md) {
    const lines = md.split("\n");
    let html = "";
    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed.startsWith("# ")) {
        html += "<h1>" + _esc(trimmed.slice(2)) + "</h1>";
      } else if (trimmed.startsWith("## ")) {
        html += "<h2>" + _esc(trimmed.slice(3)) + "</h2>";
      } else if (/^\d+\.\s/.test(trimmed)) {
        html += "<div style='margin:4px 0 4px 16px'>" + _inlineMd(trimmed) + "</div>";
      } else if (trimmed.startsWith("- ")) {
        html += "<div style='margin:2px 0 2px 16px'>" + _inlineMd(trimmed) + "</div>";
      } else if (trimmed === "") {
        html += "<div style='height:8px'></div>";
      } else {
        html += "<div>" + _inlineMd(trimmed) + "</div>";
      }
    }
    return html;
  }

  function _esc(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  function _inlineMd(s) {
    let h = _esc(s);
    // Bold: **text**
    h = h.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    // Italic: *text*
    h = h.replace(/\*(.+?)\*/g, "<em>$1</em>");
    // Code: `text`
    h = h.replace(/`(.+?)`/g, "<code style='background:#1a2030;padding:1px 4px;border-radius:3px;font-size:12px'>$1</code>");
    return h;
  }

  // ─── Socket ───────────────────────────────────────────────────────────────

  function _initSocket() {
    _socket = io({ transports: ["websocket", "polling"] });

    if (window.EmiSocketHeartbeat) {
      window.EmiSocketHeartbeat.install({ socket: _socket, getRoomId: () => (_roomId || "task_spec") });
    }

    _socket.on("connect", function () {
      _socketId = _socket.id;
      var payload = { room_id: _roomId || "task_spec" };
      var since = window.EmiSocketHeartbeat && window.EmiSocketHeartbeat.getLastMessageTs();
      if (since) payload.since_ts = since;
      _socket.emit("register_chat_client", payload);
    });

    _socket.on("user_message_data", function (data) {
      if (!data) return;
      if (window.EmiSocketHeartbeat) window.EmiSocketHeartbeat.noteIncoming(data);

      // Ignore proactive/broadcast messages not meant for this room.
      var subTypes = data.sub_data_type || [];
      if (Array.isArray(subTypes) && (subTypes.indexOf("proactive") >= 0 || subTypes.indexOf("situation_audit") >= 0)) {
        return;
      }

      // Chat response
      if (data.chat && typeof data.chat === "string" && data.chat.trim()) {
        _addMessage(data.chat, "assistant");
      }

      // Widget data (spec updates, compile status)
      var items = data.widget_data || [];
      for (var i = 0; i < items.length; i++) {
        var item = items[i];
        if (!item || !item.data_type) continue;
        if (item.data_type === "task_draft_update" && item.spec_markdown) {
          _setSpec(item.spec_markdown);
          _setStatus("Updated");
          if (item.session_id) _sessionId = item.session_id;
        }
        if (item.data_type === "task_compiling") {
          _setStatus("Compiling...");
        }
        if (item.data_type === "task_compiled") {
          var tid = item.compiled_task_id || "";
          if (tid) {
            _setStatus('Compiled — <a href="/task-graph/?task_id=' + encodeURIComponent(tid) + '" target="_blank" style="color:#5abf8a">View Flow</a>', true);
          } else {
            _setStatus("Compiled");
          }
          _setCompileInFlight(false);
        }
        if (item.data_type === "task_compile_failed") {
          _setStatus("Compile failed");
          _setCompileInFlight(false);
        }
      }
    });
  }

  // ─── Send message ─────────────────────────────────────────────────────────

  function _send(text) {
    if (!text || !text.trim()) return;
    _addMessage(text, "user");

    if (!_roomReady) {
      // Queue and flush once _markRoomReady() fires. This protects against
      // early-send races where _roomId is still the default but the real
      // task_spec::<id> binding is still being set up server-side.
      _pendingOutbound.push(text);
      _setStatus("Connecting…");
      return;
    }
    _sendNow(text);
  }

  function _sendNow(text) {
    const formData = new FormData();
    formData.append("text", text);
    formData.append("room_id", _roomId);

    fetch("/process_request", {
      method: "POST",
      body: formData,
    }).catch(function (err) {
      _addMessage("Failed to send: " + err.message, "assistant");
    });
  }

  function _markRoomReady() {
    if (_roomReady) return;
    _roomReady = true;
    // Flush anything the user typed before the room binding was ready.
    const queued = _pendingOutbound.splice(0, _pendingOutbound.length);
    for (const text of queued) { _sendNow(text); }
  }

  // ─── Save ─────────────────────────────────────────────────────────────────

  function _save() {
    if (!_currentMarkdown || !_currentMarkdown.trim()) return;
    _setStatus("Saving...");

    fetch("/task-graph/spec/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        markdown: _currentMarkdown,
        draft_id: _draftId || undefined,
        session_id: _sessionId || undefined,
      }),
    })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (data.draft_id) _draftId = data.draft_id;
        _setStatus("Saved");
      })
      .catch(function () {
        _setStatus("Save failed");
      });
  }

  // ─── Compile ──────────────────────────────────────────────────────────────

  var _compileInFlight = false;

  function _setCompileInFlight(flag) {
    _compileInFlight = !!flag;
    var btn = _el("tc-compile-btn");
    if (btn) btn.disabled = _compileInFlight;
  }

  function _compile() {
    if (_compileInFlight) return;
    if (!_currentMarkdown || !_currentMarkdown.trim()) return;
    _setCompileInFlight(true);
    _send("compile");
  }

  // ─── Reset ────────────────────────────────────────────────────────────────

  function _reset() {
    _currentMarkdown = BLANK_SPEC;
    _draftId = null;
    _setSpec(BLANK_SPEC);
    _setStatus("Reset");
    // Clear chat
    const container = _el("tc-chat-messages");
    container.innerHTML = '<div class="tc-chat__placeholder">Describe your task and I\'ll help you build it.</div>';
  }

  // ─── Init ─────────────────────────────────────────────────────────────────

  function init() {
    _initSocket();

    // Chat input
    const input = _el("tc-chat-input");
    const sendBtn = _el("tc-chat-send");

    sendBtn.addEventListener("click", function () {
      _send(input.value);
      input.value = "";
    });

    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        _send(input.value);
        input.value = "";
      }
    });

    // Header buttons
    _el("tc-reset-btn").addEventListener("click", _reset);
    _el("tc-save-btn").addEventListener("click", _save);
    _el("tc-compile-btn").addEventListener("click", _compile);

    // Show blank template immediately
    _setSpec(BLANK_SPEC);

    // Check URL params for load or prompt
    const params = new URLSearchParams(window.location.search);
    const loadName = params.get("load");
    const prompt = params.get("prompt");

    if (loadName) {
      // Load existing task spec — persists to unified_log, returns room_id.
      _setStatus("Loading...");
      fetch("/task-graph/spec/load?name=" + encodeURIComponent(loadName))
        .then(function (res) { return res.json(); })
        .then(function (data) {
          if (data.spec_markdown) {
            _setSpec(data.spec_markdown);
            if (data.task_id) _taskId = data.task_id;
            if (data.room_id) {
              _roomId = data.room_id;
              if (_socket && _socket.connected) _socket.emit("register_chat_client", { room_id: _roomId });
            }
            _setStatus("Loaded: " + (data.task_id || loadName));
            _markRoomReady();
            _send("I loaded the task called " + loadName + ". Let me know if you'd like to make changes.");
          } else if (data.error) {
            _setStatus("Not found: " + loadName);
          }
        })
        .catch(function () { _setStatus("Failed to load"); });
    } else {
      // New task — allocate task_id + room_id from backend.
      fetch("/task-graph/spec/init", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      })
        .then(function (res) { return res.json(); })
        .then(function (data) {
          if (data.task_id) _taskId = data.task_id;
          if (data.room_id) {
            _roomId = data.room_id;
            if (_socket && _socket.connected) _socket.emit("register_chat_client", { room_id: _roomId });
          }
          if (data.spec_markdown) _setSpec(data.spec_markdown);
          _markRoomReady();
          if (prompt) {
            _send(prompt);
          } else {
            _send("Let's create a new task.");
          }
        })
        .catch(function () {
          // Fallback — send with default room_id (rare error path).
          _markRoomReady();
          if (prompt) { _send(prompt); } else { _send("Let's create a new task."); }
        });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
