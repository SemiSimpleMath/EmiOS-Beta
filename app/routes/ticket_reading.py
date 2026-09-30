"""/read/<ticket_id>: a ticket's reading page — the whole message and everything it rides on.

The popup shows a long ticket's first lines and links here (static/js/proactive_popup.js); the
popup keeps the answer buttons, this page is for reading. The content is assembled by
app/assistant/ticket_manager/reading.py from links that already exist; no model is called.
"""
from __future__ import annotations

from flask import Blueprint, abort, jsonify, render_template

from app.routes._security import reject_if_not_local

ticket_reading_bp = Blueprint("ticket_reading", __name__)
ticket_reading_bp.before_request(reject_if_not_local)


@ticket_reading_bp.route("/read/<ticket_id>")
def read_ticket_page(ticket_id: str):
    return render_template("read_ticket.html", ticket_id=ticket_id)


@ticket_reading_bp.route("/api/read/<ticket_id>")
def read_ticket_api(ticket_id: str):
    from app.assistant.ticket_manager.reading import ticket_reading
    try:
        return jsonify(ticket_reading(ticket_id))
    except KeyError:
        abort(404)
