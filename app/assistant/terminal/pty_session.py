"""A long-lived PTY running the interactive Claude Code CLI.

One session per terminal id. The session outlives the browser tab: reload the
page and you re-attach to the same running `claude`, scrollback and all, rather
than starting a fresh one.

`claude` is spawned with **no flags**. Everything that governs it — permission
mode, model, settings — comes from the machine's own configuration, so the
embedded terminal behaves identically to running `claude` in a local shell at
the repo root. On a Max plan that means auto mode is on by default.

Windows only for now: this uses ConPTY via pywinpty. A POSIX backend would use
the stdlib `pty` module; `spawn_backend_unavailable` says so plainly rather
than degrading into something that half works.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
from typing import Dict, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)


# How much output to keep for re-attach after a page reload. Claude Code paints
# a full screen on resize, so this only has to cover the visible state plus a
# little history; it is not a full transcript.
SCROLLBACK_LIMIT_CHARS = 256_000

# Idle poll interval for the reader thread. pywinpty's read() returns
# immediately with '' when the pty has nothing, so the loop needs to yield.
_READ_POLL_SECONDS = 0.01
_READ_CHUNK = 8192

# Environment markers stripped from the child. See _child_env().
_STRIPPED_ENV_PREFIXES = ("CLAUDE_CODE_",)
_STRIPPED_ENV_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class TerminalBackendUnavailable(RuntimeError):
    """Raised when this platform has no PTY backend wired up."""


class TerminalSession:
    """One `claude` process on a pty, with a reader thread pumping SocketIO."""

    def __init__(self, terminal_id: str, socketio, rows: int, cols: int) -> None:
        self.terminal_id = terminal_id
        self._socketio = socketio
        self._lock = threading.Lock()
        self._scrollback: list[str] = []
        self._scrollback_len = 0
        self._stopping = False

        cli = shutil.which("claude")
        if cli is None:
            raise TerminalBackendUnavailable(
                "The `claude` CLI is not on PATH. Install Claude Code "
                "(https://docs.claude.com/claude-code) and restart EmiOS."
            )

        self.proc = _spawn(cli, rows=rows, cols=cols)
        logger.info(
            "[terminal] started %s pid=%s cwd=%s size=%dx%d",
            terminal_id, self.proc.pid, get_repo_root(), rows, cols,
        )

        self._socketio.start_background_task(self._read_loop)

    # ---------------------------------------------------------------- reading

    def _read_loop(self) -> None:
        """Pump pty output to every client watching this terminal."""
        while not self._stopping:
            try:
                data = self.proc.read(_READ_CHUNK)
            except EOFError:
                break
            except OSError as exc:
                logger.warning("[terminal] %s read failed: %s", self.terminal_id, exc)
                break

            if not data:
                self._socketio.sleep(_READ_POLL_SECONDS)
                continue

            self._remember(data)
            self._socketio.emit(
                "terminal_output",
                {"terminal_id": self.terminal_id, "data": data},
                namespace="/terminal",
                to=self.terminal_id,
            )

        exit_status = self._exit_status()
        logger.info("[terminal] %s exited (status=%s)", self.terminal_id, exit_status)
        self._socketio.emit(
            "terminal_exit",
            {"terminal_id": self.terminal_id, "status": exit_status},
            namespace="/terminal",
            to=self.terminal_id,
        )
        _forget(self.terminal_id, self)

    def _exit_status(self):
        """The child's exit code, or None while it is still running."""
        status = getattr(self.proc, "exitstatus", None)
        return status

    def _remember(self, data: str) -> None:
        """Append to scrollback, trimming the oldest chunks past the cap."""
        with self._lock:
            self._scrollback.append(data)
            self._scrollback_len += len(data)
            while self._scrollback_len > SCROLLBACK_LIMIT_CHARS and len(self._scrollback) > 1:
                self._scrollback_len -= len(self._scrollback.pop(0))

    def scrollback(self) -> str:
        with self._lock:
            return "".join(self._scrollback)

    # ---------------------------------------------------------------- writing

    def write(self, data: str) -> None:
        self.proc.write(data)

    def resize(self, rows: int, cols: int) -> None:
        self.proc.setwinsize(rows, cols)

    def is_alive(self) -> bool:
        return bool(self.proc.isalive())

    def kill(self) -> None:
        self._stopping = True
        if self.proc.isalive():
            self.proc.terminate(force=True)
        logger.info("[terminal] %s killed on request", self.terminal_id)


