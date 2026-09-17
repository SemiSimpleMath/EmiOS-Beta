// EmiCode — xterm.js bound to an interactive `claude` running on a pty server-side.
//
// The server owns the session: it outlives this page. Reloading re-attaches to
// the same running process and replays its scrollback, so the terminal picks up
// mid-thought rather than starting over.
(function () {
  "use strict";

  const TERMINAL_ID = document.body.dataset.terminalId || "emi_code";
  const RESIZE_DEBOUNCE_MS = 120;

  const statusEl = document.getElementById("emi-code-status");
  const restartBtn = document.getElementById("emi-code-restart");
  const hostEl = document.getElementById("emi-code-terminal");

  // Matches the page chrome so the pty's own background blends into the shell.
  const theme = {
    background: "#12131a",
    foreground: "#d8dae4",
    cursor: "#d77757",
    cursorAccent: "#12131a",
    selectionBackground: "#3a3f55",
    black: "#2a2d3a", brightBlack: "#555a70",
    red: "#e06c75", brightRed: "#ff7b86",
    green: "#98c379", brightGreen: "#b5e890",
    yellow: "#e5c07b", brightYellow: "#ffd694",
    blue: "#61afef", brightBlue: "#7fc7ff",
    magenta: "#c678dd", brightMagenta: "#e39bf5",
    cyan: "#56b6c2", brightCyan: "#70d6e2",
    white: "#d8dae4", brightWhite: "#ffffff",
  };

  const term = new window.Terminal({
    theme,
    fontFamily: '"Cascadia Mono", "JetBrains Mono", Consolas, "Courier New", monospace',
    fontSize: 13,
    lineHeight: 1.2,
    cursorBlink: true,
    scrollback: 10000,
    allowProposedApi: true,
  });

  const fitAddon = new window.FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.loadAddon(new window.WebLinksAddon.WebLinksAddon());
  term.open(hostEl);
  fitAddon.fit();

  function setStatus(text, state) {
    statusEl.textContent = text;
    statusEl.className = "emi-code-status " + (state ? "is-" + state : "");
  }

  function dims() {
    return { rows: term.rows, cols: term.cols };
  }

  // ------------------------------------------------------------------ socket

  const socket = io("/terminal", { transports: ["websocket", "polling"] });

  function attach() {
    socket.emit("terminal_attach", Object.assign({ terminal_id: TERMINAL_ID }, dims()));
  }

  socket.on("connect", function () {
    setStatus("attaching…", "connecting");
    fitAddon.fit();
    attach();
  });

  socket.on("disconnect", function () {
    setStatus("disconnected", "error");
  });

  socket.on("connect_error", function (err) {
    setStatus("connection failed", "error");
    term.writeln("\r\n\x1b[31mSocket connection failed: " + (err && err.message) + "\x1b[0m");
  });

  socket.on("terminal_ready", function (msg) {
    if (msg.terminal_id !== TERMINAL_ID) return;
    if (msg.resumed && msg.scrollback) {
      term.write(msg.scrollback);
      // Nudge the size so the CLI repaints its full-screen UI over the replay.
      nudgeResize();
      setStatus("attached", "live");
    } else {
      setStatus("started", "live");
    }
    term.focus();
  });

  socket.on("terminal_output", function (msg) {
    if (msg.terminal_id !== TERMINAL_ID) return;
    term.write(msg.data);
  });

  socket.on("terminal_exit", function (msg) {
    if (msg.terminal_id !== TERMINAL_ID) return;
    setStatus("exited", "error");
    term.writeln(
      "\r\n\x1b[33m[claude exited" +
      (msg.status === null || msg.status === undefined ? "" : " with status " + msg.status) +
      " — press Restart to start a new session]\x1b[0m"
    );
  });

  socket.on("terminal_error", function (msg) {
    setStatus("unavailable", "error");
    term.writeln("\r\n\x1b[31m" + msg.message + "\x1b[0m");
  });

  // ------------------------------------------------------------------- input

  term.onData(function (data) {
    socket.emit("terminal_input", { terminal_id: TERMINAL_ID, data: data });
  });

  // Claude Code turns on mouse reporting; xterm forwards those as binary.
  term.onBinary(function (data) {
    socket.emit("terminal_input", { terminal_id: TERMINAL_ID, data: data });
  });

  // ------------------------------------------------------------------ resize

  let resizeTimer = null;

  function sendResize() {
    fitAddon.fit();
    socket.emit("terminal_resize", Object.assign({ terminal_id: TERMINAL_ID }, dims()));
  }

  function nudgeResize() {
    // A size change is what makes a full-screen TUI repaint. Send one column
    // narrower, then the true size, so the CLI redraws from scratch.
    const d = dims();
    socket.emit("terminal_resize", {
      terminal_id: TERMINAL_ID, rows: d.rows, cols: Math.max(20, d.cols - 1),
    });
    setTimeout(function () {
      socket.emit("terminal_resize", Object.assign({ terminal_id: TERMINAL_ID }, dims()));
    }, 50);
  }

  function onViewportChange() {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(sendResize, RESIZE_DEBOUNCE_MS);
  }

  window.addEventListener("resize", onViewportChange);
  if (window.ResizeObserver) {
    new window.ResizeObserver(onViewportChange).observe(hostEl);
  }

  // ----------------------------------------------------------------- restart

  restartBtn.addEventListener("click", function () {
    if (!window.confirm("Kill the running claude session and start a fresh one?")) return;
    setStatus("restarting…", "connecting");
    term.reset();
    socket.emit("terminal_kill", { terminal_id: TERMINAL_ID });
    // The kill lands before this fires; attaching then spawns a new session.
    setTimeout(attach, 400);
  });

  // Clicking anywhere in the terminal area should put the caret back.
  hostEl.addEventListener("click", function () {
    if (!window.getSelection().toString()) term.focus();
  });
})();
