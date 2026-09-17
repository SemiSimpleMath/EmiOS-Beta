"""SocketIO wiring for the embedded terminal.

Clients join a room named after the terminal id, so every tab looking at the
same terminal sees the same output — reload the page, or open a second tab, and
both watch the one running `claude`.

**Loopback only.** A terminal on a web page is a shell on the machine EmiOS runs
on, and EmiOS binds 0.0.0.0 in dev with no auth on any route. Every handler here
refuses a non-loopback peer, so the page works at localhost and is inert from
the LAN or through a tunnel. Widening this is a deliberate act: change
`_peer_is_local`, and put an authentication layer in front of it first.
"""
from __future__ import annotations

from flask import request
from flask_socketio import join_room

from app.assistant.terminal import pty_session
from app.assistant.terminal.pty_session import TerminalBackendUnavailable
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

NAMESPACE = "/terminal"

# The only terminal the UI offers today. Kept as an id rather than hardcoded at
# every call site so a second terminal is a matter of passing another name.
DEFAULT_TERMINAL_ID = "emi_code"

_LOOPBACK_ADDRS = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}

# Guard rails on the geometry a client can ask for.
_MIN_ROWS, _MIN_COLS = 4, 20
_MAX_ROWS, _MAX_COLS = 300, 600


def _peer_is_local() -> bool:
    return (request.remote_addr or "") in _LOOPBACK_ADDRS


def _terminal_id(data) -> str:
    if isinstance(data, dict):
        raw = data.get("terminal_id")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return DEFAULT_TERMINAL_ID


def _geometry(data) -> tuple[int, int]:
    """Clamp the client's requested size into something a pty will accept."""
    rows, cols = 30, 100
    if isinstance(data, dict):
        try:
            rows = int(data.get("rows") or rows)
            cols = int(data.get("cols") or cols)
        except (TypeError, ValueError):
            logger.debug("[terminal] unparseable geometry %r, using default", data)
    return (
        max(_MIN_ROWS, min(_MAX_ROWS, rows)),
        max(_MIN_COLS, min(_MAX_COLS, cols)),
    )


def register_terminal_handlers(socketio) -> None:
    """Attach the /terminal namespace handlers to the SocketIO server."""

    def _refuse_remote(event: str, terminal_id: str) -> bool:
        """True when this peer may not drive a terminal. Emits the refusal."""
        if _peer_is_local():
            return False
        logger.warning(
            "[terminal] refused %s for %s from non-loopback peer %s",
            event, terminal_id, request.remote_addr,
        )
        socketio.emit(
            "terminal_error",
            {
                "terminal_id": terminal_id,
                "message": (
                    "The embedded terminal is available on this machine only. "
                    "Open EmiOS at http://localhost:8000/emi-code."
                ),
            },
            namespace=NAMESPACE,
            to=request.sid,
        )
        return True

    @socketio.on("terminal_attach", namespace=NAMESPACE)
    def terminal_attach(data=None):
        terminal_id = _terminal_id(data)
        if _refuse_remote("terminal_attach", terminal_id):
            return
        rows, cols = _geometry(data)
        join_room(terminal_id, namespace=NAMESPACE)

        resumed = pty_session.get_session(terminal_id) is not None
        try:
            session = pty_session.get_or_create(
                terminal_id, socketio=socketio, rows=rows, cols=cols,
            )
        except TerminalBackendUnavailable as exc:
            logger.error("[terminal] cannot start %s: %s", terminal_id, exc)
            socketio.emit(
                "terminal_error",
                {"terminal_id": terminal_id, "message": str(exc)},
                namespace=NAMESPACE,
                to=request.sid,
            )
            return

        socketio.emit(
            "terminal_ready",
            {
                "terminal_id": terminal_id,
                "resumed": resumed,
                "scrollback": session.scrollback() if resumed else "",
            },
            namespace=NAMESPACE,
            to=request.sid,
        )
        logger.info(
            "[terminal] %s attached to %s (resumed=%s)",
            request.sid, terminal_id, resumed,
        )

    @socketio.on("terminal_input", namespace=NAMESPACE)
    def terminal_input(data=None):
        terminal_id = _terminal_id(data)
        if _refuse_remote("terminal_input", terminal_id):
            return
        session = pty_session.get_session(terminal_id)
        if session is None:
            return
        keys = (data or {}).get("data")
        if isinstance(keys, str) and keys:
            session.write(keys)

    @socketio.on("terminal_resize", namespace=NAMESPACE)
    def terminal_resize(data=None):
        terminal_id = _terminal_id(data)
        if _refuse_remote("terminal_resize", terminal_id):
            return
        session = pty_session.get_session(terminal_id)
        if session is None:
            return
        rows, cols = _geometry(data)
        session.resize(rows, cols)

    @socketio.on("terminal_kill", namespace=NAMESPACE)
    def terminal_kill(data=None):
        terminal_id = _terminal_id(data)
        if _refuse_remote("terminal_kill", terminal_id):
            return
        killed = pty_session.kill_session(terminal_id)
        logger.info("[terminal] kill %s requested (was running=%s)", terminal_id, killed)

    logger.info("[terminal] handlers registered on %s (loopback only)", NAMESPACE)
