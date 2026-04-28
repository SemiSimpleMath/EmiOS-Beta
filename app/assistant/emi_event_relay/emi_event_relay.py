import queue
import threading
import os
from typing import Any, Dict

from app.assistant.utils.pydantic_classes import Message, UserMessage
from app.assistant.ServiceLocator.service_locator import DI
from app.services import text_to_speech
from queue import Empty

from app.assistant.utils.logging_config import get_logger
from app.assistant.runtime import MonitoredThreadPoolExecutor, start_monitored_thread
logger = get_logger(__name__)

class EmiEventRelay:
    """Handles WebSocket events, TTS processing, and UI updates for the Emi system."""

    def __init__(self):
        self.blackboard = DI.global_blackboard
        self.waiting_for_user_response = False
        self.message_queue = queue.Queue()
        tts_workers = int(os.environ.get("EMI_TTS_WORKER_THREADS", "24"))
        self.tts_executor = MonitoredThreadPoolExecutor(
            name="emi-event-relay-tts",
            owner="emi_event_relay",
            max_workers=max(1, tts_workers),
            metadata={"component": "emi_event_relay"},
        )
        self.socket_lock = threading.Lock()

        start_monitored_thread(
            owner="emi_event_relay",
            name="emi-event-relay-queue",
            target=self.process_queue,
            daemon=True,
            kind="service_loop",
            metadata={"component": "emi_event_relay", "loop": "process_queue"},
        )

        DI.event_hub.register_event('socket_emit', self.socket_emit_handler)
        DI.event_hub.register_event('repo_update', self.notify_ui_of_repo_update)
        DI.event_hub.register_event('proactive_suggestion', self.proactive_suggestion_handler)
        DI.event_hub.register_event('proactive_suggestion_update', self.proactive_suggestion_update_handler)
        DI.event_hub.register_event('agent_progress_emit', self.agent_progress_emit_handler)

    def socket_emit_handler(self, message: UserMessage):
        """Queue messages for WebSocket emission, processing TTS if needed."""
        payload = message.user_message_data
        preferred_reply_to = None
        try:
            meta = getattr(message, "metadata", None)
            if isinstance(meta, dict):
                preferred_reply_to = meta.get("reply_to")
        except Exception:
            preferred_reply_to = None

        logger.info(
            "[TTS:relay] socket_emit_handler sender=%s payload.tts=%s preferred_reply_to=%r",
            getattr(message, "sender", "?"),
            getattr(payload, "tts", "?"),
            preferred_reply_to,
        )

        if payload.tts:
            if payload.tts_text:
                logger.info("[TTS:relay] Submitting TTS job. text_len=%d", len(payload.tts_text))
                self.tts_executor.submit(self._process_tts, payload.tts_text, message, preferred_reply_to)
            else:
                logger.warning("[TTS:relay] TTS flag set but 'tts_text' is missing.")
                self._emit_error('audio_file_error', "Missing 'tts_text' in payload.")

        self.message_queue.put((message, payload, preferred_reply_to))

    def notify_ui_of_repo_update(self, msg: Message):
        """Notifies the UI about repository updates via WebSocket."""
        logger.debug("notify_ui_of_repo_update: sending repo update notification")
        self._emit_to_room("master_room", "repo_update_notification", msg.data)

    def proactive_suggestion_handler(self, message: Message):
        """Emit proactive suggestion to frontend via WebSocket."""
        suggestion_data = message.data if hasattr(message, 'data') else {}
        self._emit_to_room("master_room", "proactive_suggestion", suggestion_data)

    def proactive_suggestion_update_handler(self, message: Message):
        """Signal the frontend that a ticket's state changed (expired, etc.).

        The frontend's `proactive_suggestion_update` handler re-fetches
        `/api/tickets/pending` on receipt, so the stale ticket falls off
        naturally once its DB state moves out of pending/proposed.
        """
        update_data = message.data if hasattr(message, 'data') else {}
        self._emit_to_room("master_room", "proactive_suggestion_update", update_data)

    def agent_progress_emit_handler(self, message: Message):
        """
        Emit curated agent-progress updates to frontend via Socket.IO.
        Payload is expected in message.data as a dict (already curated).
        The progress tab is optional — if it isn't open, we quietly skip.
        """
        payload = message.data if isinstance(getattr(message, "data", None), dict) else {}
        if not payload:
            return
        self._emit_to_room("progress", "agent_progress_update", payload)

    def _emit_to_room(self, room_id: str, event: str, payload) -> None:
        """
        Resolve room_id -> current socket_id and emit atomically.

        Uses ``socket_manager.emit_to_room`` so the lookup + emit sequence
        runs under the socket_manager's lock. The sweeper cannot evict the
        binding between resolve and emit, eliminating a silent-drop race.

        Log level for a missing binding depends on whether room_id is a
        required UI room (see _REQUIRED_UI_ROOMS). Master_room = ERROR
        because a missing binding there means a user-facing message was
        lost. All other rooms = DEBUG because "no recipient" is the normal
        state for optional UI surfaces, backend rooms, etc.
        """
        from app.services.socket_manager import RoomNotBound

        socket_io = DI.socket_io
        if not socket_io:
            logger.error("Cannot emit %s: DI.socket_io is not registered", event)
            return

        def _do_emit(socket_id: str) -> None:
            socket_io.emit(event, payload, room=socket_id)
            logger.debug("Emitted %s to room_id=%r socket=%s", event, room_id, socket_id[:8])

        try:
            DI.socket_manager.emit_to_room(room_id, _do_emit)
        except RoomNotBound:
            if self._is_required_ui_room(room_id):
                logger.error("Cannot emit %s: no live socket for required room_id=%r", event, room_id)
            else:
                logger.debug("Skipping %s: no live socket for optional room_id=%r", event, room_id)

    def process_queue(self):
        """Continuously process messages from the queue and emit via WebSocket."""
        while True:
            try:
                message, payload, preferred_reply_to = self.message_queue.get(timeout=1)
                self._emit_message(message, payload, preferred_reply_to)
            except Empty:
                continue

    def _resolve_reply_to(self, message, preferred_reply_to=None):
        """Single source of truth: per-message metadata.reply_to -> reply_router(request_id).

        No fallbacks. If neither source yields a reply_to, emission fails loudly.
        Callers that need a reply_to must ensure one was pinned at ingress.
        """
        if isinstance(preferred_reply_to, dict) and preferred_reply_to.get("type"):
            return preferred_reply_to

        meta = getattr(message, "metadata", None)
        if isinstance(meta, dict) and isinstance(meta.get("reply_to"), dict) and meta["reply_to"].get("type"):
            return meta["reply_to"]

        rid = getattr(message, "request_id", None)
        if rid:
            route = DI.reply_router.get_route(rid)
            if isinstance(route, dict) and route.get("type"):
                return route

        return None

    # Only master_room is a required UI-emit target. Every other logical room
    # (doc_editor, task_spec::*, dayflow_orchestrator, progress, music, etc.)
    # may legitimately have no UI tab open at a given moment — unified_log
    # keeps the record either way, so missing bindings there are not errors.
    _REQUIRED_UI_ROOMS: frozenset[str] = frozenset({"master_room"})

    @classmethod
    def _is_required_ui_room(cls, room_id: str) -> bool:
        return str(room_id or "").strip() in cls._REQUIRED_UI_ROOMS

    def _emit_via_socketio(self, *, event: str, payload: dict, reply_to: Dict[str, Any]):
        from app.services.socket_manager import RoomNotBound
        room_id = str(reply_to.get("room_id") or "").strip()
        if not room_id:
            logger.error("Cannot emit %s: reply_to missing room_id. reply_to=%r", event, reply_to)
            return
        try:
            socket_id = DI.socket_manager.resolve_socket(room_id)
        except RoomNotBound:
            if self._is_required_ui_room(room_id):
                logger.error("Cannot emit %s: no live socket for required room_id=%r", event, room_id)
            else:
                logger.debug("Skipping %s: no live socket for optional room_id=%r", event, room_id)
            return
        socket_io = DI.socket_io
        if not socket_io:
            logger.error("Cannot emit %s: DI.socket_io is not registered", event)
            return
        with self.socket_lock:
            socket_io.emit(event, payload, room=socket_id)

    def _emit_via_twilio_sms(self, *, body: str, to_number: str, from_number: str):
        try:
            from app.services.twilio_sms import TwilioSmsService
            TwilioSmsService().send_sms(to_number=to_number, from_number=from_number, body=body)
        except Exception as e:
            logger.error("Failed to send Twilio SMS: %s", e, exc_info=True)

    def _emit_via_slack(self, *, body: str, channel_id: str, thread_ts: str = ""):
        try:
            from app.services.slack import SlackService
            SlackService().send_message(
                channel_id=channel_id, text=body, thread_ts=thread_ts or None,
            )
        except Exception as e:
            logger.error("Failed to deliver Slack message to channel=%s: %s", channel_id, e)
            logger.debug("slack send exception details", exc_info=True)

    def _emit_via_telegram(self, *, body: str, chat_id: str):
        try:
            from app.services.telegram_bot import TelegramBotService
            TelegramBotService().send_message(chat_id=chat_id, text=body)
        except Exception as e:
            logger.error("Failed to send Telegram message: %s", e, exc_info=True)

    def _emit_message(self, message, payload, preferred_reply_to=None):
        """Handles actual emission of messages based on reply_to transport."""
        reply_to = self._resolve_reply_to(message, preferred_reply_to=preferred_reply_to)
        if not isinstance(reply_to, dict):
            logger.error("Cannot emit user_message_data: no reply_to resolved for message sender=%r", getattr(message, "sender", None))
            return
        rtype = (reply_to.get("type") or "").strip().lower()

        data = {
            "chat": payload.chat,
            "feed": payload.feed,
            "widget_data": payload.widget_data,
            "sound": payload.sound,
        }
        # Forward sub_data_type so the frontend can style proactive messages differently.
        sub_data_type = getattr(message, "sub_data_type", None)
        if isinstance(sub_data_type, list) and sub_data_type:
            data["sub_data_type"] = sub_data_type

        if rtype == "twilio_sms":
            text = (payload.chat or "").strip()
            if not text:
                return
            to_number = (reply_to.get("to") or "").strip()
            from_number = (reply_to.get("from") or "").strip()
            if not to_number or not from_number:
                logger.error("Twilio reply_to missing to/from; cannot send SMS.")
                return
            self._emit_via_twilio_sms(body=text, to_number=to_number, from_number=from_number)
            return

        if rtype == "telegram":
            text = (payload.chat or "").strip()
            if not text:
                return
            chat_id = str(reply_to.get("chat_id") or "").strip()
            if not chat_id:
                logger.error("Telegram reply_to missing chat_id; cannot send message.")
                return
            self._emit_via_telegram(body=text, chat_id=chat_id)
            return

        if rtype == "slack":
            text = (payload.chat or "").strip()
            if not text:
                return
            channel_id = str(reply_to.get("channel_id") or "").strip()
            if not channel_id:
                logger.error("Slack reply_to missing channel_id; cannot send message.")
                return
            thread_ts = str(reply_to.get("thread_ts") or "").strip()
            self._emit_via_slack(body=text, channel_id=channel_id, thread_ts=thread_ts)
            return

        if rtype == "socketio":
            self._emit_via_socketio(event="user_message_data", payload=data, reply_to=reply_to)
            return

        logger.error("Cannot emit user_message_data: unknown reply_to type=%r reply_to=%r", rtype, reply_to)

    def _process_tts(self, text: str, message, preferred_reply_to=None):
        """Handles text-to-speech processing (socketio only)."""
        from app.services.socket_manager import RoomNotBound
        reply_to = self._resolve_reply_to(message, preferred_reply_to=preferred_reply_to)
        if not isinstance(reply_to, dict):
            logger.error("[TTS:relay] _process_tts: no reply_to resolved; cannot deliver audio.")
            return
        rtype = (reply_to.get("type") or "").strip().lower()
        if rtype != "socketio":
            logger.warning("[TTS:relay] _process_tts skipping — rtype=%r not socketio", rtype)
            return
        room_id = str(reply_to.get("room_id") or "").strip()
        if not room_id:
            logger.error("[TTS:relay] _process_tts: reply_to missing room_id; cannot deliver audio.")
            return
        try:
            socket_id = DI.socket_manager.resolve_socket(room_id)
        except RoomNotBound:
            logger.error("[TTS:relay] _process_tts: no live socket for room_id=%r", room_id)
            return
        socket_io = DI.socket_io
        if not socket_io:
            logger.error("[TTS:relay] _process_tts: DI.socket_io not registered")
            return
        logger.info("[TTS:relay] calling text_to_speech.process_text room_id=%r socket=%s", room_id, socket_id[:8])
        try:
            text_to_speech.process_text(text, socket_id, socket_io)
            logger.info("[TTS:relay] text_to_speech.process_text completed for room_id=%r", room_id)
        except Exception as e:
            logger.error("[TTS:relay] Failed to generate TTS audio: %s", e, exc_info=True)
            self._emit_error('audio_file_error', "Failed to generate TTS audio.", str(e))

    def _emit_error(self, event, message, details=""):
        """Emit error messages to WebSocket (targets master_room as the global chat surface)."""
        error_data = {"error": message, "details": details}
        self._emit_to_room("master_room", event, error_data)
