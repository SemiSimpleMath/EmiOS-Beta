"""
Ticket API
==========

REST endpoints for ticket interactions.
Route layer - delegates to TicketService for business logic.
"""

from flask import Blueprint, jsonify, request

from app.assistant.room_session_manager.services.plan_session_service import PlanSessionService
from app.assistant.utils.identity_names import get_required_primary_user_name
from app.assistant.utils.logging_config import get_logger
from app.assistant.ServiceLocator.service_locator import DI

logger = get_logger(__name__)

ticket_api_bp = Blueprint('ticket_api', __name__)


def _plan_sessions() -> PlanSessionService:
    """Return a PlanSessionService instance bound to the global blackboard."""
    return PlanSessionService(blackboard=DI.global_blackboard)


_TICKET_TYPE_DEFAULT_ROOMS: dict[str, str] = {
    "dayflow_orchestrator": "dayflow_orchestrator",
}


def _extract_ticket_room_id(ticket) -> str:
    if ticket is None:
        raise ValueError("ticket is required")
    trigger_context = getattr(ticket, "trigger_context", None)
    if not isinstance(trigger_context, dict):
        raise ValueError(
            f"ticket.trigger_context must be a dict, got {type(trigger_context).__name__}. "
            f"ticket_id={getattr(ticket, 'ticket_id', '?')!r}"
        )
    room_id = trigger_context.get("room_id")
    if not isinstance(room_id, str) or not room_id.strip():
        ticket_type = str(getattr(ticket, "ticket_type", "") or "").strip()
        fallback = _TICKET_TYPE_DEFAULT_ROOMS.get(ticket_type)
        if fallback:
            logger.debug(
                "ticket.trigger_context.room_id missing; using type-default room %r "
                "ticket_id=%r ticket_type=%r",
                fallback,
                getattr(ticket, "ticket_id", "?"),
                ticket_type,
            )
            return fallback
        raise ValueError(
            f"ticket.trigger_context.room_id must be a non-empty string — "
            f"ticket_id={getattr(ticket, 'ticket_id', '?')!r} "
            f"trigger_context={trigger_context!r}"
        )
    return room_id.strip()


