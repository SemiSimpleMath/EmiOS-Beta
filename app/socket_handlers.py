# app/socket_handlers.py
from datetime import datetime, timedelta, timezone
from flask import request, current_app
from app.assistant.utils.logging_config import get_logger
import threading

logger = get_logger(__name__)


# Max number of missed messages we'll replay on reconnect. Protects against
# flooding a client that was away for a very long time (just load /api/chat/history
# in that case for a full restore).
_REPLAY_CAP = 50


def _extract_room_id(data, default_room_id: str) -> str:
    """Pull room_id from a registration payload; fall back to the default channel name."""
    if isinstance(data, dict):
        room_id = data.get("room_id")
        if isinstance(room_id, str) and room_id.strip():
            return room_id.strip()
    return default_room_id


def _parse_since_ts(data) -> datetime | None:
    """Extract and parse the optional since_ts from a client payload."""
    if not isinstance(data, dict):
        return None
    raw = data.get("since_ts")
    if not raw:
        return None
    try:
        # JS sends ISO strings. Accept with or without trailing 'Z'.
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(float(raw) / 1000.0 if raw > 1e12 else float(raw), tz=timezone.utc)
        s = str(raw).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        logger.debug("Could not parse since_ts=%r", raw, exc_info=True)
        return None


def _replay_missed_to_socket(*, socketio, socket_id: str, room_id: str, since_ts: datetime | None) -> int:
    """
    After a fresh bind, emit any assistant messages for this room_id that were
    written to unified_log since since_ts. Returns count of messages replayed.

    If since_ts is None (first load), we don't replay — the client's page-load
    path already calls /api/chat/history to restore history cleanly. Replay is
    specifically for hot reconnects where the client remembers where it was.
    """
    if since_ts is None:
        return 0
    try:
        DI = current_app.DI
        blackboard = DI.global_blackboard
        # Add a small epsilon so we don't replay the boundary message itself.
        cutoff = since_ts + timedelta(milliseconds=1)
        msgs = blackboard.get_recent_chat_since_utc(cutoff_utc=cutoff, room_id=room_id)
    except Exception as e:
        logger.warning("Replay: failed to query recent chat for room_id=%r: %s", room_id, e)
        return 0

    replayed = 0
    for msg in (msgs or []):
        if replayed >= _REPLAY_CAP:
            break
        role = getattr(msg, "role", None)
        content = getattr(msg, "content", None)
        if role != "assistant" or not isinstance(content, str) or not content.strip():
            continue
        try:
            socketio.emit(
                "user_message_data",
                {
                    "chat": content,
                    "feed": None,
                    "widget_data": [],
                    "sound": None,
                },
                room=socket_id,
            )
            replayed += 1
        except Exception as e:
            logger.warning("Replay: failed to emit missed message: %s", e)
            break

    if replayed > 0:
        logger.info(
            "🔁 Replayed %d missed message(s) to room_id=%r socket=%s... since %s",
            replayed, room_id, socket_id[:8], since_ts.isoformat(),
        )
    return replayed


