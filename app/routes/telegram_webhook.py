from __future__ import annotations

import os
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from app.assistant.rooms.room_bootstrap import ensure_telegram_room, make_telegram_room_id
from app.assistant.runtime import start_monitored_thread
from app.assistant.utils.identity_names import resolve_display_name
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

telegram_webhook_bp = Blueprint("telegram_webhook", __name__)


def _required_secret_token() -> str:
    token = str(os.environ.get("TELEGRAM_WEBHOOK_SECRET") or "").strip()
    if not token:
        raise RuntimeError("Missing TELEGRAM_WEBHOOK_SECRET for Telegram webhook verification.")
    return token


def _authorized_chat_ids() -> set[str]:
    """Allowlist of Telegram chat_ids permitted to reach the assistant.

    Pulled from ``TELEGRAM_AUTHORIZED_CHAT_IDS`` env var (comma-separated).
    If unset, returns the empty set → fail-closed (no inbound accepted).
    Telegram's webhook secret proves the POST is from Telegram's servers,
    but the bot is still addressable by anyone on Telegram who finds it,
    so we need chat-level authorization on top of the transport-level
    verification.
    """
    raw = str(os.environ.get("TELEGRAM_AUTHORIZED_CHAT_IDS") or "").strip()
    if not raw:
        return set()
    return {part.strip() for part in raw.split(",") if part.strip()}


def _extract_message_payload(update: dict[str, Any]) -> dict[str, str] | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    text = str(message.get("text") or "").strip()
    if not text:
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    chat_id_raw = chat.get("id")
    chat_id = str(chat_id_raw).strip() if chat_id_raw is not None else ""
    if not chat_id:
        return None
    chat_type = str(chat.get("type") or "").strip().lower()
    message_id = str(message.get("message_id") or "").strip()
    sender = message.get("from")
    sender_id = ""
    sender_username = ""
    sender_first_name = ""
    sender_last_name = ""
    if isinstance(sender, dict):
        sender_id_raw = sender.get("id")
        if sender_id_raw is not None:
            sender_id = str(sender_id_raw).strip()
        sender_username = str(sender.get("username") or "").strip()
        sender_first_name = str(sender.get("first_name") or "").strip()
        sender_last_name = str(sender.get("last_name") or "").strip()
    sender_display_name = sender_username
    if not sender_display_name:
        full_name = " ".join(part for part in [sender_first_name, sender_last_name] if part)
        sender_display_name = full_name.strip()
    sender_display_name = resolve_display_name(
        raw_name=sender_display_name,
        role="participant",
        external_id=sender_id,
        prefer_external_id_for_participant=True,
    )
    return {
        "chat_id": chat_id,
        "chat_type": chat_type,
        "message_id": message_id,
        "text": text,
        "sender_id": sender_id,
        "sender_username": sender_username,
        "sender_display_name": sender_display_name,
    }


def _process_telegram_inbound_async(*, app, payload: dict[str, str]) -> None:
    try:
        with app.app_context():
            chat_id = str(payload.get("chat_id") or "").strip()
            text = str(payload.get("text") or "").strip()
            if not chat_id or not text:
                return
            sender_id = str(payload.get("sender_id") or "").strip()
            sender_display_name = str(payload.get("sender_display_name") or "").strip()
            message_id = str(payload.get("message_id") or "").strip()
            _chat_type = str(payload.get("chat_type") or "").strip().lower()

            room_id = make_telegram_room_id(chat_id)
            ensure_telegram_room(room_id, chat_id=chat_id, sender_username=sender_display_name)

            app.DI.room_session_manager.handle_telegram_inbound(
                chat_id=chat_id,
                body=text,
                room_id=room_id,
                message_id=message_id,
                sender_name=sender_display_name,
                sender_id=sender_id,
                send_reply=True,
                message_persistence_mode="global_blackboard_and_unified_log",
            )
    except Exception as e:
        logger.error("Telegram async inbound processing failed: %s", e)
        logger.debug("Telegram async inbound processing exception details", exc_info=True)


@telegram_webhook_bp.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    try:
        expected = _required_secret_token()
        header_token = str(request.headers.get("X-Telegram-Bot-Api-Secret-Token") or "").strip()
        if header_token != expected:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        update = request.get_json(silent=True)
        if not isinstance(update, dict):
            return jsonify({"ok": True}), 200

        # Idempotency: Telegram retries webhooks on non-2xx / slow responses.
        # Skip duplicate update_id so the same inbound isn't processed twice.
        update_id = update.get("update_id")
        if update_id is not None:
            from app.routes.webhook_dedup import is_duplicate_telegram
            if is_duplicate_telegram(update_id):
                logger.info("Telegram webhook skipped (duplicate update_id=%s).", update_id)
                return jsonify({"ok": True}), 200

        payload = _extract_message_payload(update)
        if payload is None:
            return jsonify({"ok": True}), 200

        # chat_id authorization — fail-closed. Unknown chat_ids don't get to
        # bootstrap their own room; they get a silent 200 so Telegram doesn't
        # retry, but are dropped. Configure via TELEGRAM_AUTHORIZED_CHAT_IDS.
        authorized = _authorized_chat_ids()
        chat_id = str(payload.get("chat_id") or "").strip()
        if chat_id not in authorized:
            logger.warning(
                "Telegram inbound rejected: chat_id=%s not in TELEGRAM_AUTHORIZED_CHAT_IDS.",
                chat_id,
            )
            return jsonify({"ok": True}), 200

        app = current_app._get_current_object()
        start_monitored_thread(
            owner="telegram_webhook",
            name="telegram-inbound-async",
            target=_process_telegram_inbound_async,
            kwargs={"app": app, "payload": payload},
            daemon=True,
            kind="request_worker",
            metadata={"component": "telegram_webhook"},
        )
        return jsonify({"ok": True}), 200
    except Exception as e:
        logger.error("Telegram webhook handler error: %s", e)
        logger.debug("Telegram webhook handler exception details", exc_info=True)
        return jsonify({"ok": False, "error": "handler_failed"}), 500

