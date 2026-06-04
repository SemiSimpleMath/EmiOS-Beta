"""KG dev console — read-only investigation of the live KG via natural language.

The chat widget at ``/kg-dev`` POSTs user messages to the standard
``/process_request`` endpoint with ``room_id=kg_dev_room``. The room
policy points at ``kg_dev_manager`` (a single-planner-loop manager
that calls ``kg_query`` / ``pod_search`` / ``pod_fetch``). Replies
come back via the SocketIO ``user_message_data`` channel, the same
mechanism every other chat surface uses.
"""
from __future__ import annotations

from flask import Blueprint, render_template

from app.assistant.utils.identity_names import get_assistant_name
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

kg_dev_bp = Blueprint("kg_dev", __name__)


@kg_dev_bp.route("/kg-dev", methods=["GET"])
def kg_dev_console():
    return render_template(
        "kg_dev.html",
        assistant_name=get_assistant_name(),
    )
