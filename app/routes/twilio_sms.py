from __future__ import annotations

import os
import re
import uuid
from typing import Any, Dict

from flask import Blueprint, Response, current_app, jsonify, request

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

twilio_sms_bp = Blueprint("twilio_sms", __name__)


def _dev_tools_enabled() -> bool:
    try:
        if str(os.environ.get("EMI_DEV_TOOLS", "")).strip().lower() in {"1", "true", "yes", "y", "on"}:
            return True
        host = (request.host or "").split(":", 1)[0].strip().lower()
        return host in {"127.0.0.1", "localhost"}
    except Exception:
        return False


def _authorized_sms_numbers() -> set[str]:
    """Set of E.164-formatted phone numbers allowed to SMS this endpoint.

    Pulled from ``TWILIO_AUTHORIZED_NUMBERS`` env var (comma-separated).
    If unset, returns the empty set, which means NO inbound SMS is accepted
    (fail-closed). Explicit allowlist prevents random callers from triggering
    the assistant.
    """
    raw = str(os.environ.get("TWILIO_AUTHORIZED_NUMBERS") or "").strip()
    if not raw:
        return set()
    return {part.strip() for part in raw.split(",") if part.strip()}


def _derive_room_id_for_number(from_number: str) -> str:
    """Map a From phone number to the owning room. Returns '' if unmapped.

    The canonical convention is ``sms_room::<slug>`` with slug derived from a
    config map ``TWILIO_NUMBER_ROOM_MAP`` env var in the form:
        ``+14155551234=phil,+14155555678=katy``
    Unmapped numbers are treated as unauthorized even if they're on the
    authorized list (defense in depth — you have to BOTH be authorized and
    have a room mapping).
    """
    raw = str(os.environ.get("TWILIO_NUMBER_ROOM_MAP") or "").strip()
    if not raw:
        return ""
    mapping: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" not in pair:
            continue
        number, room = pair.split("=", 1)
        number = number.strip()
        room = room.strip()
        if number and room:
            mapping[number] = room
    return mapping.get(from_number.strip(), "")


def _verify_twilio_signature(flask_request) -> bool:
    """Verify the X-Twilio-Signature header against the configured auth token.

    Twilio signs every inbound webhook; without this check, anyone who knows
    the public webhook URL can POST arbitrary From/Body/MessageSid values and
    inject SMS as the user.

    Returns True on valid signature; False on missing or invalid.
    Raises RuntimeError only if TWILIO_AUTH_TOKEN is missing (fail-loud — we
    don't want to silently skip verification).

    Bypass for localhost dev is allowed only when EMI_DEV_TOOLS is enabled
    AND the request is from 127.0.0.1 / localhost (see _dev_tools_enabled).
    """
    try:
        from twilio.request_validator import RequestValidator
    except ImportError as e:
        raise RuntimeError(
            "twilio package not installed; run `pip install twilio` to enable SMS webhook signature verification."
        ) from e

    auth_token = str(os.environ.get("TWILIO_AUTH_TOKEN") or "").strip()
    if not auth_token:
        raise RuntimeError(
            "TWILIO_AUTH_TOKEN not set; refusing to accept unsigned Twilio webhook. "
            "Set the env var or disable the /twilio/sms route."
        )
    # signature verification proceeds below
    # (this comment retained so the function body remains small and readable)

    signature = flask_request.headers.get("X-Twilio-Signature") or ""
    if not signature:
        return False

    validator = RequestValidator(auth_token)
    # Twilio signs against the full public URL. Behind a tunnel/proxy (Cloudflare),
    # `request.url` may show the internal URL. Prefer X-Forwarded-Proto/Host if set,
    # otherwise fall back to request.url.
    forwarded_proto = flask_request.headers.get("X-Forwarded-Proto", "").strip()
    forwarded_host = flask_request.headers.get("X-Forwarded-Host", "").strip()
    if forwarded_proto and forwarded_host:
        url = f"{forwarded_proto}://{forwarded_host}{flask_request.path}"
        if flask_request.query_string:
            url = f"{url}?{flask_request.query_string.decode('ascii', errors='ignore')}"
    else:
        url = flask_request.url

    # form params (not multipart) — Twilio sends application/x-www-form-urlencoded
    params = flask_request.form.to_dict()
    return validator.validate(url, params, signature)


