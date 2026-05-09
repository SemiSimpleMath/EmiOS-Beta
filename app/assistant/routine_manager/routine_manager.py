from __future__ import annotations

import threading
import time as _time_mod
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_resources_dir, get_configs_dir, setup_complete
from app.assistant.utils.time_utils import utc_to_local
from app.assistant.routine_manager.utils import (
    read_json_file,
    write_json_file,
    utc_now,
    parse_iso_utc,
    status_dir,
)
from app.assistant.routine_manager.windows import (
    is_in_window,
    resolve_window,
)
from app.assistant.routine_manager.runners import (
    JobRoutineRunner,
    PipelineRoutineRunner,
    TaskRoutineRunner,
    ToolRoutineRunner,
)
from app.assistant.routine_manager.runners.function_runner import FunctionRoutineRunner
from app.assistant.routine_manager.routine_functions import ROUTINE_FUNCTION_REGISTRY
from app.assistant.routine_manager.run_types import RoutineRunContext, RoutineRunResult
from app.assistant.runtime import start_monitored_thread

logger = get_logger(__name__)


def _resources_dir() -> Path:
    return get_resources_dir()


def _configs_dir() -> Path:
    return get_configs_dir()


@dataclass
class RoutineRunState:
    last_run_utc: Optional[str] = None
    last_run_id: Optional[str] = None
    last_started_utc: Optional[str] = None
    last_finished_utc: Optional[str] = None
    last_duration_s: Optional[float] = None
    last_target_date: Optional[str] = None
    last_runner: Optional[str] = None
    last_status: Optional[str] = None
    last_error: Optional[str] = None
    run_count: int = 0


@dataclass
class RoutineConfig:
    routine_id: str
    enabled: bool
    run_policy: Dict[str, Any]
    afk_guard: Dict[str, Any] = field(default_factory=dict)
    manual_toggle: Dict[str, Any] = field(default_factory=dict)
    feature_guard: Optional[str] = None  # feature name for can_run_feature() check
    runner: str = "task"  # task|job|tool|function|pipeline
    spec: Dict[str, Any] = field(default_factory=dict)
    name: Optional[str] = None
    aliases: List[str] = field(default_factory=list)
    notes: Optional[str] = None
    # Generalized trigger spec. Supersedes run_policy long-term but
    # backward-compatible: an entry with run_policy and no `trigger`
    # is treated as `{"type": "time", "policy": <run_policy>}`.
    # Currently supported types:
    #   {"type": "time", "policy": <run_policy dict>}
    #   {"type": "event", "topic": "<event_hub topic>"}
    trigger: Dict[str, Any] = field(default_factory=dict)


