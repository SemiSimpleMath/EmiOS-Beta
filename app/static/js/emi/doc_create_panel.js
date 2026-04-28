(function (global) {
  "use strict";

  // ─── State ────────────────────────────────────────────────────────────────

  let _panelVisible = false;
  let _currentDocType = "md";   // "md" | "gdoc"
  let _currentDocId = null;     // Google Doc ID or local session filename
  let _currentDocUrl = null;    // URL for "Open" button (gdoc only)
  let _currentDocName = "";     // user-supplied name shown in header
  let _currentMarkdown = "";
  let _draftId = null;          // reused across auto-saves
  let _saveTimeout = null;      // debounce handle for auto-save
  let _currentSocketId = "";
  let _shareOnPush = false;     // whether to make the doc public on push
  let _gdocAccountId = null;    // Google account ID used to create/own this doc

  // ─── DOM helpers ──────────────────────────────────────────────────────────

  function _getPanel()    { return document.getElementById("doc-create-panel"); }
  function _getPreviewEl(){ return document.getElementById("doc-create-preview"); }
  function _getStatusEl() { return document.getElementById("doc-create-status"); }
  function _getOpenBtn()  { return document.getElementById("doc-create-open-btn"); }
  function _getSaveBtn()  { return document.getElementById("doc-create-save-btn"); }
  function _getPushBtn()  { return document.getElementById("doc-create-push-btn"); }
  function _getRefreshBtn(){ return document.getElementById("doc-create-refresh-btn"); }
  function _getTypeBadge(){ return document.getElementById("doc-create-type-badge"); }
  function _getNameEl()   { return document.getElementById("doc-create-name"); }

  // ─── Panel lifecycle ──────────────────────────────────────────────────────

  function showDocList() {
    let panel = _getPanel();
    if (!panel) {
      panel = _buildPanel();
      document.body.appendChild(panel);
    }
    if (!_panelVisible) {
      _panelVisible = true;
      panel.style.display = "flex";
      requestAnimationFrame(function () {
        panel.style.opacity = "1";
        panel.style.transform = "translateX(0)";
      });
    }
    _setStatus("Loading doc list…", true);
    _setMarkdown("");

    fetch("/doc/list")
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (data) {
        const docs = data.docs || [];
        if (!docs.length) {
          _setMarkdown("_No documents found._");
          _setStatus("No documents found.");
          return;
        }
        const lines = docs.map(function (d) {
          const name = d.name || d.doc_id;
          const id = d.doc_id;
          const type = d.doc_type ? " `" + d.doc_type + "`" : "";
          const ts = d.updated_at ? " — " + d.updated_at : "";
          return "- **" + name + "** (`" + id + "`)" + type + ts;
        });
        _setMarkdown("## Available documents\n\n" + lines.join("\n") + "\n\nType `/doc load <name>` to open one.");
        _setStatus(docs.length + " document" + (docs.length === 1 ? "" : "s") + " found. Use `/doc load <name>` to open.");
      })
      .catch(function (err) {
        _setStatus("Failed to load doc list: " + err.message);
        _setMarkdown("_Error: " + err.message + "_");
      });
  }

  function showPanel() {
    if (_panelVisible) return;
    _showPanelInternal();
  }

  // Open panel immediately on /doc create — called before server responds
  function showPanelEmpty(docType, docName) {
    _currentDocType = String(docType || "md").toLowerCase();
    _currentDocId = null;
    _currentDocUrl = null;
    _draftId = null;
    _currentDocName = String(docName || "").trim();

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

    _updateTypeBadge(_currentDocType);
    _updateDocNameDisplay(_currentDocName);
    _setStatus("Waiting for first input…");
    _setMarkdown("");
    _hideOpenBtn();
    _hidePushBtn();
    _hideRefreshBtn();
    _currentSocketId = "";
  }

  // Load a doc by name — opens panel in loading state, fetches from server
  function loadDocByName(name) {
    _currentDocId = null;
    _currentDocUrl = null;
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

    _setStatus("Loading document…", true);
    _setMarkdown("");
    _hideOpenBtn();
    _hideRefreshBtn();
    _currentSocketId = "";

    fetch("/doc/load?name=" + encodeURIComponent(name))
      .then(function (res) {
        if (!res.ok) {
          return res.json().then(function (d) {
            throw new Error((d && d.error) ? d.error : ("HTTP " + res.status));
          }).catch(function () { throw new Error("HTTP " + res.status); });
        }
        return res.json();
      })
      .then(function (data) {
        const md = (data && data.doc_markdown) ? String(data.doc_markdown) : "";
        const docId = (data && data.doc_id) ? String(data.doc_id) : "";
        const docType = (data && data.doc_type) ? String(data.doc_type) : "md";

        _currentDocType = docType;
        _currentDocId = docId || null;
        // Use the doc_id (which is the filename stem) as the display name if no explicit name
        _currentDocName = docId || name;
        _updateTypeBadge(_currentDocType);
        _updateDocNameDisplay(_currentDocName);

        if (md) {
          _setMarkdown(md);
          _setStatus("Loaded: " + (docId || name));
          setTimeout(() => { if (_panelVisible) _setStatus("Document loaded."); }, 1500);
        } else {
          _setStatus("No content found for: " + name);
        }
      })
      .catch(function (err) {
        _setStatus("Load failed: " + String(err).slice(0, 80));
      });
  }

  function loadGdocByName(name) {
    _currentDocId = null;
    _currentDocUrl = null;
    _draftId = null;

    let panel = _getPanel();
    if (!panel) {
      panel = _buildPanel();
      document.body.appendChild(panel);
    }

    if (!_panelVisible) {
      _panelVisible = true;
      panel.style.display = "flex";
      requestAnimationFrame(function () {
        panel.style.opacity = "1";
        panel.style.transform = "translateX(0)";
      });
    }

    _currentDocType = "gdoc";
    _updateTypeBadge("gdoc");
    _setStatus("Searching Google Drive…", true);
    _setMarkdown("");
    _hideOpenBtn();
    _hideRefreshBtn();
    _currentSocketId = "";

    fetch("/doc/load-gdoc?name=" + encodeURIComponent(name))
      .then(function (res) {
        if (res.status === 401) {
          return res.json().then(function (d) {
            if (d && d.needs_oauth && d.oauth_url) {
              window.open(d.oauth_url, "_blank");
            }
            throw new Error(d.error || "OAuth authentication required");
          });
        }
        if (!res.ok) {
          return res.json().then(function (d) {
            throw new Error((d && d.error) ? d.error : ("HTTP " + res.status));
          }).catch(function () { throw new Error("HTTP " + res.status); });
        }
        return res.json();
      })
      .then(function (data) {
        var md = (data && data.doc_markdown) ? String(data.doc_markdown) : "";
        var docId = (data && data.doc_id) ? String(data.doc_id) : "";
        var title = (data && data.title) ? String(data.title) : name;
        var docUrl = (data && data.doc_url) ? String(data.doc_url) : "";

        _currentDocId = docId || null;
        _currentDocUrl = docUrl || null;
        _currentDocName = title || name;
        _updateDocNameDisplay(_currentDocName);

        if (md) {
          _setMarkdown(md);
          _setStatus("Loaded: " + title);
          setTimeout(function () { if (_panelVisible) _setStatus("Google Doc loaded."); }, 1500);
        } else {
          _setStatus("Document is empty: " + title);
        }

        if (docUrl) _showOpenBtn(docUrl);
        if (docId) _showRefreshBtn();
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

    // Send /end to exit doc creation mode and return to normal chat.
    const formData = new FormData();
    formData.append("text", "/end");
    formData.append("elapsed_time", "0");
    formData.append("speaking_mode", "false");
    fetch("/process_request", { method: "POST", body: formData }).catch(() => {});
  }

  function _showPanelInternal() {
    _panelVisible = true;
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
    _updateDocNameDisplay(_currentDocName);
    _setStatus("Drafting document…");
    _setMarkdown("");
    _hideOpenBtn();
    _hidePushBtn();
    _hideRefreshBtn();
    _currentSocketId = "";
  }

  // ─── Panel DOM builder ────────────────────────────────────────────────────

  function _buildPanel() {
    const panel = document.createElement("div");
    panel.id = "doc-create-panel";
    panel.className = "doc-create-panel";
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
          <span style="color:#4ac4a0;font-size:16px">◈</span>
          <span style="color:#c8d0df;font-weight:700;font-size:14px">Document</span>
          <span id="doc-create-type-badge" style="
            background:#1a2a3a;color:#4a8fa8;border:1px solid #2a3a50;
            border-radius:4px;padding:1px 7px;font-size:10px;font-weight:700;
            letter-spacing:.05em;text-transform:uppercase;
          ">md</span>
          <span id="doc-create-name" style="
            color:#5a7890;font-size:12px;font-style:italic;
            overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:160px;
          "></span>
        </div>
        <button id="doc-create-close-btn" style="
          background:none;border:none;color:#556070;cursor:pointer;
          font-size:20px;line-height:1;padding:0 2px;
        " aria-label="Close document panel">×</button>
      </div>

      <div id="doc-create-status" style="
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

      <div id="doc-create-preview" style="
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
        <button id="doc-create-save-btn" style="
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
        <button id="doc-create-push-btn" style="
          display: none;
          background: #1a3540;
          color: #4ac4a0;
          border: 1px solid #2a5045;
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
        ">↑ Push to Google Doc</button>
        <button id="doc-create-refresh-btn" style="
          display: none;
          background: #1a2a3a;
          color: #4a9ac4;
          border: 1px solid #2a4a60;
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
        ">↻ Refresh from Google Docs</button>
        <label id="doc-create-share-toggle" style="
          display: none;
          align-items: center;
          gap: 8px;
          font-size: 12px;
          color: #7aa8b8;
          cursor: pointer;
          padding: 4px 2px;
          user-select: none;
        ">
          <input type="checkbox" id="doc-create-share-checkbox" style="accent-color:#4ac4a0; width:14px; height:14px; cursor:pointer;">
          Share link (anyone with link can view)
        </label>
        <a id="doc-create-open-btn" href="#" target="_blank" style="
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
        ">Open in Google Docs →</a>
        <div id="doc-create-share-link-row" style="
          display: none;
          align-items: center;
          gap: 6px;
          background: #111e26;
          border: 1px solid #2a4a5a;
          border-radius: 6px;
          padding: 7px 10px;
          margin-top: 2px;
        ">
          <span id="doc-create-share-link-text" style="
            flex: 1;
            font-size: 11px;
            color: #7ab8d8;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
          "></span>
          <button id="doc-create-copy-link-btn" title="Copy link" style="
            background: none;
            border: 1px solid #2a4a5a;
            border-radius: 4px;
            color: #4ac4a0;
            font-size: 11px;
            padding: 3px 8px;
            cursor: pointer;
            white-space: nowrap;
            flex-shrink: 0;
          ">Copy</button>
        </div>
      </div>
    `;

    panel.querySelector("#doc-create-close-btn").addEventListener("click", hidePanel);
    panel.querySelector("#doc-create-save-btn").addEventListener("click", _onSaveClick);
    panel.querySelector("#doc-create-push-btn").addEventListener("click", _onPushClick);
    panel.querySelector("#doc-create-refresh-btn").addEventListener("click", _onRefreshClick);
    panel.querySelector("#doc-create-share-checkbox").addEventListener("change", function() {
      _shareOnPush = this.checked;
    });
    panel.querySelector("#doc-create-copy-link-btn").addEventListener("click", function() {
      const url = document.getElementById("doc-create-share-link-text").textContent;
      if (!url) return;
      navigator.clipboard.writeText(url).then(() => {
        this.textContent = "Copied!";
        setTimeout(() => { this.textContent = "Copy"; }, 2000);
      }).catch(() => {
        this.textContent = "Copy";
      });
    });

    return panel;
  }

  // ─── Type badge ───────────────────────────────────────────────────────────

  function _updateTypeBadge(docType) {
    const badge = _getTypeBadge();
    if (!badge) return;
    badge.textContent = String(docType || "md").toUpperCase();
    if (docType === "gdoc") {
      badge.style.color = "#4ac4a0";
      badge.style.borderColor = "#2a5045";
      badge.style.background = "#1a3540";
    } else {
      badge.style.color = "#4a8fa8";
      badge.style.borderColor = "#2a3a50";
      badge.style.background = "#1a2a3a";
    }
  }

  function _updateDocNameDisplay(name) {
    const el = _getNameEl();
    if (!el) return;
    const trimmed = String(name || "").trim();
    el.textContent = trimmed ? trimmed : "";
    el.style.display = trimmed ? "inline" : "none";
  }

  // ─── Save draft logic ─────────────────────────────────────────────────────

  function _onSaveClick() {
    if (!_currentMarkdown || !_currentMarkdown.trim()) return;
    _doSave();
  }

  function _doSave(silent) {
    const md = _currentMarkdown;
    if (!md || !md.trim()) return;

    if (!silent) _setSaveBtnState("saving");

    fetch("/doc/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ markdown: md, draft_id: _draftId || undefined, doc_type: _currentDocType }),
    })
      .then((res) => {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then((data) => {
        _draftId = data.draft_id || _draftId;
        if (!silent) {
          _setSaveBtnState("saved");
          setTimeout(() => _setSaveBtnState("idle"), 2200);
        }
      })
      .catch((err) => {
        if (!silent) {
          _setSaveBtnState("error");
          _setStatus("Save failed: " + String(err).slice(0, 60));
          setTimeout(() => _setSaveBtnState("idle"), 3000);
        } else {
          _setStatus("Auto-save failed: " + String(err).slice(0, 60));
        }
      });
  }

  function _scheduleSilentSave() {
    if (_saveTimeout) clearTimeout(_saveTimeout);
    _saveTimeout = setTimeout(() => { _doSave(true); }, 2000);
  }

  function _setSaveBtnState(state) {
    const btn = _getSaveBtn();
    if (!btn) return;
    if (_currentMarkdown && _currentMarkdown.trim()) btn.style.display = "block";

    const styles = {
      idle:   { bg: "#1a2535", color: "#7a9ec0", border: "#2a3a50", text: "Save Draft",   cursor: "pointer" },
      saving: { bg: "#152030", color: "#4a7090", border: "#1e3045", text: "Saving…",       cursor: "default" },
      saved:  { bg: "#152a20", color: "#4aaa70", border: "#1e3830", text: "✓ Saved",       cursor: "default" },
      error:  { bg: "#2a1520", color: "#aa4a50", border: "#381e20", text: "Save failed",   cursor: "pointer" },
    };
    const s = styles[state] || styles.idle;
    btn.style.background = s.bg;
    btn.style.color = s.color;
    btn.style.borderColor = s.border;
    btn.textContent = s.text;
    btn.style.cursor = s.cursor;
    btn.disabled = (state === "saving");
  }

  // ─── Push to Google Doc ───────────────────────────────────────────────────

  function _onPushClick() {
    if (!_currentMarkdown || !_currentMarkdown.trim()) return;
    _setPushBtnState("pushing");
    _setStatus("Pushing to Google Docs…", true);

    fetch("/doc/push-gdoc", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        markdown: _currentMarkdown,
        doc_id: _currentDocId || undefined,
        title: _currentDocName || "Document",
        make_public: _shareOnPush,
        account_id: _gdocAccountId || undefined,
      }),
    })
      .then((res) => {
        return res.json().then((data) => ({ ok: res.ok, status: res.status, data }));
      })
      .then(({ ok, status, data }) => {
        if (status === 401 && data.needs_oauth) {
          _setPushBtnState("idle");
          _setStatus("Google auth required — opening sign-in…");
          window.open(data.oauth_url, "_blank", "width=600,height=700,noopener");
          return;
        }
        if (!ok) throw new Error(data.error || "HTTP " + status);
        _currentDocId = data.doc_id || _currentDocId;
        _currentDocUrl = data.doc_url || null;
        if (data.account_id) _gdocAccountId = data.account_id;
        _setPushBtnState("done");
        const accountNote = data.account_email ? ` (${data.account_email})` : "";
        const shareNote = data.is_public ? " — shared link active" : "";
        _setStatus("Pushed to Google Docs" + accountNote + shareNote + ".");
        if (_currentDocUrl) _showOpenBtn(_currentDocUrl);
        setTimeout(() => _setPushBtnState("idle"), 3000);
      })
      .catch((err) => {
        _setPushBtnState("error");
        _setStatus("Push failed: " + String(err).slice(0, 70));
        setTimeout(() => _setPushBtnState("idle"), 3000);
      });
  }

  function _setPushBtnState(state) {
    const btn = _getPushBtn();
    if (!btn) return;
    const styles = {
      idle:    { bg: "#1a3540", color: "#4ac4a0", border: "#2a5045", text: "↑ Push to Google Doc", cursor: "pointer",  disabled: false },
      pushing: { bg: "#152a30", color: "#3a9070", border: "#1e4035", text: "Pushing…",              cursor: "default",  disabled: true  },
      done:    { bg: "#152a30", color: "#3aaa80", border: "#1e4035", text: "✓ Pushed",               cursor: "default",  disabled: false },
      error:   { bg: "#2a1520", color: "#aa4a50", border: "#381e20", text: "Push failed",            cursor: "pointer",  disabled: false },
    };
    const s = styles[state] || styles.idle;
    btn.style.background = s.bg;
    btn.style.color = s.color;
    btn.style.borderColor = s.border;
    btn.textContent = s.text;
    btn.style.cursor = s.cursor;
    btn.disabled = s.disabled;
  }

  function _hidePushBtn() {
    const btn = _getPushBtn();
    if (btn) btn.style.display = "none";
  }

  function _showRefreshBtn() {
    const btn = _getRefreshBtn();
    if (btn) btn.style.display = "block";
  }

  function _hideRefreshBtn() {
    const btn = _getRefreshBtn();
    if (btn) btn.style.display = "none";
  }

  function _setRefreshBtnState(state) {
    const btn = _getRefreshBtn();
    if (!btn) return;
    const styles = {
      idle:       { bg: "#1a2a3a", color: "#4a9ac4", border: "#2a4a60", text: "↻ Refresh from Google Docs", cursor: "pointer",  disabled: false },
      refreshing: { bg: "#152230", color: "#3a7a9a", border: "#1e3a50", text: "Refreshing…",               cursor: "default",  disabled: true  },
      done:       { bg: "#152230", color: "#3aaa80", border: "#1e4035", text: "✓ Refreshed",               cursor: "default",  disabled: false },
      error:      { bg: "#2a1515", color: "#d07070", border: "#4a2020", text: "Refresh failed",            cursor: "pointer",  disabled: false },
    };
    const s = styles[state] || styles.idle;
    btn.style.background = s.bg;
    btn.style.color = s.color;
    btn.style.borderColor = s.border;
    btn.textContent = s.text;
    btn.style.cursor = s.cursor;
    btn.disabled = s.disabled;
  }

  function _onRefreshClick() {
    if (!_currentDocId || _currentDocType !== "gdoc") return;
    _setRefreshBtnState("refreshing");
    _setStatus("Refreshing from Google Docs…", true);

    fetch("/doc/load-gdoc?id=" + encodeURIComponent(_currentDocId))
      .then(function (res) {
        if (res.status === 401) {
          return res.json().then(function (d) {
            if (d && d.needs_oauth && d.oauth_url) {
              window.open(d.oauth_url, "_blank");
            }
            throw new Error(d.error || "OAuth authentication required");
          });
        }
        if (!res.ok) {
          return res.json().then(function (d) {
            throw new Error((d && d.error) ? d.error : ("HTTP " + res.status));
          }).catch(function () { throw new Error("HTTP " + res.status); });
        }
        return res.json();
      })
      .then(function (data) {
        const md = (data && data.doc_markdown) ? String(data.doc_markdown) : "";
        if (md) {
          _setMarkdown(md);
        }
        _setRefreshBtnState("done");
        _setStatus("Refreshed: " + (_currentDocName || _currentDocId));
        setTimeout(function () { _setRefreshBtnState("idle"); }, 2000);
      })
      .catch(function (err) {
        _setRefreshBtnState("error");
        _setStatus("Refresh failed: " + String(err).slice(0, 80));
        setTimeout(function () { _setRefreshBtnState("idle"); }, 3000);
      });
  }

  // ─── Content updates ──────────────────────────────────────────────────────

  function _setMarkdown(md) {
    _currentMarkdown = md || "";
    const el = _getPreviewEl();
    if (!el) return;
    if (!md || !md.trim()) {
      el.innerHTML = '<span style="color:#3a4a5a;font-style:italic">Waiting for first input…</span>';
      const saveBtn = _getSaveBtn();
      if (saveBtn) saveBtn.style.display = "none";
      return;
    }
    el.textContent = md;
    _applyMarkdownStyles(el);
    _setSaveBtnState("idle");
    // Show push button for gdoc type or always if there's content
    const pushBtn = _getPushBtn();
    if (pushBtn) pushBtn.style.display = "block";
    const shareToggle = document.getElementById("doc-create-share-toggle");
    if (shareToggle) shareToggle.style.display = "flex";
  }

  function _applyMarkdownStyles(el) {
    let html = el.textContent
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/^# (.+)$/gm, '<span style="color:#c8d0df;font-size:15px;font-weight:700;display:block;margin:12px 0 6px">$1</span>')
      .replace(/^## (.+)$/gm, '<span style="color:#a8b8cc;font-size:13px;font-weight:700;display:block;margin:10px 0 4px;letter-spacing:.03em">$1</span>')
      .replace(/^### (.+)$/gm, '<span style="color:#8899aa;font-size:12px;font-weight:700;display:block;margin:8px 0 3px">$1</span>')
      .replace(/\*\*(.+?)\*\*/g, '<strong style="color:#b8c8d8">$1</strong>')
      .replace(/^(\d+\. .+)$/gm, '<span style="display:block;padding-left:4px">$1</span>')
      .replace(/^(- .+)$/gm, '<span style="display:block;padding-left:8px;color:#7a8a9a">$1</span>')
      .replace(/`([^`]+)`/g, '<code style="background:#ffffff0f;border-radius:3px;padding:1px 5px;font-family:monospace;font-size:11px;color:#9b6ecf">$1</code>')
      .replace(/\n/g, "<br>");
    el.innerHTML = html;
  }

  function _setStatus(text, isSpinner) {
    const el = _getStatusEl();
    if (!el) return;
    const spinner = isSpinner ? '<span class="doc-create-spinner"></span>' : "";
    el.innerHTML = spinner + text;
  }

  function _showOpenBtn(url) {
    const btn = _getOpenBtn();
    if (!btn) return;
    btn.href = url;
    btn.style.display = "block";

    const linkRow = document.getElementById("doc-create-share-link-row");
    const linkText = document.getElementById("doc-create-share-link-text");
    if (linkRow && linkText) {
      linkText.textContent = url;
      linkRow.style.display = "flex";
    }
  }

  function _hideOpenBtn() {
    const btn = _getOpenBtn();
    if (btn) btn.style.display = "none";
    const linkRow = document.getElementById("doc-create-share-link-row");
    if (linkRow) linkRow.style.display = "none";
  }

  // ─── Socket event handlers ────────────────────────────────────────────────

  function handleDocDraftUpdate(item) {
    const md = (item && item.doc_markdown) ? String(item.doc_markdown) : "";
    const docType = (item && item.doc_type) ? String(item.doc_type) : _currentDocType;
    const docId = (item && item.doc_id) ? String(item.doc_id) : null;
    const docName = (item && item.doc_name) ? String(item.doc_name) : _currentDocName;
    if (item && item.socket_id) _currentSocketId = String(item.socket_id);

    _currentDocType = docType;
    if (docId) _currentDocId = docId;
    if (docName) _currentDocName = docName;

    if (!_panelVisible) _showPanelInternal();
    _updateTypeBadge(_currentDocType);
    _updateDocNameDisplay(_currentDocName);
    _setStatus("Updating document…");
    _setMarkdown(md);
    setTimeout(() => { if (_panelVisible) _setStatus("Drafting document…"); }, 800);

    // Auto-save to drafts
    _scheduleSilentSave();
  }

  function handleDocCreated(item) {
    const docId = (item && item.doc_id) ? String(item.doc_id) : null;
    const docUrl = (item && item.doc_url) ? String(item.doc_url) : null;
    const docType = (item && item.doc_type) ? String(item.doc_type) : _currentDocType;
    const accountId = (item && item.account_id) ? String(item.account_id) : null;

    _currentDocId = docId;
    _currentDocUrl = docUrl;
    _currentDocType = docType;
    if (accountId) _gdocAccountId = accountId;
    _updateTypeBadge(_currentDocType);

    _setStatus("Document saved.");
    if (docUrl) {
      _showOpenBtn(docUrl);
    }
  }

  function handleDocSaveFailed(item) {
    const error = (item && item.error) ? String(item.error) : "Unknown error";
    _setStatus("Save failed: " + error.slice(0, 80));
  }

  // ─── Spinner + button CSS ─────────────────────────────────────────────────

  function _injectStyles() {
    if (document.getElementById("doc-create-panel-styles")) return;
    const style = document.createElement("style");
    style.id = "doc-create-panel-styles";
    style.textContent = `
      @keyframes doc-spin {
        to { transform: rotate(360deg); }
      }
      .doc-create-spinner {
        display: inline-block;
        width: 10px;
        height: 10px;
        border: 2px solid #2a3a4a;
        border-top-color: #4ac4a0;
        border-radius: 50%;
        animation: doc-spin 0.7s linear infinite;
        margin-right: 7px;
        vertical-align: middle;
      }
      .doc-create-panel::-webkit-scrollbar { width: 4px; }
      .doc-create-panel::-webkit-scrollbar-track { background: #13161f; }
      .doc-create-panel::-webkit-scrollbar-thumb { background: #2a3040; border-radius: 2px; }
      #doc-create-open-btn:hover { background: #3a7abf !important; }
      #doc-create-save-btn:hover:not(:disabled) { background: #1e2e42 !important; color: #9ab8d8 !important; border-color: #3a4e68 !important; }
      #doc-create-push-btn:hover:not(:disabled) { background: #1e4a3a !important; color: #6adfc0 !important; border-color: #3a6050 !important; }
      #doc-create-preview::-webkit-scrollbar { width: 4px; }
      #doc-create-preview::-webkit-scrollbar-track { background: #13161f; }
      #doc-create-preview::-webkit-scrollbar-thumb { background: #2a3040; border-radius: 2px; }
    `;
    document.head.appendChild(style);
  }

  // ─── Widget dispatch ──────────────────────────────────────────────────────

  function dispatchWidget(items) {
    if (!Array.isArray(items)) return;
    for (const item of items) {
      const type = (item && item.data_type) ? item.data_type : "";
      switch (type) {
        case "doc_draft_update":
          handleDocDraftUpdate(item);
          break;
        case "doc_created":
          handleDocCreated(item);
          break;
        case "doc_save_failed":
          handleDocSaveFailed(item);
          break;
        case "doc_open_empty":
          // Redirect to dedicated doc editor page.
          var createUrl = "/doc/editor";
          if (item.doc_type === "gdoc" && item.doc_name) {
            createUrl += "?gdoc=" + encodeURIComponent(item.doc_name);
          } else if (item.doc_name) {
            createUrl += "?load=" + encodeURIComponent(item.doc_name);
          }
          window.open(createUrl, "_blank");
          break;
        case "doc_load_request":
          // Redirect to dedicated doc editor page.
          var loadUrl = "/doc/editor";
          if (item.doc_type === "gdoc" && item.doc_name) {
            loadUrl += "?gdoc=" + encodeURIComponent(item.doc_name);
          } else if (item.doc_name) {
            loadUrl += "?load=" + encodeURIComponent(item.doc_name);
          }
          window.open(loadUrl, "_blank");
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

  global.EmiDocPanel = {
    init,
    showPanel,
    showPanelEmpty,
    showDocList,
    loadDocByName,
    loadGdocByName,
    hidePanel,
    dispatchWidget,
    handleDocDraftUpdate,
    handleDocCreated,
    handleDocSaveFailed,
  };
})(window);
