# app/services/socket_manager.py
import threading
import time
from datetime import datetime, timezone

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class RoomNotBound(Exception):
    """Raised when resolve_socket is called for a room_id with no current binding."""


class SocketManager:
    """
    Owns the live mapping between EmiAi logical room_ids and physical socket.io socket_ids.

    Invariant: every room_id maps to at most one socket_id, and every socket_id maps
    to at most one room_id. Bind overwrites any stale binding on either side.

    Liveness: last_seen_utc is tracked per socket. Clients send heartbeats; the
    sweeper thread evicts bindings whose last_seen is older than max_age_s.
    This catches zombie sockets (silent TCP death, e.g. Cloudflare tunnel drops)
    that never fire a Socket.IO disconnect event.
    """

    # Window (seconds) over which to count repeat binds per room_id. A rebind
    # is expected when a client reconnects (Flask restart, Cloudflare drop),
    # but a storm of them within this window means something is flapping.
    _BIND_CHURN_WINDOW_S: float = 60.0
    # Threshold: log WARNING when a single room_id exceeds this many binds
    # within the window. Tuned so a normal reconnect-storm (<=3 rebinds) is
    # quiet but a flapping tunnel (10+ rebinds/min) is loud.
    _BIND_CHURN_WARN_THRESHOLD: int = 5

    def __init__(self):
        self._rooms: dict[str, str] = {}
        self._sockets: dict[str, str] = {}
        self._last_seen: dict[str, datetime] = {}
        self._lock = threading.RLock()
        self._sweeper_thread: threading.Thread | None = None
        self._sweeper_stop = threading.Event()
        # Rolling list of bind timestamps per room_id for churn detection.
        self._bind_history: dict[str, list[float]] = {}

    @staticmethod
    def _now_utc() -> datetime:
        return datetime.now(timezone.utc)

    def bind(self, room_id: str, socket_id: str) -> str | None:
        """Bind room_id -> socket_id. Overwrites any prior binding on either key.

        Returns the prior socket_id that was displaced from ``room_id``, or
        None if there was none or it was already the same socket. The caller
        can use the returned socket_id to notify the old client (e.g. emit
        "socket_hijacked" and force-disconnect) so the user gets a signal
        that their tab is no longer the active one, rather than silently
        having messages disappear.
        """
        if not isinstance(room_id, str) or not room_id.strip():
            raise ValueError(f"room_id must be a non-empty string, got {room_id!r}")
        if not isinstance(socket_id, str) or not socket_id.strip():
            raise ValueError(f"socket_id must be a non-empty string, got {socket_id!r}")
        room_id = room_id.strip()
        socket_id = socket_id.strip()
        displaced_socket_id: str | None = None
        with self._lock:
            prior_socket = self._rooms.get(room_id)
            if prior_socket and prior_socket != socket_id:
                self._sockets.pop(prior_socket, None)
                self._last_seen.pop(prior_socket, None)
                displaced_socket_id = prior_socket
            prior_room = self._sockets.get(socket_id)
            if prior_room and prior_room != room_id:
                self._rooms.pop(prior_room, None)
            self._rooms[room_id] = socket_id
            self._sockets[socket_id] = room_id
            self._last_seen[socket_id] = self._now_utc()
        if displaced_socket_id:
            logger.warning(
                "🔌 bind room_id=%r socket_id=%s... displaced prior socket=%s... "
                "(likely a second tab opened; old tab will be notified).",
                room_id, socket_id[:8], displaced_socket_id[:8],
            )
        else:
            logger.debug("🔌 bind room_id=%r socket_id=%s...", room_id, socket_id[:8])

        # Bind-churn detection: if a single room_id rebinds many times in a
        # short window, the client is almost certainly reconnecting in a loop
        # (Cloudflare tunnel flapping, bad network, or a buggy client). Log
        # a WARNING with the count so an operator can spot it without
        # grepping every bind event.
        now_ts = time.time()
        with self._lock:
            history = self._bind_history.setdefault(room_id, [])
            cutoff = now_ts - self._BIND_CHURN_WINDOW_S
            history[:] = [t for t in history if t >= cutoff]
            history.append(now_ts)
            count = len(history)
            # Evict cold room entries from the history map so it doesn't grow
            # with every unique room_id ever seen.
            if len(self._bind_history) > 256:
                stale = [rid for rid, h in self._bind_history.items() if not h or h[-1] < cutoff]
                for rid in stale:
                    self._bind_history.pop(rid, None)
        if count >= self._BIND_CHURN_WARN_THRESHOLD:
            logger.warning(
                "🔌 bind churn: room_id=%r rebound %d times in last %.0fs — client may be flapping.",
                room_id, count, self._BIND_CHURN_WINDOW_S,
            )
        return displaced_socket_id

    def unbind(self, room_id: str) -> None:
        """Remove binding by room_id. No-op if not bound."""
        if not isinstance(room_id, str) or not room_id.strip():
            return
        room_id = room_id.strip()
        with self._lock:
            socket_id = self._rooms.pop(room_id, None)
            if socket_id:
                self._sockets.pop(socket_id, None)
                self._last_seen.pop(socket_id, None)
        if socket_id:
            logger.debug(f"🔌 unbind room_id={room_id!r} socket_id={socket_id[:8]}...")

    def unbind_by_socket_id(self, socket_id: str) -> None:
        """Remove binding by socket_id. Called on disconnect. No-op if not bound."""
        if not isinstance(socket_id, str) or not socket_id.strip():
            return
        socket_id = socket_id.strip()
        with self._lock:
            room_id = self._sockets.pop(socket_id, None)
            self._last_seen.pop(socket_id, None)
            if room_id:
                self._rooms.pop(room_id, None)
        if room_id:
            logger.debug(f"🔌 unbind_by_socket_id socket_id={socket_id[:8]}... room_id={room_id!r}")

    def resolve_socket(self, room_id: str) -> str:
        """
        Return the current live socket_id for room_id.

        Raises RoomNotBound if no binding exists. No fallback — the caller must
        decide whether to log and skip, or propagate.

        NOTE: if you are about to emit to this socket_id, prefer
        ``emit_to_room(room_id, emit_fn)`` which holds the lock across the
        lookup and the emit call. Calling resolve_socket and then emitting
        separately creates a race window where the sweeper can evict the
        binding between the two calls and the emit silently drops.
        """
        if not isinstance(room_id, str) or not room_id.strip():
            raise RoomNotBound(f"room_id must be a non-empty string, got {room_id!r}")
        room_id = room_id.strip()
        with self._lock:
            socket_id = self._rooms.get(room_id)
        if not socket_id:
            raise RoomNotBound(f"No live socket bound for room_id={room_id!r}")
        return socket_id

    def emit_to_room(self, room_id: str, emit_fn) -> None:
        """Atomically resolve room_id and call ``emit_fn(socket_id)`` under the lock.

        The lock is held for the entire lookup + emit sequence, so the sweeper
        cannot evict the binding between the two steps. ``emit_fn`` should be
        a short synchronous call (e.g. ``socketio.emit(...)``) — do NOT pass a
        long-running callable since the lock is held while it runs.

        Raises RoomNotBound if no binding exists. The lock is RLock, so
        emit_fn is free to call back into the SocketManager if needed.
        """
        if not isinstance(room_id, str) or not room_id.strip():
            raise RoomNotBound(f"room_id must be a non-empty string, got {room_id!r}")
        room_id = room_id.strip()
        with self._lock:
            socket_id = self._rooms.get(room_id)
            if not socket_id:
                raise RoomNotBound(f"No live socket bound for room_id={room_id!r}")
            emit_fn(socket_id)

    def is_bound(self, room_id: str) -> bool:
        """True iff room_id currently has a live binding."""
        if not isinstance(room_id, str) or not room_id.strip():
            return False
        with self._lock:
            return room_id.strip() in self._rooms

    def snapshot(self) -> dict[str, str]:
        """Return a copy of the current {room_id: socket_id} map. For diagnostics."""
        with self._lock:
            return dict(self._rooms)

    # ------------------------------------------------------------------
    # Liveness
    # ------------------------------------------------------------------

    def record_heartbeat(self, socket_id: str) -> None:
        """Update last_seen_utc for a socket. Called from the heartbeat handler."""
        if not isinstance(socket_id, str) or not socket_id.strip():
            return
        socket_id = socket_id.strip()
        with self._lock:
            if socket_id in self._sockets:
                self._last_seen[socket_id] = self._now_utc()

    def sweep_stale(self, max_age_seconds: float) -> list[str]:
        """
        Unbind any socket whose last_seen is older than max_age_seconds.
        Returns the list of evicted (socket_id, room_id) pairs as a sanity log.
        """
        cutoff = self._now_utc().timestamp() - float(max_age_seconds)
        evicted: list[tuple[str, str]] = []
        with self._lock:
            stale_socket_ids = [
                sid for sid, ts in self._last_seen.items()
                if ts.timestamp() < cutoff
            ]
            for sid in stale_socket_ids:
                rid = self._sockets.pop(sid, None)
                self._last_seen.pop(sid, None)
                if rid:
                    self._rooms.pop(rid, None)
                    evicted.append((sid, rid))
        for sid, rid in evicted:
            logger.warning(
                "🔌 sweep: evicting stale socket_id=%s... room_id=%r (no heartbeat > %.0fs)",
                sid[:8], rid, max_age_seconds,
            )
        return [rid for _sid, rid in evicted]

    def start_stale_sweeper(self, *, interval_s: float = 60.0, max_age_s: float = 90.0) -> None:
        """
        Start a daemon thread that periodically evicts sockets with no recent
        heartbeat. Idempotent — safe to call multiple times; only the first
        call starts the thread.
        """
        with self._lock:
            if self._sweeper_thread and self._sweeper_thread.is_alive():
                return

            def _loop():
                while not self._sweeper_stop.wait(interval_s):
                    try:
                        self.sweep_stale(max_age_s)
                    except Exception as e:
                        logger.error("socket_manager sweeper tick failed: %s", e, exc_info=True)

            self._sweeper_stop.clear()
            t = threading.Thread(target=_loop, name="socket-manager-sweeper", daemon=True)
            t.start()
            self._sweeper_thread = t
        logger.info("socket_manager: stale-socket sweeper started (interval=%.1fs max_age=%.1fs)", interval_s, max_age_s)

    def stop_stale_sweeper(self) -> None:
        """Request the sweeper thread to stop. Used by tests."""
        self._sweeper_stop.set()