class RoutineManager:
    """
    Routine manager that executes recurring routines (batch jobs / tool runs).
    Intended to be called from BackgroundTaskManager on a short interval.
    """

    DEFAULT_CONFIG_FILE = "routines.json"
    _BANNER_LINE = "+" * 39
    _DEFAULT_CAPACITY_WARN_RATIO = 0.80
    _DEFAULT_CAPACITY_CRITICAL_RATIO = 0.95
    _DEFAULT_ALERT_COOLDOWN_SECONDS = 60

    def __init__(self, config_filename: Optional[str] = None, *, resource_manager: Any = None, afk_monitor: Any = None):
        self.config_filename = config_filename or self.DEFAULT_CONFIG_FILE
        self._resource_manager = resource_manager
        self._afk_monitor = afk_monitor
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._running: set[str] = set()
        self._active_threads: Dict[str, threading.Thread] = {}
        self._active_thread_started_utc: Dict[str, datetime] = {}
        # Routines whose event subscriptions have been registered with
        # event_hub. Keyed by routine_id; we never re-subscribe for the
        # same id during the process lifetime (event_hub doesn't expose
        # an idempotent re-register, and dupes would cause double-fires).
        self._wired_event_routine_ids: set[str] = set()
        self._state: Dict[str, Any] = {}
        self._last_capacity_alert_by_key: Dict[str, datetime] = {}
        self._last_runtime_status: Dict[str, Any] = {}
        self._runners: Dict[str, Any] = {
            "task": TaskRoutineRunner(),
            "job": JobRoutineRunner(),
            "tool": ToolRoutineRunner(),
            "pipeline": PipelineRoutineRunner(),
            "function": FunctionRoutineRunner(registry=ROUTINE_FUNCTION_REGISTRY),
        }

    def refresh(self) -> None:
        logger.info("%s ROUTINE REFRESH TICK %s", self._BANNER_LINE, self._BANNER_LINE)

        if not setup_complete():
            logger.info("Setup not complete; skipping all routines.")
            return

        config = self._load_config()
        if not config.get("enabled", True):
            logger.info("Routine manager disabled by config; skipping refresh tick.")
            return

        routines = self._load_routines(config)
        if not routines:
            logger.info("No routines configured; skipping refresh tick.")
            return

        # Wire event-triggered routines exactly once per process. Idempotent:
        # subsequent refresh ticks don't re-subscribe, but newly added
        # event routines (after a config reload) get picked up here.
        self._wire_event_triggers(routines)

        self._load_state(config)
        now_utc = utc_now()
        now_local = utc_to_local(now_utc)
        max_workers = int(config.get("max_workers") or 2)
        self._emit_capacity_threshold_alert_if_needed(
            now_utc=now_utc,
            max_workers=max_workers,
            config=config,
        )

        for routine in routines:
            if not routine.enabled:
                logger.info("Routine skipped: %s (disabled)", routine.routine_id)
                continue
            # Event-triggered routines fire on event_hub publish, not on the
            # polling refresh tick. Skip them here so _should_run isn't called
            # against them and they don't fight the time-policy machinery.
            if str((routine.trigger or {}).get("type") or "").lower() == "event":
                continue
            cap_reached = False
            active_workers = 0
            with self._lock:
                if routine.routine_id in self._running:
                    logger.info("Routine skipped: %s (already running)", routine.routine_id)
                    continue
                active_workers = len(self._active_threads)
                if max_workers > 0 and active_workers >= max_workers:
                    cap_reached = True
            if cap_reached:
                logger.info(
                    "Routine skipped: %s (max workers reached: %s active=%s)",
                    routine.routine_id,
                    max_workers,
                    active_workers,
                )
                self._emit_max_workers_reached_alert(
                    routine_id=routine.routine_id,
                    max_workers=max_workers,
                    active_workers=active_workers,
                    now_utc=now_utc,
                    config=config,
                )
                continue
            should_run, reason = self._should_run(routine, now_utc, now_local)
            if not should_run:
                logger.info("Routine skipped: %s (%s)", routine.routine_id, reason)
                continue
            logger.info(
                "%s ROUTINE READY %s id=%s reason=%s",
                self._BANNER_LINE,
                self._BANNER_LINE,
                routine.routine_id,
                reason,
            )
            self._run_in_thread(routine)
        self._publish_runtime_status(config=config)

    # ---------------------------------------------------------------------
    # Config + State
    # ---------------------------------------------------------------------

    def _load_config(self) -> Dict[str, Any]:
        config_path = get_configs_dir() / self.config_filename
        return read_json_file(config_path) or {}

    def _load_state(self, config: Dict[str, Any]) -> None:
        """
        Load routine status state into memory.

        IMPORTANT:
        This must be serialized with _save_state() and per-routine updates to avoid lost updates.
        """
        with self._state_lock:
            state_file = str(config.get("state_resource_file") or "resource_routine_status.json")
            path = _resources_dir() / state_file
            data = read_json_file(path) or {}
            if not isinstance(data, dict):
                data = {}
            if "routines" not in data or not isinstance(data.get("routines"), dict):
                data["routines"] = {}
            data.setdefault("schema_version", 1)
            self._state = data

    def _save_state(self, config: Dict[str, Any]) -> None:
        with self._state_lock:
            self._save_state_unlocked(config)

    def _save_state_unlocked(self, config: Dict[str, Any]) -> None:
        state_file = str(config.get("state_resource_file") or "resource_routine_status.json")
        path = _resources_dir() / state_file
        write_json_file(path, self._state)
        resource_manager = self._resource_manager
        if resource_manager is None:
            from app.assistant.ServiceLocator.service_locator import DI  # local import — shim only
            resource_manager = getattr(DI, "resource_manager", None)
        if resource_manager:
            resource_id = Path(state_file).stem
            resource_manager.update_resource(resource_id, self._state, persist=False)

    def _mutate_state_entry_atomic(self, config: Dict[str, Any], routine_id: str, mutate_fn) -> RoutineRunState:
        """
        Serialize state read-modify-write for a single routine entry.

        Prevents:
        - concurrent routine threads clobbering each other's updates
        - refresh() reloading state while a routine thread is mid-update
        """
        with self._state_lock:
            # Always re-read from disk to avoid merging against a stale in-memory copy.
            state_file = str(config.get("state_resource_file") or "resource_routine_status.json")
            path = _resources_dir() / state_file
            data = read_json_file(path) or {}
            if not isinstance(data, dict):
                data = {}
            if "routines" not in data or not isinstance(data.get("routines"), dict):
                data["routines"] = {}
            data.setdefault("schema_version", 1)
            self._state = data

            entry = self._get_state_entry(routine_id)
            mutate_fn(entry)
            self._update_state_entry(routine_id, entry)
            self._save_state_unlocked(config)
            return entry

    def _load_routines(self, config: Dict[str, Any]) -> list[RoutineConfig]:
        items = config.get("routines") or []
        # Status-side overrides for the `enabled` flag. configs/routines.json
        # is the **spec** (declarative schema: which routines exist, how
        # they're wired, conservative shipped defaults). The status file
        # is the **runtime state** (what the user actually has on right
        # now, written by the /api/routines/<id>/toggle UI). Same K8s-shape
        # spec/status separation: pulls don't clobber the user's local
        # enables, and the user's commits to routines.json don't push
        # personal flags into the public repo.
        state_enabled = self._read_state_enabled_map()
        routines: list[RoutineConfig] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            routine_id = str(item.get("id") or item.get("routine_id") or "").strip()
            if not routine_id:
                continue
            run_policy = item.get("run_policy") if isinstance(item.get("run_policy"), dict) else {}
            afk_guard = item.get("afk_guard") if isinstance(item.get("afk_guard"), dict) else {}
            manual_toggle = item.get("manual_toggle") if isinstance(item.get("manual_toggle"), dict) else {}
            runner = str(item.get("runner") or "").strip().lower()
            spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
            if not runner:
                raise ValueError(f"Routine '{routine_id}' missing required field: runner")
            if runner not in ("task", "job", "tool", "function", "pipeline"):
                raise ValueError(f"Routine '{routine_id}' has unsupported runner: {runner}")
            # Effective enabled = status override if present, else spec default.
            spec_default_enabled = bool(item.get("enabled", True))
            effective_enabled = state_enabled.get(routine_id, spec_default_enabled)

            # Trigger derivation. Explicit `trigger` wins; otherwise the legacy
            # `run_policy` shape is wrapped as an implicit time trigger.
            explicit_trigger = item.get("trigger")
            if isinstance(explicit_trigger, dict) and explicit_trigger:
                trigger = dict(explicit_trigger)
                ttype = str(trigger.get("type") or "").strip().lower()
                if ttype not in ("time", "event"):
                    raise ValueError(
                        f"Routine '{routine_id}' has unsupported trigger type: {ttype!r} "
                        f"(supported: 'time', 'event')"
                    )
                trigger["type"] = ttype
                if ttype == "event" and not str(trigger.get("topic") or "").strip():
                    raise ValueError(
                        f"Routine '{routine_id}' uses trigger.type=event but is missing 'topic'"
                    )
                if ttype == "time" and "policy" not in trigger:
                    # Allow the explicit form to omit `policy` and inherit run_policy.
                    trigger["policy"] = run_policy
                # Validate the active_window field early — fail loud at load
                # time on a typo'd name rather than silently never firing.
                if ttype == "time" and "active_window" in trigger:
                    try:
                        resolve_window(trigger.get("active_window"))
                    except ValueError as e:
                        raise ValueError(
                            f"Routine '{routine_id}' has invalid active_window: {e}"
                        )
            else:
                trigger = {"type": "time", "policy": run_policy}

            routines.append(
                RoutineConfig(
                    routine_id=routine_id,
                    enabled=bool(effective_enabled),
                    run_policy=run_policy,
                    afk_guard=afk_guard,
                    manual_toggle=manual_toggle,
                    feature_guard=str(item.get("feature_guard") or "").strip() or None,
                    runner=runner,
                    spec=spec,
                    name=str(item.get("name") or "").strip() or None,
                    aliases=[str(a).strip() for a in (item.get("aliases") or []) if str(a).strip()],
                    notes=str(item.get("notes") or "") or None,
                    trigger=trigger,
                )
            )
        return routines

    def _read_state_enabled_map(self) -> Dict[str, bool]:
        """Return {routine_id: enabled} from the status file, for routines
        the user has explicitly toggled. Routines not present in the map
        fall back to the spec's default."""
        with self._state_lock:
            routines_state = (self._state or {}).get("routines") or {}
        out: Dict[str, bool] = {}
        if not isinstance(routines_state, dict):
            return out
        for rid, entry in routines_state.items():
            if isinstance(entry, dict) and "enabled" in entry:
                out[str(rid)] = bool(entry["enabled"])
        return out

    # ---------------------------------------------------------------------
    # Manual toggle guard (for high-risk routines like screenshot capture)
    # ---------------------------------------------------------------------

    def _check_manual_toggle(self, routine: RoutineConfig, now_local: datetime) -> Tuple[bool, str]:
        """
        Enforce a manual "armed/disarmed" toggle for a routine.

        Config shape (routines.json):
          "manual_toggle": {
            "resource_file": "resource_screen_capture_control.json",
            "auto_off_time_local": "08:30"
          }

        Behavior:
        - Routine runs only if enabled=true in the resource file.
        - If enabled and now_local >= auto_off_time_local, auto-disable once per local day.
        - Never auto-enables.
        """
        cfg = routine.manual_toggle or {}
        if not isinstance(cfg, dict) or not cfg:
            return True, "manual_toggle disabled"

        resource_file = str(cfg.get("resource_file") or "").strip()
        if not resource_file:
            logger.info("Routine manual_toggle invalid: %s missing resource_file", routine.routine_id)
            return False, "manual_toggle missing resource_file"

        auto_off = str(cfg.get("auto_off_time_local") or "08:30").strip()
        try:
            hh, mm = [int(x) for x in auto_off.split(":", 1)]
            auto_off_t = time(hour=hh, minute=mm)
        except Exception:
            # Fail safe: if misconfigured, treat as already past auto-off.
            auto_off_t = time(0, 0)

        path = status_dir() / resource_file
        data = read_json_file(path) or {}
        if not isinstance(data, dict):
            data = {}

        enabled = bool(data.get("enabled", False))
        today = now_local.date().isoformat()

        # Auto-disable at configured time, once per local day.
        try:
            if enabled and now_local.time() >= auto_off_t:
                last = data.get("auto_disabled_date_local")
                if last != today:
                    data = dict(data)
                    data.setdefault("schema_version", 1)
                    data["enabled"] = False
                    data["disabled_at_utc"] = utc_now().isoformat()
                    data["disabled_by"] = "system"
                    data["disabled_reason"] = f"auto_off_{auto_off.replace(':', '')}_local"
                    data["auto_disabled_date_local"] = today
                    write_json_file(path, data)
                    logger.info(
                        "%s ROUTINE AUTO-OFF %s id=%s reason=%s",
                        self._BANNER_LINE,
                        self._BANNER_LINE,
                        routine.routine_id,
                        data["disabled_reason"],
                    )
                    enabled = False
        except Exception as e:
            logger.warning("Manual toggle auto-off failed (%s): %s", routine.routine_id, e)
            enabled = False

        if not enabled:
            return False, "manual_toggle disarmed"
        return True, "manual_toggle armed"

    # ---------------------------------------------------------------------
    # Scheduling
    # ---------------------------------------------------------------------

    def _get_state_entry(self, routine_id: str) -> RoutineRunState:
        routines = self._state.get("routines") if isinstance(self._state, dict) else None
        if not isinstance(routines, dict):
            routines = {}
        data = routines.get(routine_id)
        if not isinstance(data, dict):
            return RoutineRunState()
        return RoutineRunState(
            last_run_utc=data.get("last_run_utc"),
            last_run_id=data.get("last_run_id"),
            last_started_utc=data.get("last_started_utc"),
            last_finished_utc=data.get("last_finished_utc"),
            last_duration_s=data.get("last_duration_s"),
            last_target_date=data.get("last_target_date"),
            last_runner=data.get("last_runner"),
            last_status=data.get("last_status"),
            last_error=data.get("last_error"),
            run_count=int(data.get("run_count") or 0),
        )

    def _update_state_entry(self, routine_id: str, entry: RoutineRunState) -> None:
        routines = self._state.setdefault("routines", {})
        if not isinstance(routines, dict):
            routines = {}
            self._state["routines"] = routines
        routines[routine_id] = {
            "last_run_utc": entry.last_run_utc,
            "last_run_id": entry.last_run_id,
            "last_started_utc": entry.last_started_utc,
            "last_finished_utc": entry.last_finished_utc,
            "last_duration_s": entry.last_duration_s,
            "last_target_date": entry.last_target_date,
            "last_runner": entry.last_runner,
            "last_status": entry.last_status,
            "last_error": entry.last_error,
            "run_count": entry.run_count,
        }

    def _should_run(
        self,
        routine: RoutineConfig,
        now_utc: datetime,
        now_local: datetime,
    ) -> Tuple[bool, str]:
        toggle_ok, toggle_reason = self._check_manual_toggle(routine, now_local)
        if not toggle_ok:
            return False, toggle_reason

        afk_ok, afk_reason = self._check_afk_guard(routine)
        if not afk_ok:
            return False, afk_reason

        if routine.feature_guard:
            from app.assistant.user_settings_manager.user_settings import can_run_feature
            if not can_run_feature(routine.feature_guard):
                return False, f"feature '{routine.feature_guard}' disabled or missing keys"

        # Read state under lock to avoid races with concurrent routine updates.
        with self._state_lock:
            entry = self._get_state_entry(routine.routine_id)
        last_finished = parse_iso_utc(entry.last_finished_utc) or parse_iso_utc(entry.last_run_utc)

        # Active window gate (composes with any time policy below).
        # Resolved at load time, but resolve again here so a hot-edit of
        # configs/windows.json takes effect on the next tick without restart.
        active_window_spec = (routine.trigger or {}).get("active_window")
        if active_window_spec:
            try:
                window = resolve_window(active_window_spec)
            except ValueError as e:
                return False, f"window resolve failed: {e}"
            if window is not None and not is_in_window(window, now_utc):
                return False, "outside active window"

        policy_type = str(routine.run_policy.get("type") or "interval").strip().lower()

        if policy_type == "daily":
            time_local = str(routine.run_policy.get("time_local") or "").strip()
            if not time_local or ":" not in time_local:
                return False, "missing daily time_local"
            try:
                hour_str, min_str = time_local.split(":", 1)
                target_hour = int(hour_str)
                target_min = int(min_str)
            except Exception:
                return False, "invalid daily time_local"

            today = now_local.date()
            # "Already ran today" should mean "succeeded today" (by local date).
            if entry.last_status == "success" and last_finished:
                last_local = utc_to_local(last_finished)
                if last_local.date() == today:
                    return False, "already succeeded today"
            if (now_local.hour, now_local.minute) < (target_hour, target_min):
                return False, "not yet time"
            return True, "daily schedule"

        if policy_type == "weekly":
            day_of_week = str(routine.run_policy.get("day_of_week") or "Monday").strip().capitalize()
            time_local = str(routine.run_policy.get("time_local") or "02:00").strip()
            _DOW = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
                    "Friday": 4, "Saturday": 5, "Sunday": 6}
            target_dow = _DOW.get(day_of_week)
            if target_dow is None:
                return False, f"invalid day_of_week: {day_of_week}"
            if ":" not in time_local:
                return False, "missing weekly time_local"
            try:
                hour_str, min_str = time_local.split(":", 1)
                target_hour = int(hour_str)
                target_min = int(min_str)
            except Exception:
                return False, "invalid weekly time_local"

            # Already succeeded this calendar week (Mon–Sun)?
            if entry.last_status == "success" and last_finished:
                last_local = utc_to_local(last_finished)
                # ISO week: both dates must share the same year+week number
                if (last_local.isocalendar()[:2] == now_local.isocalendar()[:2]):
                    return False, "already succeeded this week"

            # Is it the right day and past the target time?
            if now_local.weekday() != target_dow:
                return False, f"not the scheduled day (today={now_local.strftime('%A')})"
            if (now_local.hour, now_local.minute) < (target_hour, target_min):
                return False, "not yet time"
            return True, "weekly schedule"

        if policy_type == "quiet_hours":
            feature = str(routine.run_policy.get("feature") or "").strip()
            if not feature:
                return False, "missing quiet_hours feature"
            # Already succeeded today (local date)?
            today = now_local.date()
            if entry.last_status == "success" and last_finished:
                last_local = utc_to_local(last_finished)
                if last_local.date() == today:
                    return False, "already succeeded today"
            # Run only while inside the configured quiet window for this feature.
            try:
                from app.assistant.user_settings_manager.user_settings import get_settings_manager
                if get_settings_manager().is_quiet_mode_active(feature):
                    return True, "quiet hours active"
            except Exception:
                pass
            return False, "outside quiet hours window"

        min_interval = int(routine.run_policy.get("min_interval_seconds") or 0)
        if min_interval <= 0:
            return True, "interval disabled"
        if not last_finished:
            return True, "first run"
        elapsed = (now_utc - last_finished).total_seconds()
        if elapsed >= min_interval:
            return True, "interval ready"
        return False, "interval not reached"

    def _check_afk_guard(self, routine: RoutineConfig) -> Tuple[bool, str]:
        guard = routine.afk_guard or {}
        if not guard:
            return True, "afk_guard disabled"

        skip_when_afk = bool(guard.get("skip_when_afk", False))
        skip_when_potential = bool(guard.get("skip_when_potentially_afk", False))
        if not (skip_when_afk or skip_when_potential):
            return True, "afk_guard disabled"

        try:
            monitor = self._afk_monitor
            if monitor is None:
                from app.assistant.ServiceLocator.service_locator import DI  # local import — shim only
                monitor = getattr(DI, "afk_manager", None) or getattr(DI, "afk_monitor", None)
            if not monitor:
                # Fail closed: if a routine explicitly asks for AFK gating, do not run
                # when we cannot determine AFK status.
                return False, "afk_monitor unavailable"
            activity = monitor.get_computer_activity() or {}
            if not isinstance(activity, dict) or not activity:
                # Fail closed for unknown AFK status.
                return False, "afk status unknown"
            if skip_when_potential and activity.get("is_potentially_afk", False):
                return False, "user potentially afk"
            if skip_when_afk and activity.get("is_afk", False):
                return False, "user afk"
        except Exception as e:
            logger.warning("RoutineManager AFK check failed: %s", e)
            # Fail closed: if AFK check errors, skip the routine rather than run blind.
            return False, "afk check failed (skip)"

        return True, "afk ok"

    # ---------------------------------------------------------------------
    # Event triggers
    # ---------------------------------------------------------------------

    def _wire_event_triggers(self, routines: list[RoutineConfig]) -> None:
        """Subscribe every event-triggered routine to its event_hub topic.

        Idempotent per routine_id for the lifetime of the process: each
        routine is subscribed at most once even if `refresh()` runs many
        times. The handler closure re-reads `routine.enabled` at fire time
        (via `_resolve_current_routine`), so toggling a routine on/off via
        the admin UI takes effect on the next event fire — no resubscribe
        needed.

        Subscribes regardless of `enabled` state at startup; the handler
        short-circuits when disabled. Subscription is cheap and avoids
        the "toggled on but never wired" edge case.
        """
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            event_hub = getattr(DI, "event_hub", None)
        except Exception:
            event_hub = None

        if event_hub is None:
            return

        for routine in routines:
            trigger = routine.trigger or {}
            if str(trigger.get("type") or "").lower() != "event":
                continue
            if routine.routine_id in self._wired_event_routine_ids:
                continue
            topic = str(trigger.get("topic") or "").strip()
            if not topic:
                continue
            try:
                rid = routine.routine_id
                event_hub.register_event(topic, lambda msg, _rid=rid: self._on_event_fire(_rid, msg))
                self._wired_event_routine_ids.add(rid)
                logger.info(
                    "[routine_manager] wired event trigger: routine=%s topic=%s",
                    rid, topic,
                )
            except Exception as e:
                logger.error(
                    "[routine_manager] failed to wire event trigger for routine=%s topic=%s: %s",
                    routine.routine_id, topic, e, exc_info=True,
                )

    def _on_event_fire(self, routine_id: str, _message: Any) -> None:
        """Handler invoked by event_hub when a subscribed event publishes.

        Re-reads the current routine from config so toggles take effect
        without restart. Honors the same concurrency caps and AFK guard
        as time-triggered routines.
        """
        try:
            routine = self._resolve_current_routine(routine_id)
            if routine is None:
                return  # routine removed from config since wiring; ignore
            if not routine.enabled:
                logger.debug(
                    "[routine_manager] event fired for disabled routine %s; skipping",
                    routine_id,
                )
                return

            # Concurrency: same in-flight + worker-cap rules as time-polling.
            with self._lock:
                if routine_id in self._running:
                    logger.info(
                        "Routine skipped (event): %s (already running)", routine_id,
                    )
                    return

            ok, reason = self._check_afk_guard(routine)
            if not ok:
                logger.info(
                    "Routine skipped (event): %s (afk: %s)", routine_id, reason,
                )
                return

            logger.info("Routine triggered by event: %s", routine_id)
            self._run_in_thread(routine)
        except Exception as e:
            logger.error(
                "[routine_manager] event handler crashed for routine=%s: %s",
                routine_id, e, exc_info=True,
            )

    def _resolve_current_routine(self, routine_id: str) -> Optional[RoutineConfig]:
        """Re-load and return the named routine's current config, or None."""
        try:
            config = self._load_config()
            for r in self._load_routines(config):
                if r.routine_id == routine_id:
                    return r
        except Exception as e:
            logger.warning(
                "[routine_manager] failed to resolve routine %s: %s", routine_id, e,
            )
        return None

    # ---------------------------------------------------------------------
    # Execution
    # ---------------------------------------------------------------------

    def _run_in_thread(self, routine: RoutineConfig) -> None:
        def _target():
            try:
                self._execute_routine(routine)
            finally:
                with self._lock:
                    self._running.discard(routine.routine_id)
                    self._active_threads.pop(routine.routine_id, None)
                    self._active_thread_started_utc.pop(routine.routine_id, None)
                try:
                    self._publish_runtime_status(config=self._load_config())
                except Exception as e:
                    logger.error("Failed to publish runtime status after routine completion: %s", e)
                    logger.debug("routine runtime status publish exception details", exc_info=True)

        with self._lock:
            self._running.add(routine.routine_id)
        logger.info(
            "%s ROUTINE THREAD START %s id=%s runner=%s",
            self._BANNER_LINE,
            self._BANNER_LINE,
            routine.routine_id,
            routine.runner,
        )
        thread = start_monitored_thread(
            owner="routine_manager",
            name=f"routine-{routine.routine_id}",
            target=_target,
            daemon=False,
            kind="routine_worker",
            metadata={"component": "routine_manager", "routine_id": routine.routine_id, "runner": routine.runner},
        )
        with self._lock:
            self._active_threads[routine.routine_id] = thread
            self._active_thread_started_utc[routine.routine_id] = utc_now()
        self._publish_runtime_status(config=self._load_config())

    def shutdown(self, timeout_seconds: float = 120.0) -> None:
        """Wait for all in-flight routine threads to complete."""
        with self._lock:
            threads = list(self._active_threads.values())
        if not threads:
            return
        logger.info(
            "RoutineManager: waiting for %d in-flight routine(s)...",
            len(threads),
        )
        deadline = _time_mod.monotonic() + timeout_seconds
        for t in threads:
            remaining = max(0.1, deadline - _time_mod.monotonic())
            t.join(timeout=remaining)
            if t.is_alive():
                logger.warning(
                    "RoutineManager: thread '%s' did not finish within timeout",
                    t.name,
                )
        logger.info("RoutineManager: shutdown complete")

    def run_routine_now(self, routine_id: str, *, target_date: Optional[str] = None) -> None:
        """
        Run a configured routine immediately (on-demand), bypassing schedule checks.

        This is intended for chat-triggered runs via a tool (e.g., "run routine daily_insights_pipeline").
        """
        rid = (routine_id or "").strip()
        if not rid:
            raise ValueError("routine_id is required")

        config = self._load_config()
        routines = self._load_routines(config)
        routine = next((r for r in routines if r.routine_id == rid), None)
        if routine is None:
            raise ValueError(f"Routine not found: {rid}")

        with self._lock:
            if rid in self._running:
                raise RuntimeError(f"Routine already running: {rid}")
            self._running.add(rid)
        logger.info(
            "%s ROUTINE MANUAL START %s id=%s target_date=%s",
            self._BANNER_LINE,
            self._BANNER_LINE,
            rid,
            target_date,
        )
        try:
            self._execute_routine(routine, target_date=target_date, propagate_exceptions=True)
        finally:
            with self._lock:
                self._running.discard(rid)

    def _dispatch_routine(self, routine: RoutineConfig, run_ctx: RoutineRunContext) -> RoutineRunResult:
        runner_name = (routine.runner or "").strip().lower()
        runner = self._runners.get(runner_name)
        if runner is None:
            raise ValueError(f"Unknown or unsupported runner: {runner_name}")
        return runner.run(routine, run_ctx)

    def _execute_routine(
        self,
        routine: RoutineConfig,
        *,
        target_date: Optional[str] = None,
        propagate_exceptions: bool = False,
    ) -> None:
        config = self._load_config()
        run_id = uuid.uuid4().hex[:8]
        started_at_utc = utc_now()
        started_at_utc_iso = started_at_utc.isoformat()
        started_at_local = utc_to_local(started_at_utc)
        logger.info(
            "%s ROUTINE STARTING %s id=%s run_id=%s runner=%s local_start=%s",
            self._BANNER_LINE,
            self._BANNER_LINE,
            routine.routine_id,
            run_id,
            routine.runner,
            started_at_local.isoformat(),
        )

        # Mark running (do NOT update last_run_utc here; keep that as "last finished").
        self._mutate_state_entry_atomic(
            config,
            routine.routine_id,
            lambda entry: _set_running_state(
                entry,
                run_id=run_id,
                started_at_utc_iso=started_at_utc_iso,
                target_date=target_date,
                runner=routine.runner,
            ),
        )

        status = "success"
        error: Optional[str] = None
        try:
            run_ctx = RoutineRunContext(
                run_id=run_id,
                now_utc=started_at_utc,
                now_local=started_at_local,
                target_date=target_date,
                force=bool((routine.spec or {}).get("force", False)),
            )
            logger.info(
                "Routine dispatch: id=%s run_id=%s runner=%s spec_keys=%s",
                routine.routine_id,
                run_id,
                routine.runner,
                sorted((routine.spec or {}).keys()),
            )
            self._dispatch_routine(routine, run_ctx)
        except Exception as e:
            status = "error"
            error = str(e)[:1000]
            logger.error("Routine '%s' failed", routine.routine_id)
            logger.debug("routine '%s' failed exception details", routine.routine_id, exc_info=True)
            if propagate_exceptions:
                raise
        finally:
            finished_at_utc = utc_now()
            finished_iso = finished_at_utc.isoformat()
            duration_s = round((finished_at_utc - started_at_utc).total_seconds(), 2)
            succeeded = status == "success"

            self._mutate_state_entry_atomic(
                config,
                routine.routine_id,
                lambda entry: _set_finished_state(
                    entry,
                    finished_at_utc_iso=finished_iso,
                    duration_s=duration_s,
                    status=status,
                    error=error,
                    increment_run_count=succeeded,
                ),
            )
            if succeeded:
                logger.info(
                    "%s ROUTINE FINISHED %s id=%s run_id=%s status=success duration_s=%.2f",
                    self._BANNER_LINE,
                    self._BANNER_LINE,
                    routine.routine_id,
                    run_id,
                    duration_s,
                )
            else:
                logger.error(
                    "%s ROUTINE FINISHED %s id=%s run_id=%s status=error duration_s=%.2f error=%s",
                    self._BANNER_LINE,
                    self._BANNER_LINE,
                    routine.routine_id,
                    run_id,
                    duration_s,
                    error,
                )

    def _capacity_alert_cooldown_seconds(self, config: Dict[str, Any]) -> int:
        value = int(config.get("capacity_alert_cooldown_seconds") or self._DEFAULT_ALERT_COOLDOWN_SECONDS)
        if value <= 0:
            return self._DEFAULT_ALERT_COOLDOWN_SECONDS
        return value

    def _capacity_warn_ratio(self, config: Dict[str, Any]) -> float:
        try:
            value = float(config.get("capacity_warn_ratio") or self._DEFAULT_CAPACITY_WARN_RATIO)
        except Exception as e:
            logger.error("Invalid capacity_warn_ratio in routines config: %s", e)
            logger.debug("capacity_warn_ratio parse exception details", exc_info=True)
            value = self._DEFAULT_CAPACITY_WARN_RATIO
        if value <= 0:
            return self._DEFAULT_CAPACITY_WARN_RATIO
        return min(value, 1.0)

    def _capacity_critical_ratio(self, config: Dict[str, Any]) -> float:
        try:
            value = float(config.get("capacity_critical_ratio") or self._DEFAULT_CAPACITY_CRITICAL_RATIO)
        except Exception as e:
            logger.error("Invalid capacity_critical_ratio in routines config: %s", e)
            logger.debug("capacity_critical_ratio parse exception details", exc_info=True)
            value = self._DEFAULT_CAPACITY_CRITICAL_RATIO
        if value <= 0:
            return self._DEFAULT_CAPACITY_CRITICAL_RATIO
        return min(value, 1.0)

    def _should_emit_capacity_alert(self, *, key: str, now_utc: datetime, config: Dict[str, Any]) -> bool:
        cooldown_seconds = self._capacity_alert_cooldown_seconds(config)
        last = self._last_capacity_alert_by_key.get(key)
        if last is None:
            self._last_capacity_alert_by_key[key] = now_utc
            return True
        elapsed = (now_utc - last).total_seconds()
        if elapsed >= cooldown_seconds:
            self._last_capacity_alert_by_key[key] = now_utc
            return True
        return False

    def _emit_capacity_threshold_alert_if_needed(
        self,
        *,
        now_utc: datetime,
        max_workers: int,
        config: Dict[str, Any],
    ) -> None:
        if max_workers <= 0:
            return
        with self._lock:
            active = len(self._active_threads)
        ratio = active / float(max_workers)
        critical_ratio = self._capacity_critical_ratio(config)
        warn_ratio = self._capacity_warn_ratio(config)

        if ratio >= critical_ratio:
            key = "routine_capacity_critical"
            if self._should_emit_capacity_alert(key=key, now_utc=now_utc, config=config):
                logger.error(
                    "Routine capacity critical: active_workers=%s max_workers=%s saturation=%.2f",
                    active,
                    max_workers,
                    ratio,
                )
        elif ratio >= warn_ratio:
            key = "routine_capacity_warning"
            if self._should_emit_capacity_alert(key=key, now_utc=now_utc, config=config):
                logger.warning(
                    "Routine capacity warning: active_workers=%s max_workers=%s saturation=%.2f",
                    active,
                    max_workers,
                    ratio,
                )

    def _emit_max_workers_reached_alert(
        self,
        *,
        routine_id: str,
        max_workers: int,
        active_workers: int,
        now_utc: datetime,
        config: Dict[str, Any],
    ) -> None:
        key = "routine_capacity_max_reached"
        if not self._should_emit_capacity_alert(key=key, now_utc=now_utc, config=config):
            return
        logger.error(
            "Routine capacity cap reached: routine_id=%s active_workers=%s max_workers=%s. New work is being skipped.",
            routine_id,
            active_workers,
            max_workers,
        )

    def _build_runtime_status_payload(self, *, config: Dict[str, Any]) -> Dict[str, Any]:
        max_workers = int(config.get("max_workers") or 2)
        now_utc = utc_now()
        with self._lock:
            thread_rows = []
            for routine_id, thread in self._active_threads.items():
                started_at = self._active_thread_started_utc.get(routine_id)
                running_for_s = None
                if isinstance(started_at, datetime):
                    running_for_s = round((now_utc - started_at).total_seconds(), 2)
                thread_rows.append(
                    {
                        "routine_id": routine_id,
                        "thread_name": str(thread.name or ""),
                        "thread_ident": thread.ident,
                        "is_alive": bool(thread.is_alive()),
                        "started_at_utc": started_at.isoformat() if isinstance(started_at, datetime) else None,
                        "running_for_s": running_for_s,
                    }
                )
            active_workers = len(self._active_threads)

        saturation_ratio = 0.0
        if max_workers > 0:
            saturation_ratio = round(active_workers / float(max_workers), 4)

        return {
            "schema_version": 1,
            "generated_at_utc": now_utc.isoformat(),
            "component": "routine_manager",
            "max_workers": max_workers,
            "active_workers": active_workers,
            "saturation_ratio": saturation_ratio,
            "running_routine_ids": sorted(list(self._running)),
            "active_threads": sorted(thread_rows, key=lambda row: str(row.get("routine_id") or "")),
            "thresholds": {
                "warning_ratio": self._capacity_warn_ratio(config),
                "critical_ratio": self._capacity_critical_ratio(config),
                "alert_cooldown_seconds": self._capacity_alert_cooldown_seconds(config),
            },
        }

    def _publish_runtime_status(self, *, config: Dict[str, Any]) -> None:
        payload = self._build_runtime_status_payload(config=config)
        self._last_runtime_status = payload
        status_path = status_dir() / "resource_runtime_concurrency_status.json"
        write_json_file(status_path, payload)
        resource_manager = self._resource_manager
        if resource_manager is None:
            from app.assistant.ServiceLocator.service_locator import DI  # local import — shim only
            resource_manager = getattr(DI, "resource_manager", None)
        if resource_manager:
            resource_manager.update_resource("resource_runtime_concurrency_status", payload, persist=False)

    def get_runtime_concurrency_status(self) -> Dict[str, Any]:
        config = self._load_config()
        return self._build_runtime_status_payload(config=config)