def register_socket_handlers(socketio):
    @socketio.on('connect')
    def socket_connection_handler():
        from app.assistant.utils import thread_debug
        logger.info(f"Active threads count: {threading.active_count()}")
        #thread_debug.print_thread_info()
        # Don't auto-register here - let clients identify themselves

    @socketio.on('disconnect')
    def socket_disconnection_handler():
        try:
            socket_id = request.sid
            if not socket_id:
                return
            DI = current_app.DI
            DI.socket_manager.unbind_by_socket_id(socket_id)
        except Exception as e:
            logger.error("Error handling socket disconnect: %s", e)
            logger.debug("socket disconnect exception details", exc_info=True)

    @socketio.on('register_chat_client')
    def handle_chat_registration(data):
        """
        Chat tab registers itself. Payload: {room_id: <emi_room_id>, since_ts?: <iso>}.
        If since_ts is present, this is a hot reconnect — replay any assistant
        messages for this room_id written to unified_log since that timestamp so
        the UI catches up without the user having to refresh.

        If a different socket was already bound to this room_id (e.g. user
        opened a second tab), notify the displaced socket so its UI knows it
        lost the binding instead of silently having messages disappear.
        """
        try:
            socket_id = request.sid
            if not socket_id:
                logger.error("Missing socket_id during chat registration.")
                return
            room_id = _extract_room_id(data, default_room_id="chat")
            if room_id == "chat":
                logger.warning(
                    "💬 register_chat_client fell back to room_id='chat' — client did not send "
                    "room_id in payload. Server emits target 'master_room', so this binding "
                    "receives nothing. Update the client to send {room_id: 'master_room'}."
                )
            DI = current_app.DI
            displaced = DI.socket_manager.bind(room_id, socket_id)
            logger.info(f"💬 Chat client registered: room_id={room_id!r} socket={socket_id[:8]}...")

            if displaced:
                try:
                    socketio.emit(
                        "socket_hijacked",
                        {
                            "room_id": room_id,
                            "reason": "another_tab_connected",
                            "message": "This tab's connection was taken over by a newer tab.",
                        },
                        room=displaced,
                    )
                    socketio.disconnect(displaced)
                    logger.info(
                        "💬 Notified displaced socket=%s... on room_id=%r",
                        displaced[:8], room_id,
                    )
                except Exception as e:
                    logger.warning(
                        "Failed notifying displaced socket=%s... room_id=%r: %s",
                        displaced[:8], room_id, e,
                    )

            since_ts = _parse_since_ts(data)
            if since_ts is not None:
                _replay_missed_to_socket(
                    socketio=socketio,
                    socket_id=socket_id,
                    room_id=room_id,
                    since_ts=since_ts,
                )
        except Exception as e:
            logger.error("Error registering chat client: %s", e)
            logger.debug("error registering chat client exception details", exc_info=True)

    @socketio.on('heartbeat')
    def handle_heartbeat(data):
        """
        Client-initiated liveness ping. Updates last_seen for the socket and
        echoes an ack back. Clients that don't receive an ack within their
        timeout force a reconnect on their end.
        """
        try:
            socket_id = request.sid
            if not socket_id:
                return
            DI = current_app.DI
            DI.socket_manager.record_heartbeat(socket_id)
            # Ack with the server's wall clock (useful for clock-skew debugging).
            socketio.emit(
                "heartbeat_ack",
                {"server_ts": datetime.now(timezone.utc).isoformat()},
                room=socket_id,
            )
        except Exception as e:
            logger.error("Error handling heartbeat: %s", e)
            logger.debug("heartbeat exception details", exc_info=True)

    @socketio.on('register_music_client')
    def handle_music_registration(data):
        """Music tab registers itself. Payload: {room_id: <emi_room_id>}. Falls back to 'music'."""
        try:
            socket_id = request.sid
            if not socket_id:
                logger.error("Missing socket_id during music registration.")
                return
            room_id = _extract_room_id(data, default_room_id="music")
            DI = current_app.DI
            DI.socket_manager.bind(room_id, socket_id)
            logger.info(f"🎵 Music client registered: room_id={room_id!r} socket={socket_id[:8]}...")
        except Exception as e:
            logger.error("Error registering music client: %s", e)
            logger.debug("error registering music client exception details", exc_info=True)

    @socketio.on('register_progress_client')
    def handle_progress_registration(data):
        """Progress tab registers itself. Payload: {room_id: <emi_room_id>}. Falls back to 'progress'."""
        try:
            socket_id = request.sid
            if not socket_id:
                logger.error("Missing socket_id during progress registration.")
                return
            room_id = _extract_room_id(data, default_room_id="progress")
            DI = current_app.DI
            DI.socket_manager.bind(room_id, socket_id)
            logger.info(f"🧭 Progress client registered: room_id={room_id!r} socket={socket_id[:8]}...")
        except Exception as e:
            logger.error("Error registering progress client: %s", e)
            logger.debug("error registering progress client exception details", exc_info=True)
    
    # Music state update handler
    @socketio.on('music_state_update')
    def handle_music_state_update(data):
        """Handle playback state updates from the music player frontend."""
        try:
            from app.assistant.music_manager import get_music_manager
            from app.assistant.dj_manager import get_dj_manager
            
            # Update music manager state
            music_manager = get_music_manager()
            music_manager.update_playback_state(data)
            
            # Notify DJ of track change (for logging/stats)
            #
            # IMPORTANT: We intentionally do NOT let backend auto-queue based on
            # music_state_update. The frontend owns queue timing and requests picks.
            # This avoids double-queueing and excessive Apple API traffic.
            dj_manager = get_dj_manager()
            current_track = data.get("current_track")
            dj_manager.on_track_changed(current_track)
            
        except Exception as e:
            logger.error("Error handling music state update: %s", e)
            logger.debug("error handling music state update exception details", exc_info=True)
    
    # Music song queued notification
    @socketio.on('music_song_queued')
    def handle_song_queued(data):
        """Handle notification that a song was queued."""
        try:
            from app.assistant.dj_manager import get_dj_manager
            
            dj_manager = get_dj_manager()
            dj_manager.on_frontend_queued(data)
            
        except Exception as e:
            logger.error("Error handling song queued: %s", e)
            logger.debug("error handling song queued exception details", exc_info=True)
    
    # Frontend requests DJ to pick a song
    @socketio.on('music_pick_request')
    def handle_pick_request(data):
        """Handle request from frontend to pick a new song."""
        try:
            from app.assistant.dj_manager import get_dj_manager
            
            dj_manager = get_dj_manager()

            # Log full context for debugging "thinking but nothing happens".
            try:
                logger.info(
                    "🎵 Pick request event: enabled=%s continuous=%s pick_in_progress=%s data=%s",
                    dj_manager.is_enabled(),
                    dj_manager.is_continuous_mode(),
                    dj_manager.is_pick_in_progress(),
                    data,
                )
            except Exception as e:
                logger.debug("Failed to log pick request context: %s", e, exc_info=True)

            # If a pick is already in progress, skip
            if dj_manager.is_pick_in_progress():
                logger.debug("🎵 Pick request ignored - already picking")
                return

            queue_length = data.get('queue_length', 0)
            logger.info(f"🎵 Pick request received (queue: {queue_length})")
            
            # Enqueue pick request (DJ thread handles it).
            #
            # IMPORTANT: Do NOT drop requests just because enable/continuous mode
            # hasn't been applied yet on the DJ thread; the enable event and this
            # request are queued FIFO and will self-resolve.
            dj_manager.request_pick_and_queue(reason="frontend_request")
            
        except Exception as e:
            logger.error("Error handling pick request: %s", e)
            logger.debug("error handling pick request exception details", exc_info=True)
    
    # Frontend requests backup song (when primary wasn't found)
    @socketio.on('music_backup_request')
    def handle_backup_request(data):
        """Handle request for a backup song when primary wasn't found."""
        try:
            from app.assistant.dj_manager import get_dj_manager
            from app.assistant.dj_manager.query_utils import build_search_query
            
            dj_manager = get_dj_manager()
            failed_query = data.get('failed_query', 'unknown')
            logger.info(f"🎵 Backup request received (failed: {failed_query})")
            
            # Thread-safe: routed through DJ event queue internally.
            backup = dj_manager.get_backup_song(timeout_seconds=30)
            if backup:
                title = (backup.get("title") or "").strip()
                artist = (backup.get("artist") or "").strip()
                search_query = (backup.get("search_query") or build_search_query(title, artist)).strip()
                logger.info(f"🎵 Sending backup: {search_query}")
                
                # Send backup song to frontend
                from app.services.socket_manager import RoomNotBound
                DI = current_app.DI
                try:
                    socket_id = DI.socket_manager.resolve_socket("music")
                except RoomNotBound:
                    logger.warning("🎵 No music client connected; backup song not delivered")
                    socket_id = None
                socket_io = DI.socket_io
                if socket_id and socket_io:
                    socket_io.emit("music_command", {
                        "command": "queue_next",
                        "payload": {
                            "query": search_query,
                            "is_backup": True,
                            "dataset": {
                                "title": title,
                                "artist": artist,
                                "genre": backup.get("genre"),
                                "sliders": backup.get("sliders"),
                                "prob_factor": backup.get("prob_factor"),
                            },
                        }
                    }, room=socket_id)
            else:
                logger.warning("🎵 No backups available, frontend will need to request new pick")
            
        except Exception as e:
            logger.error("Error handling backup request: %s", e)
            logger.debug("error handling backup request exception details", exc_info=True)