def _publish_twilio_inbound(*, from_number: str, to_number: str, body: str, message_sid: str) -> str:
    request_id = str(uuid.uuid4())
    metadata = {
        "reply_to": {
            "type": "twilio_sms",
            "to": from_number,
            "from": to_number,
            "inbound_message_sid": message_sid,
        }
    }
    try:
        current_app.DI.reply_router.set_route(request_id, metadata.get("reply_to"))
    except Exception:
        logger.debug("Could not set reply route for request_id=%s", request_id, exc_info=True)

    current_app.DI.room_session_manager.handle_sms_inbound(
        from_number=from_number,
        to_number=to_number,
        body=body,
        message_sid=message_sid,
        room_id="master_room",
        send_reply=False,
        message_persistence_mode="global_blackboard_and_unified_log",
    )
    return request_id


def _derive_room_contact_name(room_id: str) -> str:
    rid = str(room_id or "").strip()
    if not rid:
        return "User"
    cleaned = re.sub(r"[_\-]+", " ", rid).strip()
    if not cleaned:
        return "User"
    return cleaned[:1].upper() + cleaned[1:]


def _run_room_manager_for_sms(
    *,
    from_number: str,
    to_number: str,
    body: str,
    message_sid: str,
    room_id: str,
    message_persistence_mode: str = "global_blackboard_and_unified_log",
) -> Dict[str, Any]:
    session_manager = current_app.DI.room_session_manager
    return session_manager.handle_sms_inbound(
        from_number=from_number,
        to_number=to_number,
        body=body,
        message_sid=message_sid,
        room_id=room_id,
        send_reply=False,
        message_persistence_mode=message_persistence_mode,
    )


@twilio_sms_bp.route("/twilio/sms", methods=["POST"])
def twilio_inbound_sms():
    """
    Twilio SMS webhook (inbound).

    Twilio will POST form-encoded fields like:
    - From: user's phone number
    - To:   your Twilio number
    - Body: message text
    - MessageSid: Twilio message id

    Signature verification is enforced via X-Twilio-Signature unless the
    request is from localhost with dev tools enabled.
    """
    try:
        if not (_dev_tools_enabled() and str(os.environ.get("EMI_TWILIO_SKIP_SIG_LOCALHOST", "")).strip().lower() in {"1", "true", "yes"}):
            if not _verify_twilio_signature(request):
                logger.warning(
                    "Twilio inbound rejected: invalid or missing X-Twilio-Signature. remote=%s",
                    request.remote_addr,
                )
                return Response("Forbidden", status=403)
    except RuntimeError as e:
        logger.error("Twilio signature verification unavailable: %s", e)
        return Response("Service unavailable", status=503)

    try:
        from_number = str(request.values.get("From") or "").strip()
        to_number = str(request.values.get("To") or "").strip()
        body = str(request.values.get("Body") or "").strip()
        message_sid = str(request.values.get("MessageSid") or "").strip()
        message_persistence_mode = str(
            request.values.get("message_persistence_mode") or "global_blackboard_and_unified_log"
        ).strip().lower()
        if message_persistence_mode not in {"global_blackboard_only", "global_blackboard_and_unified_log"}:
            message_persistence_mode = "global_blackboard_and_unified_log"

        if not from_number or not to_number or not body:
            logger.warning("Twilio inbound missing From/To/Body.")
            return Response("<Response></Response>", mimetype="text/xml", status=200)

        # Idempotency: Twilio retries on non-2xx / slow responses. Skip if
        # we've already processed this MessageSid in the last N minutes.
        if message_sid:
            from app.routes.webhook_dedup import is_duplicate_twilio
            if is_duplicate_twilio(message_sid):
                logger.info("Twilio inbound skipped (duplicate MessageSid=%s).", message_sid)
                return Response("<Response></Response>", mimetype="text/xml", status=200)

        # Number-based authorization + room mapping. Both are required —
        # a number must be on the allowlist AND have a configured room.
        authorized = _authorized_sms_numbers()
        if from_number not in authorized:
            logger.warning(
                "Twilio inbound rejected: %s not in TWILIO_AUTHORIZED_NUMBERS.",
                from_number,
            )
            return Response("<Response></Response>", mimetype="text/xml", status=200)

        room_id = _derive_room_id_for_number(from_number)
        if not room_id:
            logger.warning(
                "Twilio inbound rejected: %s authorized but no TWILIO_NUMBER_ROOM_MAP entry.",
                from_number,
            )
            return Response("<Response></Response>", mimetype="text/xml", status=200)

        current_app.DI.room_session_manager.handle_sms_inbound(
            from_number=from_number,
            to_number=to_number,
            body=body,
            message_sid=message_sid,
            room_id=room_id,
            send_reply=True,
            message_persistence_mode=message_persistence_mode,
        )
    except Exception as e:
        logger.error("Twilio inbound SMS handler error: %s", e)
        logger.debug("twilio inbound SMS handler error exception details", exc_info=True)

    # Return empty TwiML; reply delivery is handled by RoomSessionManager.
    return Response("<Response></Response>", mimetype="text/xml", status=200)


