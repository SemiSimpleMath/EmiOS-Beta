"""EmiCode console — bridge Jukka + Emi to a local Claude Code CLI session.

The chat widget at ``/emi-code`` POSTs user messages to the standard
``/process_request`` endpoint with ``room_id=emi_code_room``. The room
policy points at ``emi_code_room_manager`` (front-door manager that
dispatches to ``claude_code_invoke``, the tool that shells out to the
user's local ``claude`` CLI). Replies come back via the SocketIO
``user_message_data`` channel, the same mechanism every other chat
surface uses.
"""
from __future__ import annotations

from flask import Blueprint, render_template

from app.assistant.utils.identity_names import get_assistant_name
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

emi_code_bp = Blueprint("emi_code", __name__)


@emi_code_bp.route("/emi-code", methods=["GET"])
def emi_code_console():
    return render_template(
        "emi_code.html",
        assistant_name=get_assistant_name(),
    )
