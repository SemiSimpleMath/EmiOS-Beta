from __future__ import annotations

import queue
import threading
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.ServiceLocator.service_locator import DI

from app.assistant.dj_manager.events import (
    DJEvent,
    Enable,
    Disable,
    SetContinuousMode,
    SetPauseOnAfk,
    TrackChanged,
    RequestPickAndQueue,
    PickSong,
    RequestBackupSong,
    FrontendQueued,
    StopThread,
)
from app.assistant.dj_manager.socket_client import MusicSocketClient
from app.assistant.dj_manager.vibe import VibePlanner
from app.assistant.dj_manager.selector import CandidateSelector
from app.assistant.dj_manager.query_utils import parse_search_query, build_search_query
from app.assistant.dj_manager.scope import build_dj_scope_context
from app.assistant.runtime import start_monitored_thread

logger = get_logger(__name__)

PICK_DEBOUNCE_SECONDS = 5
QUEUE_RETRY_COOLDOWN_SECONDS = 20

PICK_SONG_TIMEOUT_SECONDS = 90
BACKUP_SONG_TIMEOUT_SECONDS = 30


class DJManager:
    """
    Thread-owned state machine:
    - External callers enqueue events
    - Only the DJ thread mutates internal state
    """

    def __init__(
            self,
            socket_client: Optional[MusicSocketClient] = None,
            vibe: Optional[VibePlanner] = None,
            selector: Optional[CandidateSelector] = None,
            app=None,
    ):
        self._app = app
        self._q: "queue.Queue[DJEvent]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._running = False

        self._socket = socket_client or MusicSocketClient()
        self._vibe = vibe or VibePlanner()
        self._selector = selector or CandidateSelector()

        # Restore the persisted toggle so a restart doesn't silently turn the DJ
        # off (it did, for ~3 months). If restored ON, start() below is called by
        # bootstrap; the loop then resumes continuous picking on its own.
        try:
            from app.assistant.dj_manager.dj_state_store import load_dj_state
            _st = load_dj_state()
        except Exception:
            _st = {"enabled": False, "continuous_mode": False}
        self._enabled = bool(_st.get("enabled", False))
        self._continuous_mode = bool(_st.get("continuous_mode", False))

        # Legacy fields kept for status compatibility only.
        # AFK pause/resume policy is now handled by the frontend (music_afk_state).
        self._pause_on_afk = True
        self._paused_by_dj = False

        self._next_song_queued = False
        self._pick_in_progress = False
        self._last_pick_utc: Optional[datetime] = None
        self._queue_retry_after_utc: Optional[datetime] = None

        self._current_track_id: Optional[str] = None

        self._stats = {
            "started_at": None,
            "last_action": None,
            "last_action_time": None,
        }

    # Public API, safe from any thread

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stats["started_at"] = datetime.now(timezone.utc).isoformat()

        self._thread = start_monitored_thread(
            owner="dj_manager",
            name="dj-manager",
            target=self._loop,
            daemon=True,
            kind="service_loop",
            metadata={"component": "dj_manager"},
        )
        logger.info("DJ Manager thread started")

    def stop(self) -> None:
        if not self._running:
            return
        self.enqueue(StopThread())
        if self._thread:
            self._thread.join(timeout=2)
        self._running = False
        logger.info("DJ Manager thread stopped")

    def enqueue(self, event: DJEvent) -> None:
        self._q.put(event)

    def enable(self) -> None:
        self.start()
        self.enqueue(Enable(continuous_mode=True))

    def disable(self) -> None:
        self.enqueue(Disable())

    def set_continuous_mode(self, enabled: bool) -> None:
        self.enqueue(SetContinuousMode(enabled=enabled))

    def set_pause_on_afk(self, enabled: bool) -> None:
        # AFK pause/resume policy is handled in the frontend now (via music_afk_state socket event).
        # Keep this method for API compatibility, but it is intentionally a no-op.
        return

    def on_track_changed(self, track: Optional[Dict[str, Any]]) -> None:
        self.enqueue(TrackChanged(track=track))

    def request_pick_and_queue(self, reason: str = "manual") -> None:
        self.enqueue(RequestPickAndQueue(reason=reason))

    def pick_song(self, reason: str = "manual", timeout_seconds: int = PICK_SONG_TIMEOUT_SECONDS) -> Optional[Dict[str, Any]]:
        """
        Pick a song (selection only) on the DJ thread and return the pick result.

        This is intentionally separated from playback. Call `play_song()` with the returned
        `search_query` afterwards.

        Note: this method requires DJ mode to be enabled; use `pick_song_once()` for a one-shot
        pick when DJ mode is disabled.
        """
        if not self._enabled:
            return None

        # Route selection through the DJ thread (thread-owned state).
        reply_q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=1)
        self.enqueue(PickSong(reason=reason, reply_queue=reply_q, allow_when_disabled=False))

        try:
            return reply_q.get(timeout=max(1, int(timeout_seconds)))
        except Exception:
            return None

    def pick_song_once(self, reason: str = "manual_once", timeout_seconds: int = PICK_SONG_TIMEOUT_SECONDS) -> Optional[Dict[str, Any]]:
        """
        Pick a song even when DJ mode is disabled (one-shot, no continuous mode required).

        This does NOT toggle enable/disable state; it only routes a pick through the DJ thread.
        """
        self.start()

        reply_q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=1)
        self.enqueue(PickSong(reason=reason, reply_queue=reply_q, allow_when_disabled=True))

        try:
            return reply_q.get(timeout=max(1, int(timeout_seconds)))
        except Exception:
            return None

    def on_frontend_queued(self, data: Dict[str, Any]) -> None:
        self.enqueue(FrontendQueued(data=data))

    def get_backup_song(self, timeout_seconds: int = BACKUP_SONG_TIMEOUT_SECONDS) -> Optional[Dict[str, Any]]:
        """
        Return a backup song in a thread-safe way.

        - If called on the DJ thread, pop directly.
        - Otherwise, route through the DJ event queue and wait for reply.
        """
        self.start()

        # If we are already on the DJ thread, execute directly.
        if self._thread is not None and threading.current_thread() is self._thread:
            return self._get_backup_song_internal()

        reply_q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=1)
        self.enqueue(RequestBackupSong(reply_queue=reply_q))

        try:
            return reply_q.get(timeout=max(1, int(timeout_seconds)))
        except Exception as e:
            logger.warning(f"Timed out waiting for backup song reply: {e}")
            logger.debug("Backup song reply wait exception details", exc_info=True)
            return None

    def get_status(self) -> Dict[str, Any]:
        """
        Snapshot status without locking.
        If you need strict consistency, route this through the DJ thread.
        """
        thread_alive = self._thread.is_alive() if self._thread else False
        return {
            "enabled": self._enabled,
            "running": self._running,
            "thread_alive": thread_alive,
            "paused_by_dj": self._paused_by_dj,
            "continuous_mode": self._continuous_mode,
            "pause_on_afk": self._pause_on_afk,
            "next_song_queued": self._next_song_queued,
            "pick_in_progress": self._pick_in_progress,
            "backup_candidates": self._selector.backup_count() if self._selector else 0,
            "vibe_plan": self._vibe.get_plan_debug(),
            "stats": dict(self._stats),
        }

    def is_enabled(self) -> bool:
        return self._enabled

    def is_continuous_mode(self) -> bool:
        return self._continuous_mode

    def is_pick_in_progress(self) -> bool:
        return self._pick_in_progress

    # Direct playback commands (pass-through to socket client)

    def play(self) -> bool:
        return self._socket.send_command("play")

    def pause(self) -> bool:
        return self._socket.send_command("pause")

    def next_track(self) -> bool:
        return self._socket.send_command("next")

    def previous_track(self) -> bool:
        return self._socket.send_command("previous")

    def search_and_play(self, query: str) -> bool:
        return self._socket.send_command("search_and_play", {"query": query})

    def set_volume(self, volume: float) -> bool:
        return self._socket.send_command("set_volume", {"volume": max(0.0, min(1.0, volume))})

    def queue_next(self, query: str) -> bool:
        return self._socket.send_command("queue_next", {"query": query})

    def play_song(self, search_query: str, *, mode: str = "queue_next", payload: Optional[Dict[str, Any]] = None) -> bool:
        """
        Play/queue a song by query on the frontend player.

        Modes:
        - "queue_next": queue to play next (or play immediately if nothing playing)
        - "search_and_play": immediate search+play
        """
        q = (search_query or "").strip()
        if not q:
            return False
        if mode == "queue_next":
            pl = {"query": q}
            if isinstance(payload, dict) and payload:
                pl.update(payload)
            return self._socket.send_command("queue_next", pl)
        if mode == "search_and_play":
            pl = {"query": q}
            if isinstance(payload, dict) and payload:
                pl.update(payload)
            return self._socket.send_command("search_and_play", pl)
        raise ValueError(f"Unknown play mode: {mode}")

    def record_pick(self, *, title: str, artist: str, targets: Dict[str, Any], search_query: Optional[str] = None) -> None:
        """Record the chosen song to DB (no extra side effects)."""
        try:
            from app.models.played_songs import record_song_play
        except Exception as e:
            logger.error("Could not import record_song_play: %s", e)
            logger.debug("could not import record_song_play exception details", exc_info=True)
            return

        try:
            t = (title or "").strip()
            a = (artist or "").strip() or "Unknown"
            if not t:
                return

            audio_targets = targets.get("audio_targets") if isinstance(targets, dict) else None
            q = (search_query or build_search_query(t, a)).strip()
            record_song_play(
                title=t,
                artist=a,
                search_query=q or None,
                audio_targets=audio_targets,
            )
            logger.info(f"Recorded at pick: {t} by {a}")
        except Exception as e:
            logger.error("Failed to record pick: %s", e)
            logger.debug("failed to record pick exception details", exc_info=True)

    # Internal loop

    def _loop(self) -> None:
        while True:
            try:
                event = self._q.get(timeout=0.25)
            except queue.Empty:
                continue

            if isinstance(event, StopThread):
                break

            try:
                self._handle_event(event)
            except Exception as e:
                logger.error("Error handling DJ event %s: %s", type(event).__name__, e)
                logger.debug("error handling DJ event exception details", exc_info=True)

    # Event handling

    def _persist_state(self) -> None:
        """Save enabled/continuous so a restart resumes where the user left off.
        Called only from the DJ thread (the sole mutator of these flags)."""
        try:
            from app.assistant.dj_manager.dj_state_store import save_dj_state
            save_dj_state(enabled=self._enabled, continuous_mode=self._continuous_mode)
        except Exception as e:
            logger.error("Failed to persist DJ state: %s", e)

    def _handle_event(self, event: DJEvent) -> None:
        if isinstance(event, Enable):
            self._enabled = True
            self._continuous_mode = bool(event.continuous_mode)
            self._next_song_queued = False
            self._selector.clear_backups()
            self._persist_state()
            logger.info(f"DJ enabled (continuous_mode={self._continuous_mode})")

        elif isinstance(event, Disable):
            self._enabled = False
            self._continuous_mode = False
            self._next_song_queued = False
            self._pick_in_progress = False
            self._paused_by_dj = False
            self._vibe.clear()
            self._selector.clear_backups()
            self._persist_state()
            logger.info("DJ disabled")

        elif isinstance(event, SetContinuousMode):
            self._continuous_mode = event.enabled
            if not event.enabled:
                self._next_song_queued = False
            self._persist_state()
            logger.info(f"Continuous mode set to {self._continuous_mode}")

        elif isinstance(event, SetPauseOnAfk):
            # Legacy: ignore. AFK is handled by frontend.
            return

        elif isinstance(event, TrackChanged):
            self._on_track_changed(event.track)

        elif isinstance(event, RequestPickAndQueue):
            self._maybe_pick_and_queue(reason=event.reason)

        elif isinstance(event, PickSong):
            result: Optional[Dict[str, Any]] = None
            try:
                result = self._pick_song(debounce=False, allow_when_disabled=bool(getattr(event, "allow_when_disabled", False)))
            except Exception as e:
                logger.error("Error picking song (%s): %s", event.reason, e)
                logger.debug("error picking song () exception details", exc_info=True)
                result = None

            try:
                if event.reply_queue is not None:
                    event.reply_queue.put(result)
            except Exception as e:
                logger.warning(f"Could not reply to PickSong request: {e}", exc_info=True)

        elif isinstance(event, RequestBackupSong):
            result: Optional[Dict[str, Any]] = None
            try:
                result = self._get_backup_song_internal()
            except Exception as e:
                logger.error("Error getting backup song: %s", e)
                logger.debug("error getting backup song exception details", exc_info=True)
                result = None

            try:
                if event.reply_queue is not None:
                    event.reply_queue.put(result)
            except Exception as e:
                logger.warning(f"Could not reply to RequestBackupSong: {e}", exc_info=True)

        elif isinstance(event, FrontendQueued):
            title = event.data.get("title", "")
            artist = event.data.get("artist", "")
            logger.debug(f"Frontend confirmed queue: {title} by {artist}")

    # NOTE: AFK pause/resume is handled in the frontend now (music_afk_state socket event).

    # Music events: queueing and track changes

    def _on_track_changed(self, track: Optional[Dict[str, Any]]) -> None:
        if track:
            new_id = f"{track.get('title', '')}-{track.get('artist', '')}"
            if new_id != self._current_track_id:
                old_id = self._current_track_id
                self._current_track_id = new_id
                self._next_song_queued = False
                logger.info(f"Track changed: '{old_id}' to '{new_id}', queue flag reset")
        else:
            if self._current_track_id is not None:
                logger.info(f"Track ended (was: {self._current_track_id})")
                self._current_track_id = None
                self._next_song_queued = False

    # Pick flow

    def _maybe_pick_and_queue(self, reason: str) -> None:
        try:
            # Enforce queue retry cooldown if we recently failed to queue.
            if self._queue_retry_after_utc is not None:
                now_utc = datetime.now(timezone.utc)
                if now_utc < self._queue_retry_after_utc:
                    return

            # Pick requests from the frontend are authoritative; do not require "DJ enabled"
            # (that flag is for AFK automation / optional chat-driven behavior).
            picked = self._pick_song(debounce=True, allow_when_disabled=True)
            if not picked:
                self._schedule_queue_retry("no_pick_result")
                return

            if picked.get("skip_music", False):
                self._schedule_queue_retry("skip_music")
                return

            title = (picked.get("title") or "").strip()
            artist = (picked.get("artist") or "").strip()

            # Backward-compatible fallback
            if (not title or not artist) and (picked.get("search_query") or "").strip():
                t2, a2 = parse_search_query(str(picked.get("search_query")))
                title = title or t2
                artist = artist or a2

            if not title:
                self._schedule_queue_retry("empty_query")
                return

            targets = picked.get("targets", {}) if isinstance(picked.get("targets"), dict) else {}
            self.record_pick(title=title, artist=artist or "Unknown", targets=targets)

            # Attach current weight factors to each candidate in the batch so UI can show
            # track/artist/genre weights without any reads.
            def _norm(s: Any) -> str:
                return str(s or "").strip().lower()

            def _track_weight_key(tid: str, t: str, a: str) -> str:
                if tid:
                    return f"spotify:{_norm(tid)}"
                return ""

            def _get_weight_factors(track_id: str, t: str, a: str, g: str) -> dict[str, float]:
                try:
                    from app.assistant.database.db_instance import db
                    from app.models.music_weights import MusicTrackWeight, MusicArtistWeight, MusicGenreWeight

                    w_track = 1.0
                    w_artist = 1.0
                    w_genre = 1.0
                    if self._app is None:
                        raise RuntimeError("DJManager._app is not set — pass app= at construction time.")
                    with self._app.app_context():
                        try:
                            k = _track_weight_key(track_id, t, a)
                            if k:
                                row = db.session.get(MusicTrackWeight, k)
                                if row is not None:
                                    w_track = float(row.factor)
                        except Exception as e:
                            logger.debug("Failed to fetch track weight for %r: %s", track_id, e, exc_info=True)
                        try:
                            row = db.session.get(MusicArtistWeight, _norm(a))
                            if row is not None:
                                w_artist = float(row.factor)
                        except Exception as e:
                            logger.debug("Failed to fetch artist weight for %r: %s", a, e, exc_info=True)
                        try:
                            row = db.session.get(MusicGenreWeight, _norm(g))
                            if row is not None:
                                w_genre = float(row.factor)
                        except Exception as e:
                            logger.debug("Failed to fetch genre weight for %r: %s", g, e, exc_info=True)
                    return {"track": float(w_track), "artist": float(w_artist), "genre": float(w_genre)}
                except Exception as e:
                    logger.debug("Weight enrichment unavailable for candidate: %s", e, exc_info=True)
                    return {"track": 1.0, "artist": 1.0, "genre": 1.0}

            try:
                if isinstance(picked.get("candidates"), list):
                    for c in picked["candidates"]:
                        if not isinstance(c, dict):
                            continue
                        tid = str(c.get("track_id") or "").strip()
                        tt = str(c.get("title") or "").strip()
                        aa = str(c.get("artist") or "").strip()
                        gg = str(c.get("genre") or "").strip()
                        c["weights"] = _get_weight_factors(tid, tt, aa, gg)
            except Exception as e:
                logger.debug("Failed to enrich candidates with weight data: %s", e, exc_info=True)

            # Send canonical dataset meta along with the queue request so the UI can show
            # energy/valence/etc even if Apple Music returns a slightly different display title/artist.
            ok = self.play_song(
                build_search_query(title, artist),
                mode="queue_next",
                payload={
                    "dataset": {
                        "track_id": picked.get("track_id") or "",
                        "title": title,
                        "artist": artist,
                        "genre": picked.get("genre"),
                        "sliders": picked.get("sliders"),
                        "prob_factor": picked.get("prob_factor"),
                        "weights": picked.get("weights"),
                    },
                    # Batch for Apple fallback (primary + backups)
                    "candidates": picked.get("candidates") if isinstance(picked.get("candidates"), list) else None,
                },
            )
            if ok:
                self._next_song_queued = True
                self._queue_retry_after_utc = None
                self._stats["last_action"] = f"queue_next({reason})"
                self._stats["last_action_time"] = datetime.now(timezone.utc).isoformat()
            else:
                self._schedule_queue_retry("socket_failed")
        except Exception as e:
            logger.error("Error during pick and queue: %s", e)
            logger.debug("error during pick and queue exception details", exc_info=True)
            self._schedule_queue_retry("exception")
        finally:
            self._pick_in_progress = False

    def _schedule_queue_retry(self, why: str) -> None:
        self._next_song_queued = False
        self._queue_retry_after_utc = datetime.now(timezone.utc) + timedelta_seconds(QUEUE_RETRY_COOLDOWN_SECONDS)
        logger.info(f"Queue retry scheduled in {QUEUE_RETRY_COOLDOWN_SECONDS}s (reason: {why})")

    def _pick_song(self, *, debounce: bool, allow_when_disabled: bool = False) -> Optional[Dict[str, Any]]:
        """
        Flow:
        1) Ensure vibe plan fresh
        2) Compute targets
        3) Sample 20 candidates from dataset (randomized, vibe-aligned)
        4) DJ (LLM) selects 10 finalists from those 20 (no inventing)
        5) Choose uniformly at random from finalists, store backups
        """
        if (not allow_when_disabled) and (not self._enabled):
            return None

        now_utc = datetime.now(timezone.utc)
        if debounce and self._last_pick_utc:
            elapsed = (now_utc - self._last_pick_utc).total_seconds()
            if elapsed < PICK_DEBOUNCE_SECONDS:
                logger.debug(f"Debounced pick ({elapsed:.1f}s since last)")
                return None

        if self._pick_in_progress:
            return None

        self._pick_in_progress = True
        self._last_pick_utc = now_utc

        try:
            from app.assistant.utils.pydantic_classes import Message
            from app.assistant.utils.time_utils import get_local_time
            from app.assistant.pipelines.dayflow.utils.context_sources import get_calendar_events_for_orchestrator
            from app.assistant.utils.chat_formatting import messages_to_chat_excerpts

            now_local = get_local_time()

            calendar_events = []
            recent_chat = []
            try:
                calendar_events = get_calendar_events_for_orchestrator()
            except Exception as e:
                logger.warning(f"Could not load calendar events for DJManager: {e}", exc_info=True)
            try:
                from app.assistant.ServiceLocator.service_locator import DI

                logger.info("DJ recent_chat(music) fetch starting")
                # Vibe check should receive ONLY music-scoped chat instructions.
                now_utc2 = datetime.now(timezone.utc)
                msgs = DI.global_blackboard.get_recent_chat_since_utc(
                    now_utc2 - timedelta(hours=3),
                    limit=None,
                    include_tags=["music"],
                    include_command_scopes=["music"],
                    )
                recent_chat = messages_to_chat_excerpts(msgs)

                # TEMP DEBUG: log what we fetched so we can confirm routing.
                try:
                    logger.info(f"DJ recent_chat(music) fetched={len(recent_chat)} (last 3h)")
                    for i, m in enumerate(recent_chat[:5]):
                        logger.info(f"DJ recent_chat(music)[{i+1}]: [{m.get('time_local')}] {m.get('sender')}: {m.get('content')}")
                except Exception as e:
                    logger.warning(f"DJ recent_chat(music) preview log failed: {e}", exc_info=True)
            except Exception as e:
                # Don't swallow: if this fails, /music routing will be invisible.
                logger.warning(f"DJ recent_chat(music) fetch failed: {e}")

            self._vibe.ensure_fresh_plan(calendar_events=calendar_events, recent_chat=recent_chat)
            targets = self._vibe.get_targets()

            recently_played = self._get_recently_played()
            last_played = None
            try:
                from app.models.played_songs import get_last_played_song

                last_played = get_last_played_song()
            except Exception:
                last_played = None
            # ------------------------------------------------------------------
            # Candidate generation (dataset)
            # ------------------------------------------------------------------
            candidates_20: list[dict[str, Any]] = []
            try:
                from app.assistant.dj_manager.music_dataset import SqliteMusicDataset

                audio_targets = targets.get("audio_targets") if isinstance(targets, dict) else None
                if isinstance(audio_targets, dict):
                    # User-requested filters (from vibe_check). These should be treated as authoritative.
                    music_filters = targets.get("music_filters") if isinstance(targets, dict) else None
                    if not isinstance(music_filters, dict):
                        music_filters = {}

                    # Safety guard: if music_filters has include_artists/include_genres but recent
                    # chat is empty (no user request), the LLM may have invented constraints that
                    # will produce zero candidates. Clear include-type filters in that case.
                    if music_filters and not recent_chat:
                        had_artists = bool(music_filters.get("include_artists"))
                        had_genres = bool(music_filters.get("include_genres"))
                        had_kw = bool(music_filters.get("include_keywords"))
                        if had_artists or had_genres or had_kw:
                            logger.warning(
                                "[dj_pick] Clearing music_filters include constraints (include_artists=%s include_genres=%s include_keywords=%s): "
                                "no recent user chat to justify them — vibe_check likely invented these.",
                                music_filters.get("include_artists"),
                                music_filters.get("include_genres"),
                                music_filters.get("include_keywords"),
                            )
                            music_filters.pop("include_artists", None)
                            music_filters.pop("include_genres", None)
                            music_filters.pop("include_keywords", None)
                            music_filters.pop("include_related_artists", None)

                    # If user asked for "like <artist>", expand include_artists with Apple-related artists (cached).
                    try:
                        if bool(music_filters.get("include_related_artists")):
                            anchors = music_filters.get("include_artists") if isinstance(music_filters.get("include_artists"), list) else []
                            anchors = [str(a).strip() for a in anchors if isinstance(a, str) and a.strip()]
                            if anchors:
                                from app.assistant.music_manager.apple_catalog import get_related_artists_cached

                                expanded: list[str] = list(anchors)
                                for anchor in anchors[:2]:
                                    rel = get_related_artists_cached(anchor_artist=anchor).related_artists
                                    expanded.extend(rel or [])
                                # de-dupe
                                seen = set()
                                uniq = []
                                for a in expanded:
                                    k = str(a).strip().lower()
                                    if not k or k in seen:
                                        continue
                                    seen.add(k)
                                    uniq.append(str(a).strip())
                                music_filters["include_artists"] = uniq[:25]
                    except Exception as e:
                        logger.debug("Apple related artists expansion failed (non-fatal): %s", e, exc_info=True)

                    # Default guardrail: if user wants low energy and did NOT explicitly request
                    # electronic/dance, avoid beat-forward electronic genres that often "feel" high-intensity
                    # despite low energy. This prevents "low energy" selecting dub/techno-style tracks.
                    #
                    # We only apply this when there are no explicit INCLUDE constraints.
                    def _has_includes(mf: dict) -> bool:
                        return bool(mf.get("include_artists") or mf.get("include_genres") or mf.get("include_keywords"))

                    try:
                        energy = float(audio_targets.get("energy", 55))
                    except Exception:
                        energy = 55.0

                    if energy <= 40.0 and not _has_includes(music_filters):
                        existing_exc = music_filters.get("exclude_genres")
                        exc = list(existing_exc) if isinstance(existing_exc, list) else []
                        # Conservative substrings; matched against dataset genre text.
                        for g in ["edm", "dance", "techno", "house", "dub", "trance", "electronic"]:
                            if g not in [str(x).strip().lower() for x in exc]:
                                exc.append(g)
                        music_filters["exclude_genres"] = exc
                    dataset = SqliteMusicDataset()
                    seed = int(datetime.now(timezone.utc).timestamp() * 1000) & 0xFFFFFFFF

                    # Bias genres primarily using the core sliders (energy/valence/acousticness),
                    # rather than a separate knob, to avoid "knob collisions".
                    try:
                        acoustic = float(audio_targets.get("acousticness", 40))
                    except Exception:
                        acoustic = 40.0

                    # The user's recorded taste, loaded once per pick. This is what makes
                    # the boost/ban buttons (and implicit playback signals) actually steer
                    # selection. A load failure is logged LOUD and the pick proceeds
                    # taste-neutral — degraded, never silently wrong.
                    learned = None
                    try:
                        from app.assistant.dj_manager.learned_weights import load_learned_weights
                        learned = load_learned_weights()
                    except Exception as e:
                        logger.error("[dj_pick] learned weights unavailable — picking "
                                     "taste-neutral this round: %s", e, exc_info=True)

                    # Genre boosting comes from the LEARNED genre weights (the user's own
                    # taste table), not hardcoded genre-family lists. Vibe alignment is
                    # already the distance term's job; taste is the boost's job. With no
                    # learned genres yet, sampling is distance+prob_factor driven.
                    boost_genres = sorted(learned.preferred_genres()) if learned else []

                    # "Random 20": we still bias toward the current vibe by sampling from a
                    # close-match pool with weights + recency filters.
                    _pool, prompt_20 = dataset.sample_for_prompt(
                        audio_targets,
                        music_filters=music_filters,
                        match_pool_size=250,
                        base_pool_size=10000,
                        prompt_pick_count=20,
                        boost_genres=boost_genres,
                        # Mild flat lift only: the exact per-genre factor already rides in
                        # via `learned` (taste_w), so a large boost here would double-count.
                        boost_factor=2.0,
                        max_energy_delta=7.5,
                        max_valence_delta=12.5,
                        seed=seed,
                        learned=learned,
                    )

                    candidates_20 = [
                        {
                            "track_id": getattr(s, "track_id", "") or "",
                            "title": s.track_name,
                            "artist": s.artist,
                            "reasoning": "Candidate from dataset close-match pool (randomized, vibe-aligned)",
                            "genre": s.genre,
                            "sliders": s.sliders,
                            "prob_factor": getattr(s, "prob_factor", 1.0),
                        }
                        for s in prompt_20
                    ]
                    logger.info("DJ dataset candidates=%s (seed=%s)", len(candidates_20), seed)
            except Exception as e:
                logger.warning(f"Could not sample dataset candidates for DJ pick: {e}", exc_info=True)

            if not candidates_20:
                return None

            # Index provided candidates so we can attach dataset stats to finalists.
            provided_by_key: dict[tuple[str, str], dict[str, Any]] = {}
            for s in candidates_20:
                try:
                    kt = str(s.get("title") or "").strip().lower()
                    ka = str(s.get("artist") or "").strip().lower()
                    if kt and ka:
                        provided_by_key[(kt, ka)] = s
                except Exception:
                    continue

            # ------------------------------------------------------------------
            # DJ (LLM) chooses 10 finalists from the provided 20 (no inventing)
            # ------------------------------------------------------------------
            finalists_10: list[dict[str, Any]] = []
            try:
                agent = DI.agent_factory.create_agent("dj_orchestrator")
                result = agent.action_handler(
                    Message(
                        agent_input={
                            "day_of_week": now_local.strftime("%A"),
                            "vibe": targets,
                            "recently_played": recently_played,
                            "last_played": last_played,
                            "provided_songs": candidates_20,
                        },
                        scope_context=build_dj_scope_context(actor_id="dj_orchestrator_runner"),
                    )
                )
                data = None
                if hasattr(result, "data") and isinstance(result.data, dict):
                    data = result.data
                elif isinstance(result, dict):
                    data = result

                if data and data.get("skip_music", False):
                    return {"skip_music": True, "skip_reason": data.get("skip_reason", "skip")}

                chosen_raw = (data or {}).get("candidates", []) if isinstance(data, dict) else []
                if not isinstance(chosen_raw, list):
                    chosen_raw = []

                # Enforce subset-of-provided contract (normalize for safety).
                provided_set = {(str(s.get("title") or "").strip().lower(), str(s.get("artist") or "").strip().lower()) for s in candidates_20}
                used: set[tuple[str, str]] = set()
                for c in chosen_raw:
                    if not isinstance(c, dict):
                        continue
                    t = str(c.get("title") or "").strip()
                    a = str(c.get("artist") or "").strip()
                    key = (t.lower(), a.lower())
                    if not t or not a:
                        continue
                    if key not in provided_set:
                        continue
                    if key in used:
                        continue
                    used.add(key)
                    src = provided_by_key.get(key, {})
                    finalists_10.append(
                        {
                            "track_id": src.get("track_id") or "",
                            "title": t,
                            "artist": a,
                            "reasoning": c.get("reasoning", ""),
                            "source": "provided",
                            "genre": src.get("genre"),
                            "sliders": src.get("sliders"),
                            "prob_factor": src.get("prob_factor"),
                        }
                    )
                    if len(finalists_10) >= 10:
                        break
            except Exception as e:
                logger.warning(f"DJ finalist selection (LLM) failed; will fallback: {e}", exc_info=True)

            # Fallback: if LLM failed or returned too few, take the first 10 (still vibe-aligned pool)
            if len(finalists_10) < 10:
                fallback = []
                used = {(c.get("title","").strip().lower(), c.get("artist","").strip().lower()) for c in finalists_10}
                for s in candidates_20:
                    t = str(s.get("title") or "").strip()
                    a = str(s.get("artist") or "").strip()
                    if not t or not a:
                        continue
                    key = (t.lower(), a.lower())
                    if key in used:
                        continue
                    used.add(key)
                    fallback.append(
                        {
                            "track_id": s.get("track_id") or "",
                            "title": t,
                            "artist": a,
                            "reasoning": "Fallback finalist from dataset pool",
                            "source": "provided",
                            "genre": s.get("genre"),
                            "sliders": s.get("sliders"),
                            "prob_factor": s.get("prob_factor"),
                        }
                    )
                    if len(finalists_10) + len(fallback) >= 10:
                        break
                finalists_10 = (finalists_10 + fallback)[:10]

            chosen_bundle = self._selector.choose_uniform(candidates=finalists_10)
            if not chosen_bundle:
                return None

            chosen = chosen_bundle["chosen"]
            logger.info(
                f"Picked: '{chosen.get('title', '')}' by {chosen.get('artist', '')} phase={targets.get('phase_note', 'n/a')}"
            )

            backups = []
            try:
                backups = self._selector.peek_backups(limit=5) if self._selector else []
            except Exception:
                backups = []

            # Build batch (primary + backups) for Apple fallback in the player.
            candidates_batch: list[dict[str, Any]] = []
            try:
                seen = set()
                for item in [chosen] + (backups or []):
                    if not isinstance(item, dict):
                        continue
                    k = (
                        str(item.get("track_id") or "").strip().lower(),
                        str(item.get("title") or "").strip().lower(),
                        str(item.get("artist") or "").strip().lower(),
                    )
                    if k in seen:
                        continue
                    seen.add(k)
                    candidates_batch.append(item)
            except Exception:
                candidates_batch = [chosen]

            return {
                "title": chosen.get("title", ""),
                "artist": chosen.get("artist", ""),
                "reasoning": chosen.get("reasoning", ""),
                "skip_music": False,
                "track_id": chosen.get("track_id") or "",
                "genre": chosen.get("genre"),
                "sliders": chosen.get("sliders"),
                "prob_factor": chosen.get("prob_factor"),
                "candidates": candidates_batch,
                "targets": targets,
            }
        except Exception as e:
            logger.error("DJ pick failed: %s", e)
            logger.debug("dJ pick failed exception details", exc_info=True)
            return None
        finally:
            self._pick_in_progress = False

    def _get_recently_played(self) -> list:
        try:
            from app.models.played_songs import get_recently_played
            return get_recently_played(limit=10)
        except Exception as e:
            logger.debug(f"Could not get recently played: {e}")
            return []

    # Backup API, used when the frontend cannot find the chosen song

    def _get_backup_song_internal(self) -> Optional[Dict[str, Any]]:
        """
        DJ-thread-only backup pop implementation.
        """
        backup = self._selector.pop_backup()
        if not backup:
            logger.info("No backup candidates available")
            return None

        targets = self._vibe.get_targets()
        self.record_pick(title=backup.title, artist=backup.artist, targets=targets, search_query=backup.search_query)

        logger.info(f"Using backup: '{backup.title}' by {backup.artist} (score={backup.score:.3f})")
        return {
            "title": backup.title,
            "artist": backup.artist,
            "search_query": backup.search_query,
            "reasoning": backup.reasoning,
            "genre": getattr(backup, "genre", None),
            "sliders": getattr(backup, "sliders", None),
            "prob_factor": getattr(backup, "prob_factor", None),
        }


def timedelta_seconds(seconds: int) -> timedelta:
    return timedelta(seconds=int(seconds))