@ticket_api_bp.route('/api/tickets/pending')
def get_pending_tickets():
    """
    Get tickets ready to show to user.
    Returns tickets in PENDING or PROPOSED state.
    """
    try:
        ticket_manager = DI.ticket_manager
        tickets = ticket_manager.get_tickets_pending_or_proposed()
        active_ticket_ids = _plan_sessions().list_active_ticket_ids()
        tickets = [t for t in tickets if str(getattr(t, "ticket_id", "") or "") not in active_ticket_ids]

        return jsonify({
            "count": len(tickets),
            "tickets": [t.to_dict() for t in tickets],
        })

    except Exception as e:
        logger.error(f"Error getting pending tickets: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@ticket_api_bp.route('/api/tickets/respond', methods=['POST'])
def respond_to_ticket():
    """
    Handle user response to a ticket.
    
    Request body:
    {
        "ticket_id": "...",
        "action": "done" | "skip" | "later" | "acknowledge" | "willdo" | "no" | "accept" | "dismiss",
        "user_text": "optional user explanation",
        "snooze_minutes": 30  // only used if action is "later"
    }
    
    Actions:
    - Activity layout: "done" (completed), "skip" (dismissed), "later" (snoozed)
    - Advice layout: "acknowledge" (received), "willdo" (will act), "no" (not applicable), "later"
    - Tool approval: "accept" (allow), "dismiss" (deny)
    """
    try:
        from app.assistant.ticket_manager.ticket_service import get_ticket_service

        data = request.get_json() or {}

        ticket_id = data.get('ticket_id', '')
        action = data.get('action', '')
        user_text = data.get('user_text', '')
        snooze_minutes = data.get('snooze_minutes', 30)

        service = get_ticket_service()
        response = service.respond(
            ticket_id=ticket_id,
            action=action,
            user_text=user_text or None,
            snooze_minutes=snooze_minutes,
        )

        result = {
            "ticket_id": response.ticket_id,
            "action": response.action,
            "success": response.success,
        }

        if response.error:
            return jsonify({"error": response.error}), 400

        if response.snooze_until:
            result["snooze_until"] = response.snooze_until.isoformat()

        return jsonify(result)

    except Exception as e:
        logger.error(f"Error responding to ticket: {e}", exc_info=True)
        logger.debug("ticket respond endpoint exception details", exc_info=True)
        return jsonify({"error": str(e)}), 500


@ticket_api_bp.route('/api/tickets/plan/start', methods=['POST'])
def start_ticket_plan_session():
    """
    Start (or resume) a plan-chat session for a ticket.
    """
    try:
        ticket_manager = DI.ticket_manager
        data = request.get_json() or {}
        ticket_id = str(data.get("ticket_id") or "").strip()
        if not ticket_id:
            return jsonify({"error": "ticket_id required"}), 400

        ticket = ticket_manager.get_ticket_by_id(ticket_id)
        if ticket is None:
            return jsonify({"error": "ticket not found"}), 404
        room_id = _extract_ticket_room_id(ticket)

        session_view = _plan_sessions().start_or_resume_ticket_session(
            ticket_id=ticket_id,
            room_id=room_id,
            room_context_id=f"plan::{ticket_id}",
            room_surface="ui",
        )
        return jsonify(session_view)
    except Exception as e:
        logger.error("Error starting ticket plan session: %s", e)
        logger.debug("ticket plan start exception details", exc_info=True)
        return jsonify({"error": str(e)}), 500


@ticket_api_bp.route('/api/tickets/plan/chat', methods=['POST'])
def chat_ticket_plan_session():
    """
    Send one chat line to active orchestrator plan session and return response text.
    """
    try:
        data = request.get_json() or {}
        ticket_id = str(data.get("ticket_id") or "").strip()
        plan_session_id = str(data.get("plan_session_id") or "").strip()
        text = str(data.get("text") or "").strip()
        socket_id = str(data.get("socket_id") or "").strip()
        sender_name = str(data.get("sender_name") or "").strip() or get_required_primary_user_name()

        if not ticket_id:
            return jsonify({"error": "ticket_id required"}), 400
        if not plan_session_id:
            return jsonify({"error": "plan_session_id required"}), 400
        if not text:
            return jsonify({"error": "text required"}), 400

        payload = _plan_sessions().get_ticket_session(plan_session_id)
        if not isinstance(payload, dict):
            return jsonify({"error": "plan session not found"}), 404
        if str(payload.get("ticket_id") or "") != ticket_id:
            return jsonify({"error": "plan session ticket mismatch"}), 400
        if str(payload.get("status") or "") != "active":
            return jsonify({"error": "plan session is not active"}), 400

        room_id = str(payload.get("room_id") or "").strip()
        if not room_id:
            raise RuntimeError("Plan session room_id is missing")
        metadata = {
            "plan_session_id": plan_session_id,
            "ticket_id": ticket_id,
            "room_context_id": payload.get("room_context_id") or f"plan::{ticket_id}",
            "reply_to": {"type": "socketio", "room_id": room_id},
        }
        outcome = DI.room_session_manager.handle_ui_inbound(
            socket_id=socket_id,
            body=text,
            room_id=room_id,
            sender_name=sender_name,
            send_reply=False,
            metadata=metadata,
            message_persistence_mode="global_blackboard_only",
        )
        reply_text = str((outcome or {}).get("reply_text") or "").strip()
        _plan_sessions().touch_ticket_session(plan_session_id)

        return jsonify(
            {
                "ticket_id": ticket_id,
                "plan_session_id": plan_session_id,
                "status": "active",
                "reply_text": reply_text,
            }
        )
    except Exception as e:
        logger.error("Error chatting in ticket plan session: %s", e)
        logger.debug("ticket plan chat exception details", exc_info=True)
        return jsonify({"error": str(e)}), 500


@ticket_api_bp.route('/api/tickets/plan/done', methods=['POST'])
def done_ticket_plan_session():
    """
    Complete a plan session and accept the source ticket.
    """
    try:
        data = request.get_json() or {}
        ticket_id = str(data.get("ticket_id") or "").strip()
        plan_session_id = str(data.get("plan_session_id") or "").strip()
        user_text = str(data.get("user_text") or "").strip()

        if not ticket_id:
            return jsonify({"error": "ticket_id required"}), 400
        if not plan_session_id:
            return jsonify({"error": "plan_session_id required"}), 400

        _plan_sessions().set_ticket_session_status(
            ticket_id=ticket_id,
            plan_session_id=plan_session_id,
            status="done",
        )

        from app.assistant.ticket_manager.ticket_service import get_ticket_service

        service = get_ticket_service()
        response = service.respond(
            ticket_id=ticket_id,
            action="acknowledge",
            user_text=user_text or "User completed plan mode session.",
            snooze_minutes=30,
        )
        if not response.success:
            return jsonify({"error": response.error or "failed to update ticket"}), 400

        return jsonify(
            {
                "ticket_id": ticket_id,
                "plan_session_id": plan_session_id,
                "status": "done",
                "success": True,
            }
        )
    except Exception as e:
        logger.error("Error completing ticket plan session: %s", e)
        logger.debug("ticket plan done exception details", exc_info=True)
        return jsonify({"error": str(e)}), 500


@ticket_api_bp.route('/api/tickets/plan/cancel', methods=['POST'])
def cancel_ticket_plan_session():
    """
    Cancel a plan session and dismiss the source ticket.
    """
    try:
        data = request.get_json() or {}
        ticket_id = str(data.get("ticket_id") or "").strip()
        plan_session_id = str(data.get("plan_session_id") or "").strip()
        user_text = str(data.get("user_text") or "").strip()

        if not ticket_id:
            return jsonify({"error": "ticket_id required"}), 400
        if not plan_session_id:
            return jsonify({"error": "plan_session_id required"}), 400

        _plan_sessions().set_ticket_session_status(
            ticket_id=ticket_id,
            plan_session_id=plan_session_id,
            status="cancelled",
        )

        from app.assistant.ticket_manager.ticket_service import get_ticket_service

        service = get_ticket_service()
        response = service.respond(
            ticket_id=ticket_id,
            action="dismiss",
            user_text=user_text or "User cancelled plan mode session.",
            snooze_minutes=30,
        )
        if not response.success:
            return jsonify({"error": response.error or "failed to update ticket"}), 400

        return jsonify(
            {
                "ticket_id": ticket_id,
                "plan_session_id": plan_session_id,
                "status": "cancelled",
                "success": True,
            }
        )
    except Exception as e:
        logger.error("Error cancelling ticket plan session: %s", e)
        logger.debug("ticket plan cancel exception details", exc_info=True)
        return jsonify({"error": str(e)}), 500


@ticket_api_bp.route('/api/tickets/count')
def get_pending_count():
    """Quick check for pending ticket count (for badge display)."""
    try:
        ticket_manager = DI.ticket_manager
        tickets = ticket_manager.get_tickets_pending_or_proposed()

        return jsonify({"count": len(tickets)})

    except Exception as e:
        logger.error(f"Error getting pending ticket count: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
