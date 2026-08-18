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
        DI.event_hub.register_event('agent_progress_emit', self.agent_progress_emit_handler)

    def socket_emit_handler(self, message: UserMessage):
        """Queue messages for WebSocket emission, processing TTS if needed."""
        # System alerts publish a plain Message carrying {"event", "payload"}
        # instead of a UserMessage — llm_client._check_and_trip_quota does this
        # for the "quota exhausted, shutting down" notice. Those have no
        # user_message_data and no TTS, so emit them straight to the room.
        # Without this branch the handler raised AttributeError inside the
        # event hub and the shutdown notice never reached the UI: the process
        # exited and the user saw the app vanish with no explanation.
        payload = getattr(message, "user_message_data", None)
        if payload is None:
            data = getattr(message, "data", None)
            if isinstance(data, dict) and data.get("event"):
                self._emit_to_room("master_room", data["event"], data.get("payload", {}))
            else:
                logger.warning(
                    "socket_emit_handler: no user_message_data and no usable "
                    "data payload (sender=%s) — dropping",
                    getattr(message, "sender", "?"),
                )
            return

        preferred_reply_to = None
        try:
            meta = getattr(message, "metadata", None)
            if isinstance(meta, dict):
                preferred_reply_to = meta.get("reply_to")
        except Exception:
            preferred_reply_to = None

        # Resolve ONCE here and hand the resolved destination to both the
        # TTS job and the queued emit. Each used to resolve independently,
        # so a message with no pinned destination logged the defaulted-to-
        # master_room WARN twice per message and could race the reply_router
        # TTL to two different answers.
        reply_to = self._resolve_reply_to(message, preferred_reply_to=preferred_reply_to)

        logger.info(
            "[TTS:relay] socket_emit_handler sender=%s payload.tts=%s reply_to=%r",
            getattr(message, "sender", "?"),
            getattr(payload, "tts", "?"),
            reply_to,
        )

        if payload.tts:
            if payload.tts_text:
                logger.info("[TTS:relay] Submitting TTS job. text_len=%d", len(payload.tts_text))
                self.tts_executor.submit(self._process_tts, payload.tts_text, message, reply_to)
            else:
                logger.warning("[TTS:relay] TTS flag set but 'tts_text' is missing.")
                self._emit_error('audio_file_error', "Missing 'tts_text' in payload.")

        self.message_queue.put((message, payload, reply_to))

    def notify_ui_of_repo_update(self, msg: Message):
        """Notifies the UI about repository updates via WebSocket."""
        logger.debug("notify_ui_of_repo_update: sending repo update notification")
        self._emit_to_room("master_room", "repo_update_notification", msg.data)

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
        """Resolve delivery destination, in priority order: preferred_reply_to ->
        per-message metadata.reply_to -> reply_router(request_id) -> owner UI
        default (master_room).

        The first three honor a destination pinned at ingress (a room that sent a
        request gets its reply back — socketio/twilio_sms/slack/telegram). A message
        that reaches here with NO pinned destination is, by definition, a system /
        autonomous owner notification (reminders, dayflow tickets, situation-audit);
        historically these just emitted to the user UI, which is now the logical
        `master_room`, so default there rather than dropping. master_room is a logical
        room — socket_manager resolves it to the live/active browser socket and
        re-points on a browser switch — so it follows whatever surface the owner is on.
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

        # No destination pinned -> owner UI default. WARN (not silent) so a sender that
        # should have pinned one stays visible, but DELIVER instead of dropping.
        logger.warning(
            "reply_to defaulted to master_room for sender=%r — none pinned at ingress",
            getattr(message, "sender", None),
        )
        return {"type": "socketio", "room_id": "master_room"}

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
            # stack=True reveals the upstream caller that constructed the
            # invalid reply_to. Without it the error is unactionable — every
            # bad reply_to looks the same and you can't tell which control
            # node / pipeline / agent built it.
            logger.error(
                "Cannot emit %s: reply_to missing room_id. reply_to=%r",
                event, reply_to, stack_info=True,
            )
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

    def _emit_message(self, message, payload, preferred_reply_to=None):
        """Handles actual emission of messages based on reply_to transport.

        Socketio is emitted here (the relay IS the socket terminal — routing
        this through OutboundChatPublisher's socketio path would publish
        socket_emit right back to this relay). Every other surface delegates
        to OutboundChatPublisher, the single surface dispatcher: one set of
        transport branches, and Slack rides SlackRoomTransport/SlackTool with
        its allow_real_slack_send safety gate (the old inline branch called
        SlackService directly, bypassing the gate). embed_sender=False —
        these are the assistant's own replies, not narrator/worker lines.
        """
        reply_to = self._resolve_reply_to(message, preferred_reply_to=preferred_reply_to)
        if not isinstance(reply_to, dict):
            logger.error("Cannot emit user_message_data: no reply_to resolved for message sender=%r", getattr(message, "sender", None))
            return
        rtype = (reply_to.get("type") or "").strip().lower()

        if rtype == "socketio":
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
            self._emit_via_socketio(event="user_message_data", payload=data, reply_to=reply_to)
            return

        if rtype in ("twilio_sms", "telegram", "slack"):
            text = (payload.chat or "").strip()
            if not text:
                return
            DI.outbound_chat_publisher.publish(
                sender=str(getattr(message, "sender", "") or ""),
                text=text,
                reply_to=reply_to,
                embed_sender=False,
            )
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