@twilio_sms_bp.route("/twilio/sms/simulate", methods=["POST"])
def twilio_inbound_sms_simulate():
    """
    Dev-only simulator for Twilio inbound messages.

    Accepts JSON or form payload with:
    - from_number (or From)
    - to_number (or To)
    - body (or Body)
    - message_sid (optional; autogenerated if missing)
    - room_id (optional; defaults to "phil")
    - mode (optional): "room_manager" (default) or "event_hub"
    """
    if not _dev_tools_enabled():
        return jsonify({"error": "Not found"}), 404

    payload = request.get_json(silent=True) if request.is_json else None
    payload = payload if isinstance(payload, dict) else request.values

    from_number = str(payload.get("from_number") or payload.get("From") or "").strip()
    to_number = str(payload.get("to_number") or payload.get("To") or "").strip()
    body = str(payload.get("body") or payload.get("Body") or "").strip()
    message_sid = str(payload.get("message_sid") or payload.get("MessageSid") or "").strip()
    room_id = str(payload.get("room_id") or "phil").strip()
    mode = str(payload.get("mode") or "room_manager").strip().lower()
    message_persistence_mode = str(
        payload.get("message_persistence_mode") or "global_blackboard_and_unified_log"
    ).strip().lower()
    if message_persistence_mode not in {"global_blackboard_only", "global_blackboard_and_unified_log"}:
        return jsonify(
            {
                "error": (
                    "Invalid message_persistence_mode. "
                    "Use 'global_blackboard_only' or 'global_blackboard_and_unified_log'."
                )
            }
        ), 400
    if not message_sid:
        message_sid = f"SIM-{uuid.uuid4().hex[:12]}"

    if not from_number or not to_number or not body:
        return jsonify({"error": "Missing required fields: from_number, to_number, body"}), 400

    try:
        if mode == "event_hub":
            request_id = _publish_twilio_inbound(
                from_number=from_number,
                to_number=to_number,
                body=body,
                message_sid=message_sid,
            )
            return jsonify(
                {
                    "status": "accepted",
                    "simulated": True,
                    "mode": "event_hub",
                    "request_id": request_id,
                    "reply_to": {"type": "twilio_sms", "to": from_number, "from": to_number},
                }
            ), 202

        # Default simulator mode: run through room_manager synchronously and return preview.
        outcome = _run_room_manager_for_sms(
            from_number=from_number,
            to_number=to_number,
            body=body,
            message_sid=message_sid,
            room_id=room_id,
            message_persistence_mode=message_persistence_mode,
        )
        return jsonify(
            {
                "status": "ok",
                "simulated": True,
                "mode": "room_manager",
                "request_id": outcome["request_id"],
                "room_id": outcome["room_id"],
                "inbound": {
                    "from_number": from_number,
                    "to_number": to_number,
                    "body": body,
                    "message_sid": message_sid,
                },
                "reply_preview": outcome["reply_text"],
                "reply_payload": outcome["reply_payload"],
                "message_persistence_mode": outcome.get("message_persistence_mode"),
                "unified_log_persistence_enabled": outcome.get("unified_log_persistence_enabled"),
                "unified_log_persistence_reason": outcome.get("unified_log_persistence_reason"),
            }
        ), 200
    except Exception as e:
        logger.error("Twilio simulator handler error: %s", e)
        logger.debug("twilio simulator handler error exception details", exc_info=True)
        return jsonify({"error": "Simulation failed"}), 500