def _set_running_state(
    entry: RoutineRunState,
    *,
    run_id: str,
    started_at_utc_iso: str,
    target_date: Optional[str],
    runner: str,
) -> None:
    entry.last_run_id = run_id
    entry.last_started_utc = started_at_utc_iso
    entry.last_finished_utc = None
    entry.last_duration_s = None
    entry.last_target_date = target_date
    entry.last_runner = runner
    entry.last_status = "running"
    entry.last_error = None


def _set_finished_state(
    entry: RoutineRunState,
    *,
    finished_at_utc_iso: str,
    duration_s: float,
    status: str,
    error: Optional[str],
    increment_run_count: bool,
) -> None:
    entry.last_finished_utc = finished_at_utc_iso
    entry.last_run_utc = finished_at_utc_iso
    entry.last_duration_s = duration_s
    entry.last_status = status
    entry.last_error = error if status != "success" else None
    if increment_run_count:
        entry.run_count += 1

_routine_manager: Optional[RoutineManager] = None


def get_routine_manager() -> RoutineManager:
    global _routine_manager
    if _routine_manager is None:
        from app.assistant.ServiceLocator.service_locator import DI  # resolved once here
        _routine_manager = RoutineManager(
            resource_manager=getattr(DI, "resource_manager", None),
            afk_monitor=getattr(DI, "afk_monitor", None),
        )
    return _routine_manager