# --------------------------------------------------------------------- spawn


def _child_env() -> Dict[str, str]:
    """The environment the CLI runs under.

    Two things are removed from EmiOS's own environment before handing it down:

    - ``CLAUDE_CODE_*`` markers. If EmiOS itself was started from inside a Claude
      Code session, the child inherits a marker that makes the CLI treat itself
      as a nested session and silently stop saving transcripts.
    - ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN``. EmiOS sets these for its
      own LLM calls, and an inherited key takes precedence over the user's
      claude.ai login — the embedded terminal would quietly bill the API
      instead of using the subscription the CLI is there to use. Stripping them
      lets the CLI resolve auth the way it does in a normal shell.
    """
    env = {
        k: v for k, v in os.environ.items()
        if k not in _STRIPPED_ENV_KEYS
        and not any(k.startswith(p) for p in _STRIPPED_ENV_PREFIXES)
    }
    env["TERM"] = "xterm-256color"
    return env


def _spawn(cli: str, *, rows: int, cols: int):
    """Start `claude` on a pty at the repo root. No flags, by design."""
    if sys.platform != "win32":
        raise TerminalBackendUnavailable(
            f"The embedded terminal has no PTY backend for {sys.platform!r} yet "
            f"(Windows/ConPTY only). Run `claude` in a normal shell instead."
        )
    try:
        from winpty import PtyProcess
    except ImportError as exc:
        raise TerminalBackendUnavailable(
            "pywinpty is not installed — run "
            "`.venv\\Scripts\\python.exe -m pip install pywinpty`."
        ) from exc

    return PtyProcess.spawn(
        [cli],
        cwd=str(get_repo_root()),
        env=_child_env(),
        dimensions=(rows, cols),
    )


# ------------------------------------------------------------------ registry

_SESSIONS: Dict[str, TerminalSession] = {}
_REGISTRY_LOCK = threading.Lock()


def get_session(terminal_id: str) -> Optional[TerminalSession]:
    """The live session for this id, or None if there isn't one."""
    with _REGISTRY_LOCK:
        session = _SESSIONS.get(terminal_id)
    if session is not None and not session.is_alive():
        _forget(terminal_id, session)
        return None
    return session


def get_or_create(terminal_id: str, *, socketio, rows: int, cols: int) -> TerminalSession:
    """Re-attach to the running session for this id, or start one."""
    existing = get_session(terminal_id)
    if existing is not None:
        return existing
    with _REGISTRY_LOCK:
        session = TerminalSession(terminal_id, socketio, rows, cols)
        _SESSIONS[terminal_id] = session
        return session


def kill_session(terminal_id: str) -> bool:
    """Kill the session if one is running. True if there was one to kill."""
    session = get_session(terminal_id)
    if session is None:
        return False
    session.kill()
    _forget(terminal_id, session)
    return True


def _forget(terminal_id: str, session: TerminalSession) -> None:
    """Drop a dead session from the registry, if it is still the one on file."""
    with _REGISTRY_LOCK:
        if _SESSIONS.get(terminal_id) is session:
            del _SESSIONS[terminal_id]


def shutdown_all() -> None:
    """Kill every live terminal. Called when EmiOS is shutting down."""
    with _REGISTRY_LOCK:
        sessions = list(_SESSIONS.values())
        _SESSIONS.clear()
    for session in sessions:
        session.kill()
