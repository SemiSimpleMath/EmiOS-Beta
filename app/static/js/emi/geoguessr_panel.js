/**
 * EmiGeoPanel — GeoGuessr game sidebar panel
 *
 * Socket events handled:
 *   geo_update  — new screenshot analyzed, panel refreshes clues + confidence
 *   geo_reveal  — answer revealed, panel shows location with fanfare
 *
 * Commands supported via chat:
 *   /play geoguessr   → open panel (immediate)
 *   /play stop        → end game
 *   /play pause       → pause screenshot timer
 *   /play resume      → resume screenshot timer
 *   /play next        → start next round (clears current state)
 *   hint              → ask Emi for a clue (handled by host agent)
 *   tell me / reveal  → trigger answer reveal
 */
(function (global) {
  "use strict";

  // ── State ─────────────────────────────────────────────────────────────────
  let _visible = false;
  let _paused = false;
  let _screenshots = [];     // { url, clue, confidence, timestamp }
  let _currentScreenIdx = 0;
  let _clueLog = [];
  let _confidence = 0;
  let _revealed = false;
  let _bestGuess = "";
  let _roundNumber = 1;

  // ── DOM refs ──────────────────────────────────────────────────────────────
  let _panel, _badge, _confidenceBar, _confidenceLabel,
      _clueList, _screenshotImg, _screenshotCounter,
      _revealSection, _revealAnswer, _hintBtn, _revealBtn,
      _pauseBtn, _nextRoundBtn,
      _prevBtn, _nextBtn, _statusDot, _statusText, _roundLabel, _cluesHint;

  // ── Init ──────────────────────────────────────────────────────────────────
  function init() {
    _injectStyles();
    _buildPanel();
  }

  // ── Public API ────────────────────────────────────────────────────────────
  function showPanel() {
    _ensureBuilt();
    _resetState();
    _panel.style.display = "flex";
    requestAnimationFrame(() => {
      _panel.classList.add("geo-panel--visible");
    });
    _visible = true;
    _paused = true;  // timer starts paused server-side
    _setStatus("paused", "Ready — click Start to begin!");
    if (_badge) { _badge.textContent = "READY"; _badge.style.background = "#64748b"; _badge.style.animation = "none"; }
    _render();
  }

  function hidePanel() {
    if (!_panel) return;
    _panel.classList.remove("geo-panel--visible");
    setTimeout(() => {
      if (_panel) _panel.style.display = "none";
    }, 300);
    _visible = false;
  }

  function handleGeoUpdate(item) {
    // item: { visible_clue, commentary, confidence, screenshot_count, latest_screenshot }
    if (!_visible) showPanel();
    if (_paused) return; // drop updates while paused

    const clue = item.visible_clue || "";
    const commentary = item.commentary || "";
    const conf = parseInt(item.confidence || 0, 10);
    const imgUrl = item.latest_screenshot || "";

    if (clue) _clueLog.push(clue);
    _confidence = conf;

    if (imgUrl) {
      _screenshots.push({
        url: imgUrl,
        clue: clue,
        confidence: conf,
        timestamp: new Date().toLocaleTimeString(),
      });
      _currentScreenIdx = _screenshots.length - 1;
    }

    _setStatus("analyzing", commentary || "Analyzing clues...");
    _render();
  }

  function handleGeoReveal(item) {
    // item: { best_guess, confidence }
    _revealed = true;
    _paused = true;
    _bestGuess = item.best_guess || "";
    _confidence = parseInt(item.confidence || 0, 10);
    if (_badge) { _badge.textContent = "REVEALED"; _badge.style.background = "#22c55e"; _badge.style.animation = "none"; }
    _setStatus("revealed", "Answer revealed!");
    _render();
  }

  // Called by request_sender on /play pause
  function pausePanel() {
    if (!_paused) {
      _paused = true;
      _setStatus("paused", "Paused — screenshots suspended");
      if (_badge) { _badge.textContent = "PAUSED"; _badge.style.background = "#64748b"; _badge.style.animation = "none"; }
      _render();
    }
  }

  // Called by request_sender on /play resume
  function resumePanel() {
    if (_paused) {
      _paused = false;
      _setStatus("watching", "Resumed — watching your screen...");
      if (_badge) { _badge.textContent = "LIVE"; _badge.style.background = "#ef4444"; _badge.style.animation = ""; }
      _render();
    }
  }

  // Called by request_sender on /play next — wipe the slate for a new round
  function newRound() {
    _roundNumber++;
    _resetState(/* keepRound */ true);
    _paused = false;
    if (_badge) { _badge.textContent = "LIVE"; _badge.style.background = "#ef4444"; _badge.style.animation = ""; }
    _setStatus("watching", `Round ${_roundNumber} — watching your screen...`);
    // Show placeholder again
    const ph = _panel && _panel.querySelector("#geo-shot-placeholder");
    if (ph) ph.style.display = "flex";
    if (_screenshotImg) _screenshotImg.style.display = "none";
    _render();
  }

  // ── State management ──────────────────────────────────────────────────────
  function _resetState(keepRound) {
    _screenshots = [];
    _currentScreenIdx = 0;
    _clueLog = [];
    _confidence = 0;
    _revealed = false;
    _bestGuess = "";
    if (!keepRound) _roundNumber = 1;
  }

  // ── Render ────────────────────────────────────────────────────────────────
  function _render() {
    if (!_panel) return;

    // Round label
    if (_roundLabel) _roundLabel.textContent = _roundNumber > 1 ? `Round ${_roundNumber}` : "";

    // Confidence bar
    const pct = Math.max(0, Math.min(100, _confidence));
    _confidenceBar.style.width = pct + "%";
    _confidenceBar.style.background = _confidenceColor(pct);
    _confidenceLabel.textContent = pct + "%";

    // Screenshot
    if (_screenshots.length > 0) {
      const shot = _screenshots[_currentScreenIdx];
      _screenshotImg.src = shot.url;
      _screenshotImg.style.display = "block";
      const ph = _panel.querySelector("#geo-shot-placeholder");
      if (ph) ph.style.display = "none";
      _screenshotCounter.textContent =
        "Screenshot " + (_currentScreenIdx + 1) + " / " + _screenshots.length;
    } else {
      _screenshotImg.style.display = "none";
      _screenshotCounter.textContent = _paused ? "Paused" : "Waiting for first screenshot...";
    }

    // Nav buttons
    _prevBtn.disabled = _currentScreenIdx <= 0;
    _nextBtn.disabled = _currentScreenIdx >= _screenshots.length - 1;

    // Clue log
    _clueList.innerHTML = "";
    const clues = _revealed ? _clueLog : _clueLog.slice(-8);
    clues.forEach((clue, i) => {
      const li = document.createElement("li");
      li.className = "geo-clue-item";
      const num = _revealed ? i + 1 : _clueLog.length - clues.length + i + 1;
      const textClass = _revealed ? "geo-clue-text" : "geo-clue-text geo-clue-text--hidden";
      li.innerHTML = `<span class="geo-clue-num">#${num}</span><span class="${textClass}">${_esc(clue)}</span>`;
      _clueList.appendChild(li);
    });
    if (!_clueLog.length) {
      const li = document.createElement("li");
      li.className = "geo-clue-empty";
      li.textContent = _paused
        ? "Game paused. Click Resume to continue..."
        : "Clues will appear here as Emi analyzes screenshots...";
      _clueList.appendChild(li);
    }

    // Pause button label
    if (_pauseBtn) {
      const label = _paused ? (_screenshots.length === 0 ? "▶ Start" : "▶ Resume") : "⏸ Pause";
      const title = _paused ? "Start the screenshot timer" : "Pause screenshot timer";
      _pauseBtn.textContent = label;
      _pauseBtn.title = title;
      _pauseBtn.classList.toggle("geo-btn--paused-active", _paused);
    }

    // Next round button — show after reveal, or always when a round has data
    if (_nextRoundBtn) {
      _nextRoundBtn.style.display = (_screenshots.length > 0 || _revealed) ? "inline-flex" : "none";
    }

    // Reveal section
    if (_revealed) {
      _revealSection.style.display = "block";
      _revealAnswer.textContent = _bestGuess || "Unknown";
      _hintBtn.style.display = "none";
      _revealBtn.style.display = "none";
      if (_cluesHint) _cluesHint.style.display = "none";
      _revealSection.classList.add("geo-reveal--flash");
      setTimeout(() => _revealSection.classList.remove("geo-reveal--flash"), 1200);
    } else {
      _revealSection.style.display = "none";
      _hintBtn.style.display = "inline-flex";
      _revealBtn.style.display = "inline-flex";
      if (_cluesHint) _cluesHint.style.display = "";
    }
  }

  function _confidenceColor(pct) {
    if (pct < 30) return "#ef4444";
    if (pct < 60) return "#f97316";
    if (pct < 80) return "#eab308";
    return "#22c55e";
  }

  // ── Status indicator ──────────────────────────────────────────────────────
  function _setStatus(state, text) {
    if (!_statusDot) return;
    _statusDot.className = "geo-status-dot geo-status-dot--" + state;
    _statusText.textContent = text;
  }

  // ── DOM construction ──────────────────────────────────────────────────────
  function _ensureBuilt() {
    if (!_panel) _buildPanel();
  }

  function _buildPanel() {
    // Panel
    _panel = document.createElement("div");
    _panel.className = "geo-panel";
    _panel.style.display = "none";

    _panel.innerHTML = `
      <div class="geo-panel__header">
        <div class="geo-panel__title">
          <span class="geo-panel__icon">🌍</span>
          <span>GeoGuessr</span>
          <span class="geo-badge" id="geo-badge">LIVE</span>
          <span class="geo-round-label" id="geo-round-label"></span>
        </div>
        <button class="geo-panel__close" id="geo-close" title="End game">✕</button>
      </div>

      <div class="geo-status-bar">
        <span class="geo-status-dot geo-status-dot--watching" id="geo-status-dot"></span>
        <span class="geo-status-text" id="geo-status-text">Starting up...</span>
      </div>

      <div class="geo-confidence-section">
        <div class="geo-confidence-label-row">
          <span>Emi's confidence</span>
          <span class="geo-confidence-pct" id="geo-conf-label">0%</span>
        </div>
        <div class="geo-confidence-track">
          <div class="geo-confidence-fill" id="geo-conf-bar" style="width:0%"></div>
        </div>
      </div>

      <div class="geo-screenshot-section">
        <div class="geo-screenshot-nav">
          <button class="geo-nav-btn" id="geo-prev" title="Previous screenshot">‹</button>
          <span class="geo-screenshot-counter" id="geo-shot-counter">Waiting for first screenshot...</span>
          <button class="geo-nav-btn" id="geo-next" title="Next screenshot">›</button>
        </div>
        <div class="geo-screenshot-frame">
          <img class="geo-screenshot-img" id="geo-shot-img" src="" alt="Screenshot" style="display:none" />
          <div class="geo-screenshot-placeholder" id="geo-shot-placeholder">
            <div class="geo-pulse-ring"></div>
            <span>Screenshot every 15s</span>
          </div>
        </div>
      </div>

      <div class="geo-clues-section">
        <div class="geo-section-title">🔍 Clues discovered <span class="geo-clues-hint" id="geo-clues-hint">— select to peek</span></div>
        <ul class="geo-clue-list" id="geo-clue-list"></ul>
      </div>

      <div class="geo-reveal-section" id="geo-reveal-section" style="display:none">
        <div class="geo-section-title">📍 Emi's answer</div>
        <div class="geo-reveal-answer" id="geo-reveal-answer"></div>
      </div>

      <div class="geo-actions">
        <button class="geo-btn geo-btn--hint" id="geo-hint-btn">
          💡 Hint
        </button>
        <button class="geo-btn geo-btn--reveal" id="geo-reveal-btn">
          🗺️ Reveal
        </button>
        <button class="geo-btn geo-btn--pause" id="geo-pause-btn" title="Pause screenshot timer">
          ⏸ Pause
        </button>
      </div>

      <div class="geo-actions geo-actions--secondary">
        <button class="geo-btn geo-btn--next-round" id="geo-next-round-btn" style="display:none">
          🔄 Next Round
        </button>
      </div>

      <div class="geo-footer">
        Type <kbd>hint</kbd> · <kbd>tell me</kbd> · <kbd>/play pause</kbd> · <kbd>/play next</kbd><br>
        Start with <kbd>/play geoguessr 2</kbd> to use monitor 2
      </div>
    `;

    document.body.appendChild(_panel);

    // Cache refs
    _badge            = _panel.querySelector("#geo-badge");
    _roundLabel       = _panel.querySelector("#geo-round-label");
    _statusDot        = _panel.querySelector("#geo-status-dot");
    _statusText       = _panel.querySelector("#geo-status-text");
    _confidenceBar    = _panel.querySelector("#geo-conf-bar");
    _confidenceLabel  = _panel.querySelector("#geo-conf-label");
    _screenshotImg    = _panel.querySelector("#geo-shot-img");
    _screenshotCounter = _panel.querySelector("#geo-shot-counter");
    _clueList         = _panel.querySelector("#geo-clue-list");
    _revealSection    = _panel.querySelector("#geo-reveal-section");
    _revealAnswer     = _panel.querySelector("#geo-reveal-answer");
    _hintBtn          = _panel.querySelector("#geo-hint-btn");
    _revealBtn        = _panel.querySelector("#geo-reveal-btn");
    _pauseBtn         = _panel.querySelector("#geo-pause-btn");
    _nextRoundBtn     = _panel.querySelector("#geo-next-round-btn");
    _prevBtn          = _panel.querySelector("#geo-prev");
    _nextBtn          = _panel.querySelector("#geo-next");
    _cluesHint        = _panel.querySelector("#geo-clues-hint");

    // Screenshot placeholder hide on first image
    _screenshotImg.addEventListener("load", () => {
      const ph = _panel.querySelector("#geo-shot-placeholder");
      if (ph) ph.style.display = "none";
      _screenshotImg.style.display = "block";
    });

    // Screenshot navigation
    _prevBtn.addEventListener("click", () => {
      if (_currentScreenIdx > 0) { _currentScreenIdx--; _render(); }
    });
    _nextBtn.addEventListener("click", () => {
      if (_currentScreenIdx < _screenshots.length - 1) { _currentScreenIdx++; _render(); }
    });

    // Hint
    _hintBtn.addEventListener("click", () => _sendChatMessage("hint"));

    // Reveal
    _revealBtn.addEventListener("click", () => _sendChatMessage("tell me the answer"));

    // Pause / Resume toggle
    _pauseBtn.addEventListener("click", () => {
      if (_paused) {
        _sendChatMessage("/play resume");
        resumePanel();
      } else {
        _sendChatMessage("/play pause");
        pausePanel();
      }
    });

    // Next Round
    _nextRoundBtn.addEventListener("click", () => {
      _sendChatMessage("/play next");
      newRound();
    });

    // Close
    _panel.querySelector("#geo-close").addEventListener("click", () => {
      _sendChatMessage("/play stop");
      hidePanel();
    });
  }

  function _sendChatMessage(text) {
    // Prefer the registered send function if available
    if (window._emiSendMessage && typeof window._emiSendMessage === "function") {
      window._emiSendMessage(text);
      return;
    }
    const input = document.querySelector("#chat-input");
    if (!input) return;
    input.value = text;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    // Simulate Enter keydown — composer.js listens for this
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
  }

  function _esc(str) {
    return String(str || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // ── Styles ────────────────────────────────────────────────────────────────
  function _injectStyles() {
    if (document.getElementById("geo-panel-styles")) return;
    const style = document.createElement("style");
    style.id = "geo-panel-styles";
    style.textContent = `
      .geo-panel {
        position: fixed; top: 0; right: 0; bottom: 0;
        width: calc(100vw / 3); max-width: 95vw;
        background: #0f1117;
        border-left: 1px solid #2a2d3a;
        z-index: 1201;
        display: none; flex-direction: column;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        font-size: 13px;
        color: #e2e8f0;
        transform: translateX(100%);
        transition: transform 0.3s cubic-bezier(0.4,0,0.2,1);
        overflow: hidden;
      }
      .geo-panel--visible { transform: translateX(0); }

      .geo-panel__header {
        display: flex; align-items: center; justify-content: space-between;
        padding: 14px 16px 12px;
        border-bottom: 1px solid #2a2d3a;
        background: #13151f;
        flex-shrink: 0;
      }
      .geo-panel__title {
        display: flex; align-items: center; gap: 8px;
        font-size: 15px; font-weight: 600; color: #f8fafc;
      }
      .geo-panel__icon { font-size: 18px; }
      .geo-badge {
        font-size: 9px; font-weight: 700; letter-spacing: .08em;
        padding: 2px 6px; border-radius: 3px;
        background: #ef4444; color: #fff;
        animation: geo-blink 1.5s infinite;
      }
      @keyframes geo-blink { 0%,100%{opacity:1} 50%{opacity:.45} }
      .geo-round-label {
        font-size: 10px; font-weight: 600; color: #64748b;
        letter-spacing: .04em;
      }

      .geo-panel__close {
        background: none; border: none; cursor: pointer;
        color: #64748b; font-size: 16px; padding: 4px 6px;
        border-radius: 4px; transition: color .15s, background .15s;
      }
      .geo-panel__close:hover { color: #f8fafc; background: #1e2130; }

      .geo-status-bar {
        display: flex; align-items: center; gap: 8px;
        padding: 8px 16px;
        background: #13151f;
        border-bottom: 1px solid #1e2130;
        flex-shrink: 0;
      }
      .geo-status-dot {
        width: 8px; height: 8px; border-radius: 50%;
        flex-shrink: 0;
      }
      .geo-status-dot--watching  { background: #3b82f6; animation: geo-pulse 2s infinite; }
      .geo-status-dot--analyzing { background: #f59e0b; animation: geo-pulse 1s infinite; }
      .geo-status-dot--revealed  { background: #22c55e; }
      .geo-status-dot--paused    { background: #64748b; }
      @keyframes geo-pulse {
        0%,100%{box-shadow:0 0 0 0 currentColor} 50%{box-shadow:0 0 0 4px transparent}
      }
      .geo-status-text { font-size: 12px; color: #94a3b8; }

      .geo-confidence-section {
        padding: 12px 16px 10px;
        border-bottom: 1px solid #1e2130;
        flex-shrink: 0;
      }
      .geo-confidence-label-row {
        display: flex; justify-content: space-between;
        font-size: 11px; color: #94a3b8; margin-bottom: 6px;
        text-transform: uppercase; letter-spacing: .05em;
      }
      .geo-confidence-pct { font-weight: 700; color: #f8fafc; }
      .geo-confidence-track {
        height: 6px; background: #1e2130; border-radius: 3px; overflow: hidden;
      }
      .geo-confidence-fill {
        height: 100%; border-radius: 3px;
        transition: width 0.6s ease, background 0.6s ease;
      }

      .geo-screenshot-section {
        flex-shrink: 0;
        border-bottom: 1px solid #1e2130;
      }
      .geo-screenshot-nav {
        display: flex; align-items: center; justify-content: space-between;
        padding: 6px 10px;
        background: #13151f;
      }
      .geo-screenshot-counter { font-size: 11px; color: #64748b; }
      .geo-nav-btn {
        background: none; border: none; cursor: pointer;
        color: #64748b; font-size: 18px; padding: 2px 8px;
        border-radius: 4px; transition: color .15s, background .15s;
        line-height: 1;
      }
      .geo-nav-btn:hover:not(:disabled) { color: #f8fafc; background: #1e2130; }
      .geo-nav-btn:disabled { opacity: .3; cursor: default; }

      .geo-screenshot-frame {
        position: relative; width: 100%; aspect-ratio: 16/9;
        background: #0a0c14; overflow: hidden;
      }
      .geo-screenshot-img {
        width: 100%; height: 100%; object-fit: cover;
        display: none;
      }
      .geo-screenshot-placeholder {
        position: absolute; inset: 0;
        display: flex; flex-direction: column;
        align-items: center; justify-content: center;
        gap: 12px; color: #3b4558; font-size: 12px;
      }
      .geo-pulse-ring {
        width: 36px; height: 36px; border-radius: 50%;
        border: 2px solid #3b82f6;
        animation: geo-ring 1.8s ease-out infinite;
      }
      @keyframes geo-ring {
        0%   { transform: scale(.7); opacity: 1; }
        100% { transform: scale(1.5); opacity: 0; }
      }

      .geo-clues-section {
        flex: 1; overflow-y: auto; padding: 12px 16px;
        border-bottom: 1px solid #1e2130;
      }
      .geo-section-title {
        font-size: 11px; font-weight: 600; letter-spacing: .05em;
        text-transform: uppercase; color: #64748b; margin-bottom: 8px;
      }
      .geo-clues-hint {
        font-weight: 400; font-style: italic; letter-spacing: 0;
        text-transform: none; color: #3b4558;
      }
      .geo-clue-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
      .geo-clue-item {
        display: flex; gap: 8px; align-items: flex-start;
        background: #13151f; border-radius: 6px; padding: 7px 10px;
        border-left: 2px solid #3b82f6;
        font-size: 12px; line-height: 1.5;
      }
      .geo-clue-num {
        font-size: 10px; font-weight: 700; color: #3b82f6;
        min-width: 24px; flex-shrink: 0;
      }
      .geo-clue-text { color: #cbd5e1; }
      .geo-clue-text--hidden {
        color: transparent;
        background: #2a3040;
        border-radius: 3px;
        cursor: text;
        user-select: text;
        filter: blur(0);
        transition: color 0.2s, background 0.2s, filter 0.2s;
      }
      .geo-clue-text--hidden::selection {
        color: #cbd5e1;
        background: #3b82f6;
      }
      .geo-clue-text--hidden:hover {
        background: #2e3650;
      }
      .geo-clue-empty { color: #3b4558; font-size: 12px; font-style: italic; padding: 8px 0; }

      .geo-reveal-section {
        padding: 14px 16px;
        border-bottom: 1px solid #1e2130;
        background: #0d1a0d;
        flex-shrink: 0;
      }
      .geo-reveal-section .geo-section-title { color: #86efac; }
      .geo-reveal-answer {
        font-size: 18px; font-weight: 700; color: #4ade80;
        padding: 10px 0 4px;
      }
      .geo-reveal--flash {
        animation: geo-flash 0.4s ease 2;
      }
      @keyframes geo-flash {
        0%,100%{background:#0d1a0d} 50%{background:#14532d}
      }

      .geo-actions {
        display: flex; gap: 8px;
        padding: 10px 16px 0;
        flex-shrink: 0;
      }
      .geo-actions--secondary {
        padding: 6px 16px 10px;
      }
      .geo-btn {
        flex: 1; padding: 9px 10px;
        border: none; border-radius: 7px; cursor: pointer;
        font-size: 12px; font-weight: 600;
        display: inline-flex; align-items: center; justify-content: center; gap: 5px;
        transition: opacity .15s, transform .1s;
      }
      .geo-btn:hover { opacity: .88; }
      .geo-btn:active { transform: scale(.97); }
      .geo-btn--hint        { background: #1e40af; color: #dbeafe; }
      .geo-btn--reveal      { background: #065f46; color: #d1fae5; }
      .geo-btn--pause       { background: #1e2130; color: #94a3b8; border: 1px solid #2a2d3a; }
      .geo-btn--pause.geo-btn--paused-active { background: #1c2e1c; color: #86efac; border-color: #166534; }
      .geo-btn--next-round  {
        flex: 1; background: #312e81; color: #c7d2fe;
        border: 1px solid #3730a3;
      }
      .geo-btn--next-round:hover { background: #3730a3; }

      .geo-footer {
        padding: 7px 16px 11px;
        font-size: 11px; color: #374151; text-align: center;
        flex-shrink: 0;
      }
      .geo-footer kbd {
        background: #1e2130; color: #94a3b8;
        padding: 1px 5px; border-radius: 3px; font-size: 10px;
        border: 1px solid #2a2d3a;
      }
    `;
    document.head.appendChild(style);
  }

  // ── Expose ────────────────────────────────────────────────────────────────
  global.EmiGeoPanel = {
    init,
    showPanel,
    hidePanel,
    handleGeoUpdate,
    handleGeoReveal,
    pausePanel,
    resumePanel,
    newRound,
  };

})(window);
