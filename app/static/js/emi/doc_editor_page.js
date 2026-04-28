(function () {
  "use strict";

  // ─── State ────────────────────────────────────────────────────────────────

  let _socket = null;
  let _socketId = "";
  let _currentMarkdown = "";
  let _sessionId = null;
  let _docName = "";
  let _docType = "md";
  let _docId = "";  // Google Doc ID (for push-back updates)

  // ─── DOM ──────────────────────────────────────────────────────────────────

  function _el(id) { return document.getElementById(id); }

  function _addMessage(text, role) {
    const container = _el("de-chat-messages");
    const ph = container.querySelector(".de-chat__placeholder");
    if (ph) ph.remove();

    const div = document.createElement("div");
    div.className = "de-msg de-msg--" + role;
    div.textContent = text;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
  }

  function _setDoc(markdown) {
    _currentMarkdown = markdown;
    const el = _el("de-doc-content");
    if (!markdown || !markdown.trim()) {
      el.innerHTML = '<div class="de-doc__placeholder">Your document will appear here as you describe it.</div>';
      return;
    }
    el.innerHTML = _renderMarkdown(markdown);
  }

  function _setDocName(name) {
    _docName = name;
    const el = _el("de-doc-name");
    if (el) el.textContent = name ? "— " + name : "";
  }

  function _setStatus(text, html) {
    const el = _el("de-doc-status");
    if (!el) return;
    if (html) {
      el.innerHTML = text;
    } else {
      el.textContent = text;
    }
  }

  // ─── Markdown renderer ────────────────────────────────────────────────────

  function _renderMarkdown(md) {
    const lines = md.split("\n");
    let html = "";
    let inPre = false;
    let preContent = "";

    for (const line of lines) {
      const trimmed = line.trim();

      // Fenced code blocks
      if (trimmed.startsWith("```")) {
        if (inPre) {
          html += "<pre><code>" + _esc(preContent) + "</code></pre>";
          preContent = "";
          inPre = false;
        } else {
          inPre = true;
        }
        continue;
      }
      if (inPre) {
        preContent += line + "\n";
        continue;
      }

      if (trimmed.startsWith("### ")) {
        html += "<h3>" + _inlineMd(trimmed.slice(4)) + "</h3>";
      } else if (trimmed.startsWith("## ")) {
        html += "<h2>" + _inlineMd(trimmed.slice(3)) + "</h2>";
      } else if (trimmed.startsWith("# ")) {
        html += "<h1>" + _inlineMd(trimmed.slice(2)) + "</h1>";
      } else if (trimmed.startsWith("> ")) {
        html += "<blockquote>" + _inlineMd(trimmed.slice(2)) + "</blockquote>";
      } else if (/^\d+\.\s/.test(trimmed)) {
        html += "<div style='margin:4px 0 4px 20px'>" + _inlineMd(trimmed) + "</div>";
      } else if (trimmed.startsWith("- ") || trimmed.startsWith("* ")) {
        html += "<div style='margin:2px 0 2px 20px'>" + _inlineMd(trimmed) + "</div>";
      } else if (trimmed === "") {
        html += "<div style='height:10px'></div>";
      } else {
        html += "<p>" + _inlineMd(trimmed) + "</p>";
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
    h = h.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    h = h.replace(/\*(.+?)\*/g, "<em>$1</em>");
    h = h.replace(/`(.+?)`/g, "<code>$1</code>");
    h = h.replace(/\[(.+?)\]\((.+?)\)/g, '<a href="$2" target="_blank" style="color:#5a9fd0">$1</a>');
    return h;
  }

  // ─── Socket ───────────────────────────────────────────────────────────────

  function _initSocket() {
    _socket = io({ transports: ["websocket", "polling"] });

    if (window.EmiSocketHeartbeat) {
      window.EmiSocketHeartbeat.install({ socket: _socket, getRoomId: () => "doc_editor" });
    }

    _socket.on("connect", function () {
      _socketId = _socket.id;
      var payload = { room_id: "doc_editor" };
      var since = window.EmiSocketHeartbeat && window.EmiSocketHeartbeat.getLastMessageTs();
      if (since) payload.since_ts = since;
      _socket.emit("register_chat_client", payload);
    });

    _socket.on("user_message_data", function (data) {
      if (!data) return;
      if (window.EmiSocketHeartbeat) window.EmiSocketHeartbeat.noteIncoming(data);

      var subTypes = data.sub_data_type || [];
      if (Array.isArray(subTypes) && (subTypes.indexOf("proactive") >= 0 || subTypes.indexOf("situation_audit") >= 0)) {
        return;
      }

      // Chat response
      if (data.chat && typeof data.chat === "string" && data.chat.trim()) {
        _addMessage(data.chat, "assistant");
      }

      // Widget data (doc updates)
      var items = data.widget_data || [];
      for (var i = 0; i < items.length; i++) {
        var item = items[i];
        if (!item || !item.data_type) continue;

        if (item.data_type === "doc_draft_update" && item.doc_markdown) {
          _setDoc(item.doc_markdown);
          _setStatus("Updated");
          if (item.session_id) _sessionId = item.session_id;
          if (item.doc_id) _docId = item.doc_id;
          if (item.doc_name) _setDocName(item.doc_name);
        }
        if (item.data_type === "doc_created") {
          var url = item.doc_url || "";
          if (url) {
            _setStatus('Saved — <a href="' + url + '" target="_blank" style="color:#5a9fd0">Open in Google Docs</a>', true);
          } else {
            _setStatus("Saved");
          }
        }
        if (item.data_type === "doc_save_failed") {
          _setStatus("Save failed: " + (item.error || "unknown"));
        }
      }
    });
  }

  // ─── Send message ─────────────────────────────────────────────────────────

  function _send(text) {
    if (!text || !text.trim()) return;
    _addMessage(text, "user");

    const formData = new FormData();
    formData.append("text", text);
    formData.append("room_id", "doc_editor");

    fetch("/process_request", {
      method: "POST",
      body: formData,
    }).catch(function (err) {
      _addMessage("Failed to send: " + err.message, "assistant");
    });
  }

  // ─── Save ─────────────────────────────────────────────────────────────────

  function _save() {
    if (!_currentMarkdown || !_currentMarkdown.trim()) return;

    // If it's a Google Doc, push back to Drive. Otherwise save locally.
    if (_docType === "gdoc" && _docId) {
      _pushToGdoc();
      return;
    }

    _setStatus("Saving...");
    fetch("/doc/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        markdown: _currentMarkdown,
        name: _docName || undefined,
        session_id: _sessionId || undefined,
      }),
    })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (data.success) {
          _setStatus("Draft saved");
          if (data.name) _setDocName(data.name);
        } else {
          _setStatus("Save failed");
        }
      })
      .catch(function () {
        _setStatus("Save failed");
      });
  }

  // ─── Push to Google Docs ──────────────────────────────────────────────────

  function _pushToGdoc() {
    if (!_currentMarkdown || !_currentMarkdown.trim()) return;
    _setStatus("Pushing to Google Docs...");

    fetch("/doc/push-gdoc", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        markdown: _currentMarkdown,
        doc_id: _docId || undefined,
        title: _docName || "Untitled Document",
      }),
    })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (data.doc_url) {
          if (data.doc_id) _docId = data.doc_id;
          _setStatus('Saved to Google Docs — <a href="' + data.doc_url + '" target="_blank" style="color:#5a9fd0">Open</a>', true);
        } else {
          _setStatus("Save failed: " + (data.error || "unknown"));
        }
      })
      .catch(function () {
        _setStatus("Save failed");
      });
  }

  // ─── New document ─────────────────────────────────────────────────────────

  function _newDoc() {
    _currentMarkdown = "";
    _docName = "";
    _sessionId = null;
    _setDoc("");
    _setDocName("");
    _setStatus("");
    var container = _el("de-chat-messages");
    container.innerHTML = '<div class="de-chat__placeholder">Tell me what document you\'d like to create or paste content to edit.</div>';
  }

  // ─── Init ─────────────────────────────────────────────────────────────────

  function init() {
    _initSocket();

    var input = _el("de-chat-input");
    var sendBtn = _el("de-chat-send");

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

    _el("de-new-btn").addEventListener("click", _newDoc);
    _el("de-save-btn").addEventListener("click", _save);

    // Check URL params
    var params = new URLSearchParams(window.location.search);
    var loadName = params.get("load");
    var loadGdoc = params.get("gdoc");
    var prompt = params.get("prompt");

    if (loadName) {
      _setStatus("Loading...");
      _setDocName(loadName);
      fetch("/doc/load?name=" + encodeURIComponent(loadName))
        .then(function (res) { return res.json(); })
        .then(function (data) {
          if (data.markdown) {
            _setDoc(data.markdown);
            _setStatus("Loaded");
          } else {
            _setStatus("Not found: " + loadName);
          }
        })
        .catch(function () { _setStatus("Failed to load"); });
      _send("I loaded the document '" + loadName + "'. Let me know what changes you'd like.");
    } else if (loadGdoc) {
      _setStatus("Loading from Google Docs...");
      _setDocName(loadGdoc);
      _docType = "gdoc";
      fetch("/doc/load-gdoc?name=" + encodeURIComponent(loadGdoc))
        .then(function (res) { return res.json(); })
        .then(function (data) {
          var docText = data.doc_markdown || data.markdown || data.body_text;
          if (docText) {
            _setDoc(docText);
            _setStatus("Loaded from Google Docs");
            if (data.title) _setDocName(data.title);
            if (data.doc_id) _docId = data.doc_id;
            if (data.session_id) _sessionId = data.session_id;
            _send("I loaded the Google Doc '" + (data.title || loadGdoc) + "'. Let me know what changes you'd like.");
          } else if (data.error) {
            _setStatus("Error: " + data.error);
          } else {
            _setStatus("Not found: " + loadGdoc);
          }
        })
        .catch(function () { _setStatus("Failed to load"); });
    } else if (prompt) {
      _send(prompt);
    } else {
      _send("Let's create a new document.");
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
