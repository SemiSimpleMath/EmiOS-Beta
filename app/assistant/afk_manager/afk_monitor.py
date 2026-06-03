# afk_monitor.py
"""
AFK Monitor - Handles idle detection and active session tracking.

Model: Active-First (positive evidence)
- Records ACTIVE segments: when user IS at keyboard
- Active time = sum of recorded sessions (bounded, conservative)
- AFK time = gaps between active sessions
- No data = unknown (not active) - the safe default

DB contract (provisional segment model):
- create_provisional_segment(start_utc) - when user becomes active
- update_segment(id, end_utc) - periodically while active (every 5 min)
- update_segment(id, end_utc, finalize=True) - when user goes AFK (closes segment)
- get_open_segment() - on startup, recover orphaned segments from crash

State machine:
- "active": user is at keyboard (idle < threshold)
- "afk": user is away (idle >= threshold)

Important behavior:
- On bootstrap: recover open segments, detect initial state
- On just_went_afk: finalize provisional segment
- On just_returned: create new provisional segment
- While active: update segment end_time every SEGMENT_UPDATE_INTERVAL_MINUTES
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.afk_manager.afk_db import (
    create_provisional_segment,
    update_segment,
    get_open_segment,
)
from app.assistant.utils.time_utils import utc_to_local
from app.assistant.runtime import start_monitored_thread

logger = get_logger(__name__)

# How often to update provisional segments (minutes)
SEGMENT_UPDATE_INTERVAL_MINUTES = 5


@dataclass(frozen=True)
class AFKThresholds:
    potential_minutes: int = 1
    confirmed_minutes: int = 3


class AFKMonitor:
    """
    Monitors user's AFK status via system idle detection.

    Writes a lightweight "computer_activity" snapshot into the provided status_data,
    and records AFK segments into the database.

    Snapshot keys written to status_data["computer_activity"]:
    - idle_minutes: float
    - idle_seconds: int
    - is_potentially_afk: bool (idle >= potential threshold, use for gating expensive ops)
    - is_afk: bool (idle >= confirmed threshold)
    - last_checked: ISO UTC

    Transitional keys (only when relevant):
    - last_afk_start: ISO UTC (while AFK, and also attached once on return)
    - last_afk_duration_minutes: float (only set on return)
    - _afk_return_info: dict (only set on return; optional downstream hook)

    Notes:
    - This class does not compute daily totals or active time. That belongs in a utility
      that reads ActiveSegment rows and applies day boundary logic.
    - All writes to status_data are guarded with a lock.
    """

    def __init__(self, status_data: Optional[Dict[str, Any]] = None):
        self.status_data = status_data if status_data is not None else {}
        self._lock = threading.Lock()

        self._thread: Optional[threading.Thread] = None
        self._running = False

        self.thresholds = self._load_thresholds()

        # Internal state
        self._bootstrapped = False
        self._state: Optional[str] = None
        self._current_afk_start_utc: Optional[datetime] = None
        self._current_idle_seconds_start: Optional[int] = None
        self._current_active_start_utc: Optional[datetime] = None
        # Persisted transition timestamps (stable API for interval consumers)
        self._last_afk_start_utc: Optional[datetime] = None
        self._last_afk_return_utc: Optional[datetime] = None
        
        # Provisional segment tracking
        self._current_segment_id: Optional[int] = None
        self._last_segment_update_utc: Optional[datetime] = None

    # ---------------------------------------------------------------------
    # Config
    # ---------------------------------------------------------------------

    def _load_thresholds(self) -> AFKThresholds:
        """
        Load thresholds from configs/sleep_tracking.yaml (afk_thresholds section).

        Supported keys:
        - afk_thresholds.is_potentially_afk_minutes (or potentially_afk_minutes)
        - afk_thresholds.confirmed_afk_minutes
        """
        default = AFKThresholds()
        try:
            from app.assistant.utils.path_utils import get_configs_dir
            from app.assistant.utils.config_loader import load_config

            config_file = get_configs_dir() / "sleep_tracking.yaml"

            if not config_file.exists():
                logger.warning(f"Sleep config not found at {config_file}, using defaults")
                return default

            cfg = load_config(str(config_file))

            if not isinstance(cfg, dict):
                return default

            afk_cfg = cfg.get("afk_thresholds") or {}
            if not isinstance(afk_cfg, dict):
                return default

            potential = afk_cfg.get(
                "is_potentially_afk_minutes",
                afk_cfg.get("potentially_afk_minutes", default.potential_minutes),
            )
            confirmed = afk_cfg.get("confirmed_afk_minutes", default.confirmed_minutes)

            thresholds = AFKThresholds(
                potential_minutes=max(0, int(potential)),
                confirmed_minutes=max(1, int(confirmed)),
            )

            logger.info(
                f"Loaded AFK thresholds: potential={thresholds.potential_minutes} min, confirmed={thresholds.confirmed_minutes} min"
            )
            return thresholds

        except Exception as e:
            logger.warning(f"Error loading AFK thresholds from config: {e}, using defaults")
            return default

    def _load_input_config(self) -> Dict[str, Any]:
        """Robust input-tracking config from configs/sleep_tracking.yaml
        (input_tracking section). Defaults keep robust tracking on."""
        cfg = {"use_robust": True, "mouse_jitter_px": 25.0, "require_sustained_movement": True}
        try:
            from app.assistant.utils.path_utils import get_configs_dir
            from app.assistant.utils.config_loader import load_config

            config_file = get_configs_dir() / "sleep_tracking.yaml"
            if config_file.exists():
                data = load_config(str(config_file))
                it = data.get("input_tracking") if isinstance(data, dict) else None
                if isinstance(it, dict):
                    cfg["use_robust"] = bool(it.get("use_robust", True))
                    cfg["mouse_jitter_px"] = float(it.get("mouse_jitter_px", 25.0))
                    cfg["require_sustained_movement"] = bool(it.get("require_sustained_movement", True))
        except Exception as e:
            logger.warning(f"Error loading input_tracking config: {e}, using defaults")
        return cfg

    # ---------------------------------------------------------------------
    # Thread management
    # ---------------------------------------------------------------------

    def start(self, interval_seconds: int = 5) -> None:
        """Start background thread that polls idle time."""
        if self._running:
            logger.debug("AFK monitor already running")
            return

        self._running = True
        self._thread = start_monitored_thread(
            owner="afk_monitor",
            name="afk-monitor",
            target=self._loop,
            args=(interval_seconds,),
            daemon=True,
            kind="poll_loop",
            metadata={"component": "afk_monitor", "interval_seconds": interval_seconds},
        )
        logger.info(f"AFK monitor started (polling every {interval_seconds}s)")

        # Robust input tracking (keystroke heartbeat + de-jittered mouse).
        # Best-effort: on failure the monitor falls back to GetLastInputInfo,
        # so AFK detection degrades to today's behavior rather than breaking.
        try:
            from app.assistant.utils.system_activity import start_input_listener

            ic = self._load_input_config()
            active = start_input_listener(
                mouse_jitter_px=ic["mouse_jitter_px"],
                require_sustained=ic["require_sustained_movement"],
                use_robust=ic["use_robust"],
            )
            logger.info(
                "AFK robust input listener: %s",
                "active (keystroke + de-jittered mouse)" if active
                else "inactive (fallback to GetLastInputInfo)",
            )
        except Exception as e:
            logger.warning(f"Could not start robust input listener (fallback to GetLastInputInfo): {e}")

    def stop(self) -> None:
        """Stop the AFK monitor thread."""
        if not self._running:
            return

        self._running = False
        try:
            from app.assistant.utils.system_activity import stop_input_listener
            stop_input_listener()
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=2)
        logger.info("AFK monitor stopped")

    def _loop(self, interval_seconds: int) -> None:
        while self._running:
            try:
                self.update()
            except Exception as e:
                logger.error(f"Error in AFK monitor loop: {e}")
            time.sleep(interval_seconds)

    # ---------------------------------------------------------------------
    # Core logic
    # ---------------------------------------------------------------------

    @staticmethod
    def _now_utc() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _derive_state(
        idle_minutes: float,
        thresholds: AFKThresholds,
    ) -> str:
        """Determine user state based on idle time and configured threshold."""
        if idle_minutes >= float(thresholds.confirmed_minutes):
            return "afk"
        return "active"

    def _safe_idle_from_system(self) -> tuple[float, int, Dict[str, Any]]:
        """
        Returns (idle_minutes, idle_seconds, extras). Never raises.
        extras carries A/B + debug fields: idle_source, raw_idle_seconds,
        keystroke_idle_seconds, mouse_idle_seconds.
        """
        try:
            from app.assistant.utils.system_activity import get_activity_status

            sys_activity = get_activity_status() or {}
            idle_minutes_raw = sys_activity.get("idle_minutes", None)
            idle_seconds_raw = sys_activity.get("idle_seconds", None)
            extras = {
                "idle_source": sys_activity.get("idle_source"),
                "raw_idle_seconds": sys_activity.get("raw_idle_seconds"),
                "keystroke_idle_seconds": sys_activity.get("keystroke_idle_seconds"),
                "mouse_idle_seconds": sys_activity.get("mouse_idle_seconds"),
            }

            if idle_minutes_raw is None or idle_seconds_raw is None:
                # Unknown idle state: default to AFK-safe threshold
                fallback_minutes = float(self.thresholds.confirmed_minutes)
                return fallback_minutes, int(fallback_minutes * 60), extras

            idle_minutes = float(idle_minutes_raw or 0)
            idle_seconds = int(idle_seconds_raw or 0)

            if idle_minutes < 0:
                idle_minutes = 0.0
            if idle_seconds < 0:
                idle_seconds = 0

            return idle_minutes, idle_seconds, extras

        except Exception as e:
            logger.warning(f"Could not get system activity status: {e}")
            fallback_minutes = float(self.thresholds.confirmed_minutes)
            return fallback_minutes, int(fallback_minutes * 60), {}

    @staticmethod
    def _attach_idle_extras(snapshot: Dict[str, Any], extras: Dict[str, Any]) -> Dict[str, Any]:
        """Propagate idle_source / raw_idle / component idles into the published
        snapshot (debuggability + the side-by-side A/B fields)."""
        for k in ("idle_source", "raw_idle_seconds", "keystroke_idle_seconds", "mouse_idle_seconds"):
            v = (extras or {}).get(k)
            if v is not None:
                snapshot[k] = v
        return snapshot

    def update(self) -> Dict[str, Any]:
        """
        Poll system idle status, update snapshot, and record DB events on transitions.

        Returns the snapshot dict (also written into status_data["computer_activity"]).
        """
        # De-jittered mouse sample (no-op unless the robust listener is active).
        try:
            from app.assistant.utils.system_activity import note_cursor_sample
            note_cursor_sample()
        except Exception:
            pass

        now_utc = self._now_utc()
        idle_minutes, idle_seconds, idle_extras = self._safe_idle_from_system()
        next_state = self._derive_state(idle_minutes, self.thresholds)

        # Side-by-side A/B validation: robust idle vs the raw GetLastInputInfo idle.
        # Watch overnight — with a jittery mouse, raw stays ~0 while robust crosses
        # the AFK threshold. Remove this line once the fix is validated.
        try:
            _raw = idle_extras.get("raw_idle_seconds")
            logger.info(
                "[AFK A/B] robust_idle=%ss raw_getlastinput=%s source=%s next_state=%s",
                idle_seconds,
                ("%.1f" % _raw) if isinstance(_raw, (int, float)) else "n/a",
                idle_extras.get("idle_source"),
                next_state,
            )
        except Exception:
            pass

        # Bootstrap: first observation sets internal state and publishes a snapshot.
        # Also check for open segments from a previous crash/restart.
        if not self._bootstrapped:
            self._bootstrapped = True
            
            # Check for open segment from previous session
            self._recover_open_segment(now_utc, next_state)
            
            # Set initial state based on current detection
            if next_state == "active":
                self._state = "active"
                # If we didn't recover an active session, start one now
                if self._current_active_start_utc is None:
                    self._current_active_start_utc = now_utc
                    self._start_provisional_segment(now_utc)
                logger.info(f"[BOOTSTRAP] User is active at startup")
            elif next_state == "afk":
                self._state = "afk"
                # Backdate AFK start by current idle duration — the user went idle
                # before we started monitoring, not at the moment we booted.
                self._current_afk_start_utc = now_utc - timedelta(seconds=idle_seconds)
                logger.info(f"[BOOTSTRAP] User is AFK at startup (idle={idle_minutes:.1f} min, backdated start)")
            else:
                self._state = None
                logger.info(f"[BOOTSTRAP] User state unknown at startup (idle={idle_minutes:.1f} min)")
            
            is_potentially_afk = idle_minutes >= float(self.thresholds.potential_minutes)
            is_afk = self._state == "afk"
            snapshot = self._build_snapshot(
                now_utc=now_utc,
                idle_minutes=idle_minutes,
                idle_seconds=idle_seconds,
                is_potentially_afk=is_potentially_afk,
                is_afk=is_afk,
                just_returned=False,
                return_duration_minutes=None,
            )
            self._attach_idle_extras(snapshot, idle_extras)
            self._publish_snapshot(snapshot)
            return snapshot

        # Transitions
        just_went_afk = False
        just_returned = False

        if self._state is None:
            if next_state == "active":
                self._state = "active"
                just_returned = True  # _current_active_start_utc set in just_returned block below
            elif next_state == "afk":
                self._state = "afk"
                just_went_afk = True
        elif self._state == "active" and next_state == "afk":
            self._state = "afk"
            just_went_afk = True
        elif self._state == "afk" and next_state == "active":
            self._state = "active"
            just_returned = True  # _current_active_start_utc + provisional segment in just_returned block

        # Handle went_afk - finalize the provisional segment
        active_session_minutes: Optional[float] = None
        if just_went_afk:
            # Finalize the active session that just ended
            active_start = self._current_active_start_utc
            segment_id = self._current_segment_id
            
            logger.info(
                f"[AFK TRANSITION] User went AFK at {now_utc.isoformat()} "
                f"(idle={idle_minutes:.1f} min, threshold={self.thresholds.confirmed_minutes} min)"
            )
            
            if active_start is not None:
                active_session_minutes = (now_utc - active_start).total_seconds() / 60.0
                if active_session_minutes > 0:
                    # Final update to close the segment
                    self._finalize_segment(now_utc)
                    logger.info(
                        f"[SEGMENT FINALIZED] ID={segment_id}, "
                        f"start={active_start.isoformat()}, end={now_utc.isoformat()}, "
                        f"duration={active_session_minutes:.1f} min"
                    )
            
            # Track when AFK started (for current_afk_minutes calculation)
            self._current_afk_start_utc = now_utc
            self._last_afk_start_utc = now_utc
            self._current_idle_seconds_start = int(idle_seconds)
            self._current_active_start_utc = None
            self._current_segment_id = None
            self._last_segment_update_utc = None

        # Handle returned - start new active session
        return_duration_minutes: Optional[float] = None
        return_afk_start_utc: Optional[datetime] = None
        if just_returned:
            # Calculate how long user was AFK — capture exact start before clearing.
            afk_start = self._current_afk_start_utc
            return_afk_start_utc = afk_start
            if afk_start is not None:
                return_duration_minutes = (now_utc - afk_start).total_seconds() / 60.0
                if return_duration_minutes < 0:
                    return_duration_minutes = 0.0
                logger.info(
                    f"[AFK TRANSITION] User returned from AFK at {now_utc.isoformat()}, "
                    f"was away {return_duration_minutes:.1f} min"
                )
            else:
                return_duration_minutes = 0.0
                logger.info(f"[AFK TRANSITION] User became active at {now_utc.isoformat()} (first activity)")

            # Start new active session
            self._current_active_start_utc = now_utc
            self._start_provisional_segment(now_utc)
            # Persist the return timestamp for downstream interval consumers.
            self._last_afk_return_utc = now_utc

            # Clear AFK state
            self._current_afk_start_utc = None
            self._current_idle_seconds_start = None

        # Periodic update of provisional segment while active
        if self._state == "active" and not just_returned:
            self._maybe_update_segment(now_utc)

        # Publish snapshot
        # is_potentially_afk = idle >= potential threshold (use for gating expensive ops)
        # is_afk = confirmed AFK state
        is_potentially_afk = idle_minutes >= float(self.thresholds.potential_minutes)
        is_afk = self._state == "afk"
        snapshot = self._build_snapshot(
            now_utc=now_utc,
            idle_minutes=idle_minutes,
            idle_seconds=idle_seconds,
            is_potentially_afk=is_potentially_afk,
            is_afk=is_afk,
            just_returned=just_returned,
            return_duration_minutes=return_duration_minutes,
            return_afk_start_utc=return_afk_start_utc,
        )
        self._attach_idle_extras(snapshot, idle_extras)
        self._publish_snapshot(snapshot)

        # Publish event on AFK state transitions (event bus consumers should not poll).
        if just_went_afk or just_returned:
            try:
                from app.assistant.ServiceLocator.service_locator import DI
                from app.assistant.utils.pydantic_classes import Message

                DI.event_hub.publish(
                    Message(
                        data_type="event",
                        sender="afk_monitor",
                        receiver=None,
                        event_topic="afk_state_changed",
                        content="afk" if is_afk else "active",
                        data={
                            "is_afk": bool(is_afk),
                            "just_went_afk": bool(just_went_afk),
                            "just_returned": bool(just_returned),
                            "snapshot": snapshot,
                        },
                    )
                )
            except Exception as e:
                # Never let event publishing break AFK monitoring.
                logger.debug(f"AFKMonitor: failed to publish afk_state_changed event: {e}", exc_info=True)

        return snapshot

    def _build_snapshot(
            self,
            now_utc: datetime,
            idle_minutes: float,
            idle_seconds: int,
            is_potentially_afk: bool,
            is_afk: bool,
            just_returned: bool,
            return_duration_minutes: Optional[float],
            return_afk_start_utc: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        snapshot: Dict[str, Any] = {
            "idle_minutes": round(idle_minutes, 2),
            "idle_seconds": int(idle_seconds),
            "is_potentially_afk": bool(is_potentially_afk),
            "is_afk": bool(is_afk),
            "last_checked": now_utc.isoformat(),
        }

        # Stable API: always include these keys (None until first transition observed).
        snapshot["last_afk_start_utc"] = self._last_afk_start_utc.isoformat() if self._last_afk_start_utc else None
        snapshot["last_afk_return_utc"] = self._last_afk_return_utc.isoformat() if self._last_afk_return_utc else None

        # While active, expose the current active start time
        if not is_afk and self._current_active_start_utc is not None:
            snapshot["active_start"] = utc_to_local(self._current_active_start_utc).strftime("%Y-%m-%d %I:%M %p")
            snapshot["active_start_utc"] = self._current_active_start_utc.isoformat()

        # While AFK, expose the current segment start
        if is_afk and self._current_afk_start_utc is not None:
            snapshot["last_afk_start"] = self._current_afk_start_utc.isoformat()

        # On return, use the exact AFK start time captured before clearing.
        if just_returned:
            afk_start_iso = return_afk_start_utc.isoformat() if return_afk_start_utc else None
            duration_rounded = round(float(return_duration_minutes or 0.0), 1)
            snapshot["last_afk_start"] = afk_start_iso
            snapshot["last_afk_duration_minutes"] = duration_rounded
            snapshot["_afk_return_info"] = {
                "afk_start": afk_start_iso,
                "afk_end": now_utc.isoformat(),
                "afk_duration_minutes": duration_rounded,
            }

        return snapshot

    def _publish_snapshot(self, snapshot: Dict[str, Any]) -> None:
        with self._lock:
            self.status_data["computer_activity"] = snapshot

    # ---------------------------------------------------------------------
    # Query helpers
    # ---------------------------------------------------------------------

    def get_computer_activity(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self.status_data.get("computer_activity", {}) or {})

    def is_user_at_computer(self) -> bool:
        activity = self.get_computer_activity()
        return not activity.get("is_potentially_afk", False) and not activity.get("is_afk", False)

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def get_monitor_status(self) -> Dict[str, Any]:
        return {
            "thread_running": self._running,
            "thread_alive": self._thread.is_alive() if self._thread else False,
            "thread_name": self._thread.name if self._thread else None,
            "bootstrapped": self._bootstrapped,
            "thresholds": {
                "potential_minutes": self.thresholds.potential_minutes,
                "confirmed_minutes": self.thresholds.confirmed_minutes,
            },
        }

    # ---------------------------------------------------------------------
    # Provisional segment management
    # ---------------------------------------------------------------------

    def _recover_open_segment(self, now_utc: datetime, next_state: Optional[str]) -> None:
        """
        On startup, check for an open segment from a previous crash/restart.
        
        If found and user is currently active, resume from that segment.
        If found and user is AFK, the segment is already "closed enough".
        """
        try:
            open_seg = get_open_segment(max_age_minutes=30)
            if not open_seg:
                return
            
            segment_id = open_seg['id']
            start_time = open_seg['start_time']
            
            # Ensure start_time is timezone-aware
            if hasattr(start_time, 'tzinfo') and start_time.tzinfo is None:
                from datetime import timezone as tz
                start_time = start_time.replace(tzinfo=tz.utc)
            
            if next_state == "active":
                # User is active - resume tracking from the open segment
                self._current_segment_id = segment_id
                self._current_active_start_utc = start_time
                self._last_segment_update_utc = now_utc
                
                # Update the segment to current time
                update_segment(segment_id, now_utc)
                
                duration = (now_utc - start_time).total_seconds() / 60.0
                logger.info(
                    f"Recovered open segment ID={segment_id}, "
                    f"resuming from {start_time.isoformat()} ({duration:.1f} min ago)"
                )
            else:
                # User is AFK — close the segment at the last known end_time,
                # NOT at now. We don't know when the user actually left, so
                # using now would over-credit active time for the entire gap
                # between the last update and startup.
                last_end = open_seg.get('end_time') or start_time
                if hasattr(last_end, 'tzinfo') and last_end.tzinfo is None:
                    from datetime import timezone as tz
                    last_end = last_end.replace(tzinfo=tz.utc)
                update_segment(segment_id, last_end, finalize=True)
                gap_min = (now_utc - last_end).total_seconds() / 60.0
                logger.info(
                    f"Closed orphaned segment ID={segment_id} at last known end_time "
                    f"{last_end.isoformat()} (user is AFK, gap={gap_min:.1f} min)"
                )
                
        except Exception as e:
            logger.warning(f"Error recovering open segment: {e}")

    def _start_provisional_segment(self, now_utc: datetime) -> None:
        """Create a new provisional segment when user becomes active."""
        try:
            segment_id = create_provisional_segment(now_utc)
            if segment_id:
                self._current_segment_id = segment_id
                self._last_segment_update_utc = now_utc
                logger.info(f"[SEGMENT CREATED] Provisional segment ID={segment_id}, start={now_utc.isoformat()}")
        except Exception as e:
            logger.warning(f"Failed to create provisional segment: {e}")

    def _maybe_update_segment(self, now_utc: datetime) -> None:
        """Update the provisional segment if enough time has passed."""
        if self._current_segment_id is None:
            return
        
        if self._last_segment_update_utc is None:
            self._last_segment_update_utc = now_utc
            return
        
        minutes_since_update = (now_utc - self._last_segment_update_utc).total_seconds() / 60.0
        
        if minutes_since_update >= SEGMENT_UPDATE_INTERVAL_MINUTES:
            try:
                if update_segment(self._current_segment_id, now_utc):
                    # Calculate total duration since segment start
                    duration = 0.0
                    if self._current_active_start_utc:
                        duration = (now_utc - self._current_active_start_utc).total_seconds() / 60.0
                    
                    self._last_segment_update_utc = now_utc
                    logger.info(
                        f"[SEGMENT UPDATED] ID={self._current_segment_id}, "
                        f"duration_so_far={duration:.1f} min (periodic update)"
                    )
            except Exception as e:
                logger.warning(f"Failed to update provisional segment: {e}")

    def _finalize_segment(self, now_utc: datetime) -> None:
        """Finalize the provisional segment when user goes AFK."""
        if self._current_segment_id is None:
            return
        
        try:
            if update_segment(self._current_segment_id, now_utc, finalize=True):
                logger.debug(f"Finalized segment ID={self._current_segment_id}")
        except Exception as e:
            logger.warning(f"Failed to finalize segment: {e}")
