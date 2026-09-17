"""EmiCode console — an embedded terminal running the Claude Code CLI.

The page at ``/emi-code`` is a real terminal (xterm.js) bound over SocketIO to
an interactive ``claude`` process on a pty at the repo root. The CLI is started
with no flags, so it uses the machine's own configuration — permission mode,
model, `CLAUDE.md`, `.claude/hooks/`, `.claude/skills/` — exactly as it would in
a local shell.

The session lives in ``app/assistant/terminal/`` and outlasts the page: a
reload re-attaches to the running process instead of starting a new one. It is
loopback-only; see ``terminal/socket_handlers.py``.
"""
from __future__ import annotations

from flask import Blueprint, render_template

from app.assistant.terminal.socket_handlers import DEFAULT_TERMINAL_ID
from app.assistant.utils.identity_names import get_assistant_name
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

emi_code_bp = Blueprint("emi_code", __name__)


@emi_code_bp.route("/emi-code", methods=["GET"])
def emi_code_console():
    return render_template(
        "emi_code.html",
        assistant_name=get_assistant_name(),
        terminal_id=DEFAULT_TERMINAL_ID,
    )
