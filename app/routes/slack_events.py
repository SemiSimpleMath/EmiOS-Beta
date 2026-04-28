"""
Slack Events API webhook.

Handles real inbound Slack messages via https://api.slack.com/events-api.
Complements the /slack/room/simulate endpoint (dev-only injector) with a
production path that Slack itself can POST into.

Required env vars:
  SLACK_SIGNING_SECRET              — signs every request; used for HMAC
  SLACK_AUTHORIZED_TEAM_IDS         — comma-separated team_id allowlist
                                      (empty = reject everything, fail-closed)
  SLACK_AUTHORIZED_CHANNELS         — comma-separated channel_id allowlist
                                      (empty = reject everything, fail-closed)

Slack app configuration (out-of-band):
  1. api.slack.com/apps/<your-app>  → Basic Info → Signing Secret
  2. Event Subscriptions → Request URL = https://<host>/slack/events
  3. Subscribe to bot events: message.channels and/or message.im
  4. Reinstall app to workspace, invite bot to channels

Responses must return 200 within 3 seconds or Slack retries. Heavy work is
spawned onto a monitored background thread.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from app.assistant.lib.core_tools.slack.slack import SlackTool
from app.assistant.rooms.room_bootstrap import ensure_slack_room, make_slack_room_id
from app.assistant.runtime import start_monitored_thread
from app.assistant.utils.identity_names import resolve_display_name
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

slack_events_bp = Blueprint("slack_events", __name__)

# Slack retries for 3s timeout. Reject old timestamps to prevent replay attacks.
_MAX_TIMESTAMP_SKEW_S = 60 * 5

# Shared cache of Slack user_id → display name. Populated lazily via users.info.
_user_name_cache: dict[str, str] = {}


# ──────────────────────────────────────────────────────────────────────────
# Config helpers
# ──────────────────────────────────────────────────────────────────────────


def _required_signing_secret() -> str:
    secret = str(os.environ.get("SLACK_SIGNING_SECRET") or "").strip()
    if not secret:
        raise RuntimeError(
            "SLACK_SIGNING_SECRET not set. Cannot verify Slack webhook signatures."
        )
    return secret


def _authorized_team_ids() -> set[str]:
    raw = str(os.environ.get("SLACK_AUTHORIZED_TEAM_IDS") or "").strip()
    if not raw:
        return set()
    return {p.strip() for p in raw.split(",") if p.strip()}


def _authorized_channels() -> set[str]:
    raw = str(os.environ.get("SLACK_AUTHORIZED_CHANNELS") or "").strip()
    if not raw:
        return set()
    return {p.strip() for p in raw.split(",") if p.strip()}


# ──────────────────────────────────────────────────────────────────────────
# Signature verification (api.slack.com/authentication/verifying-requests-from-slack)
# ──────────────────────────────────────────────────────────────────────────


def _verify_slack_signature(flask_request) -> bool:
    """Validate X-Slack-Signature against SLACK_SIGNING_SECRET. Reject stale timestamps."""
    ts_raw = str(flask_request.headers.get("X-Slack-Request-Timestamp") or "").strip()
    signature = str(flask_request.headers.get("X-Slack-Signature") or "").strip()
    if not ts_raw or not signature:
        return False

    try:
        ts = int(ts_raw)
    except ValueError:
        return False
    if abs(time.time() - ts) > _MAX_TIMESTAMP_SKEW_S:
        logger.warning("Slack signature rejected: stale timestamp (skew=%ds).", int(time.time() - ts))
        return False

    secret = _required_signing_secret()
    # Must be the RAW body — get_data(as_text=False) returns bytes before
    # any Flask parsing. Using request.form would double-decode URL-encoded
    # characters and break the HMAC.
    raw_body = flask_request.get_data(cache=True, as_text=False)
    basestring = f"v0:{ts}:".encode("utf-8") + raw_body
    mac = hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256)
    expected = f"v0={mac.hexdigest()}"
    return hmac.compare_digest(expected, signature)


# ──────────────────────────────────────────────────────────────────────────
# Event parsing
# ──────────────────────────────────────────────────────────────────────────


def _extract_message_event(envelope: dict[str, Any]) -> dict[str, str] | None:
    """Flatten a Slack event_callback envelope into the fields we care about.

    Returns None for events that should be ignored (bot-originated, non-message,
    edited/deleted messages, etc.)
    """
    event = envelope.get("event")
    if not isinstance(event, dict):
        return None
    if str(event.get("type") or "") != "message":
        return None

    # Ignore bot-originated events. Without this check, the assistant responds
    # to its own outbound messages in an infinite loop.
    if event.get("bot_id") or event.get("bot_profile"):
        return None
    subtype = str(event.get("subtype") or "")
    if subtype in {"bot_message", "message_changed", "message_deleted", "channel_join",
                   "channel_leave", "message_replied"}:
        return None

    text = str(event.get("text") or "").strip()
    if not text:
        return None

    channel_id = str(event.get("channel") or "").strip()
    if not channel_id:
        return None

    user_id = str(event.get("user") or "").strip()
    message_ts = str(event.get("ts") or "").strip()
    thread_ts = str(event.get("thread_ts") or "").strip()

    # Display name: Slack doesn't give us a user name inline — only user_id.
    # Look it up via users.info (cached) so logs and room prompts show real names
    # instead of raw U03E7L283S9-style IDs.
    resolved_name = ""
    if user_id:
        try:
            resolved_name = SlackTool().resolve_speaker_name(
                {"user": user_id}, _user_name_cache
            )
        except Exception as e:
            logger.warning("Slack users.info lookup failed for %s: %s", user_id, e)
    sender_display_name = resolve_display_name(
        raw_name=resolved_name,
        role="participant",
        external_id=user_id,
        prefer_external_id_for_participant=True,
    )

    return {
        "channel_id": channel_id,
        "user_id": user_id,
        "text": text,
        "message_ts": message_ts,
        "thread_ts": thread_ts,
        "sender_display_name": sender_display_name,
    }


# ──────────────────────────────────────────────────────────────────────────
# Async inbound processing
# ──────────────────────────────────────────────────────────────────────────


def _process_slack_inbound_async(*, app, payload: dict[str, str]) -> None:
    try:
        with app.app_context():
            channel_id = payload["channel_id"]
            text = payload["text"]
            user_id = payload.get("user_id", "")
            sender_name = payload.get("sender_display_name") or user_id
            message_ts = payload.get("message_ts", "")
            thread_ts = payload.get("thread_ts", "")

            room_id = make_slack_room_id(channel_id)
            ensure_slack_room(
                room_id=room_id,
                channel_id=channel_id,
                sender_name=sender_name,
            )

            app.DI.room_session_manager.handle_slack_inbound(
                channel_id=channel_id,
                body=text,
                room_id=room_id,
                message_ts=message_ts,
                sender_name=sender_name,
                sender_id=user_id,
                thread_ts=thread_ts,
                image_paths=[],
                send_reply=True,
                persist_to_history=True,
                message_persistence_mode="global_blackboard_and_unified_log",
                simulated=False,
            )
    except Exception as e:
        logger.error("Slack async inbound processing failed: %s", e)
        logger.debug("Slack async inbound processing exception details", exc_info=True)


# ──────────────────────────────────────────────────────────────────────────
# Webhook
# ──────────────────────────────────────────────────────────────────────────


@slack_events_bp.route("/slack/events", methods=["POST"])
def slack_events_webhook():
    # 1. Signature verification. Do this BEFORE any other parsing so a bad
    #    actor can't probe our behavior with malformed payloads.
    try:
        if not _verify_slack_signature(request):
            logger.warning("Slack webhook rejected: invalid or missing signature.")
            return jsonify({"ok": False, "error": "invalid_signature"}), 401
    except RuntimeError as e:
        logger.error("Slack signature verification unavailable: %s", e)
        return jsonify({"ok": False, "error": "unavailable"}), 503

    envelope = request.get_json(silent=True)
    if not isinstance(envelope, dict):
        return jsonify({"ok": True}), 200

    # 2. URL verification handshake. Slack POSTs this once when you configure
    #    the Request URL in the app admin UI. We echo the challenge.
    if envelope.get("type") == "url_verification":
        challenge = str(envelope.get("challenge") or "")
        logger.info("Slack URL verification handshake completed.")
        return jsonify({"challenge": challenge}), 200

    # 3. Only handle event_callback envelopes.
    if envelope.get("type") != "event_callback":
        return jsonify({"ok": True}), 200

    # 4. Idempotency: Slack retries on non-2xx or slow response. Skip already-seen
    #    event_ids so a retry doesn't duplicate-process.
    event_id = str(envelope.get("event_id") or "").strip()
    if event_id:
        from app.routes.webhook_dedup import is_duplicate_slack
        if is_duplicate_slack(event_id):
            logger.info("Slack webhook skipped (duplicate event_id=%s).", event_id)
            return jsonify({"ok": True}), 200

    # 5. Team authorization — make sure this event is from a workspace we expect.
    team_id = str(envelope.get("team_id") or "").strip()
    authorized_teams = _authorized_team_ids()
    if team_id not in authorized_teams:
        logger.warning(
            "Slack webhook rejected: team_id=%r not in SLACK_AUTHORIZED_TEAM_IDS.",
            team_id,
        )
        return jsonify({"ok": True}), 200

    # 6. Extract the message we actually care about. Filters out bot echoes,
    #    edited/deleted messages, and channel_join events.
    payload = _extract_message_event(envelope)
    if payload is None:
        return jsonify({"ok": True}), 200

    # 7. Channel authorization.
    channel_id = payload["channel_id"]
    authorized_channels = _authorized_channels()
    if channel_id not in authorized_channels:
        logger.warning(
            "Slack webhook rejected: channel_id=%r not in SLACK_AUTHORIZED_CHANNELS.",
            channel_id,
        )
        return jsonify({"ok": True}), 200

    # 8. Spawn async worker and 200 immediately — Slack's 3s deadline.
    app = current_app._get_current_object()
    start_monitored_thread(
        owner="slack_events",
        name="slack-events-async",
        target=_process_slack_inbound_async,
        kwargs={"app": app, "payload": payload},
        daemon=True,
        kind="request_worker",
        metadata={"component": "slack_events"},
    )
    return jsonify({"ok": True}), 200
