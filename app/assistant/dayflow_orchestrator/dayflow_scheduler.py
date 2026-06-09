"""
DayflowScheduler — Event-driven scheduling for the dayflow orchestrator.

Instead of polling on a fixed interval, the scheduler listens for events
that indicate new data is available (emails, calendar changes, user ticket
responses, AFK return) and runs the orchestrator after a debounce window.

Guarantees:
- At most one orchestrator tick runs at a time (mutual exclusion).
- Minimum gap between runs (MIN_GAP_SECONDS) is enforced.
- Rapid-fire pokes are debounced (DEBOUNCE_SECONDS).
- Uses APScheduler one-shot jobs for precise timing.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import setup_complete
from app.assistant.utils.pydantic_classes import Message
from app.services.scheduler_heartbeat import record_tick

logger = get_logger(__name__)

JOB_ID = "dayflow_scheduler_next_tick"
DEBOUNCE_SECONDS = 60
MIN_GAP_SECONDS = 120          # mutual-exclusion floor; applies to scheduled-item ticks
POKE_MIN_INTERVAL_SECONDS = 600  # delta/poke wakes throttle to >= this since last run (10 min)
MAX_CEILING_SECONDS = 1800

# A waiting/watching item whose reactivate_at_utc is overdue by more than
# this is treated as broken-and-stuck — it won't drag the scheduler into a
# 2-minute hot loop. The item stays in its current state (a separate
# sweeper / dispatcher fix is needed to actually resolve them); the
# scheduler just stops using it as wake-up bait.
ANCIENT_ITEM_OVERDUE_SECONDS = 24 * 3600  # 24 hours


class DayflowScheduler:

    # Surface an owner-visible ticket once dayflow ticks have failed this many times in a row.
    _FAILURE_NOTIFY_THRESHOLD = 3

    def __init__(self, *, timing_engine: Any, app: Any) -> None:
        self._timing_engine = timing_engine
        self._scheduler = timing_engine.scheduler
        self._app = app

        self._lock = threading.Lock()
        self._running = False
        self._last_run_finished_utc: Optional[datetime] = None
        self._pending_poke_reason: Optional[str] = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._subscribe_events()
        self._schedule_tick(delay_seconds=10, reason="startup")
        logger.info("[DayflowScheduler] Started — initial tick in 10s")

    def stop(self) -> None:
        self._started = False
        try:
            self._scheduler.remove_job(JOB_ID)
        except Exception as e:
            logger.error("[DayflowScheduler] Failed to remove scheduled job during stop: %s", e)
            logger.debug("[DayflowScheduler] stop exception details", exc_info=True)
            raise
        logger.info("[DayflowScheduler] Stopped")

    def poke(self, reason: str = "unknown") -> None:
        """
        Signal that new data is available and the orchestrator should run.

        The actual run is debounced: if multiple pokes arrive within
        DEBOUNCE_SECONDS, only one run fires after the window.
        """
        if not self._started:
            return

        with self._lock:
            if self._running:
                self._pending_poke_reason = reason
                logger.info(
                    "[DayflowScheduler] Poked (%s) while running — will re-run after current tick",
                    reason,
                )
                return

        self._schedule_tick(delay_seconds=DEBOUNCE_SECONDS, reason=reason, is_poke=True)

    def _schedule_tick(self, *, delay_seconds: float, reason: str,
                       triggered_item_id: Optional[str] = None, is_poke: bool = False) -> None:
        now_utc = datetime.now(timezone.utc)

        with self._lock:
            # Delta/poke wakes throttle to POKE_MIN_INTERVAL_SECONDS since the last
            # run (don't react instantly to every chat/email). Scheduled-item wakes
            # only respect the small MIN_GAP (mutual exclusion), so a job due at time
            # T still fires at T. Reaction to a delta = max(debounce, floor - age),
            # so it's only near-10-min when something *just* ran.
            floor_gap = POKE_MIN_INTERVAL_SECONDS if is_poke else MIN_GAP_SECONDS
            if self._last_run_finished_utc is not None:
                earliest = self._last_run_finished_utc + timedelta(seconds=floor_gap)
                candidate = now_utc + timedelta(seconds=delay_seconds)
                run_date = max(candidate, earliest)
            else:
                run_date = now_utc + timedelta(seconds=delay_seconds)

        # Never push an already-scheduled, sooner run later. A throttled poke must
        # not clobber a scheduled item tick that's due sooner — the item tick does
        # full ingestion, so it also picks up the pending delta.
        try:
            existing = self._scheduler.get_job(JOB_ID)
            if existing is not None and existing.next_run_time is not None \
                    and existing.next_run_time <= run_date:
                logger.info(
                    "[DayflowScheduler] Keeping sooner scheduled run %s (reason=%s would be %s)",
                    existing.next_run_time.isoformat(), reason, run_date.isoformat(),
                )
                return
        except Exception:
            pass  # if we can't read the existing job, just schedule normally

        try:
            self._scheduler.add_job(
                func=self._execute_tick,
                trigger="date",
                run_date=run_date,
                args=[reason, triggered_item_id],
                id=JOB_ID,
                replace_existing=True,
                misfire_grace_time=300,
            )
            delta = (run_date - now_utc).total_seconds()
            logger.info(
                "[DayflowScheduler] Scheduled tick in %.0fs (reason=%s, run_date=%s)",
                delta, reason, run_date.isoformat(),
            )
        except Exception as e:
            logger.error("[DayflowScheduler] Failed to schedule tick: %s", e)
            logger.debug("[DayflowScheduler] schedule tick exception details", exc_info=True)
            raise

    def _maybe_notify_repeated_failure(
        self, *, consecutive_errors: int, error: Exception, run_id: str,
    ) -> None:
        """When dayflow ticks fail repeatedly, surface ONE owner-visible ticket so the autonomy layer
        doesn't die silently. Fires only AT the threshold crossing (de-duped — the health surface
        carries the persistent count). Uses the ticket manager's direct write, which does NOT run the
        dayflow pipeline, so it works even when that pipeline is the thing failing. Best-effort: a
        notify failure must never break the tick's own error handling.
        """
        if consecutive_errors != self._FAILURE_NOTIFY_THRESHOLD:
            return
        msg = (
            f"The dayflow orchestrator has failed {consecutive_errors} ticks in a row "
            f"(last run_id={run_id}). Autonomy (proactive nudges, item processing) is degraded until "
            f"this clears. Last error: {str(error)[:300]}"
        )
        logger.error("[DayflowScheduler] %s", msg)
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            tm = getattr(DI, "ticket_manager", None)
            if tm is None:
                return
            ticket = tm.create_ticket(
                ticket_type="dayflow_notify",
                suggestion_type="dayflow_scheduler_failure",
                title="Dayflow autonomy is failing",
                message=msg,
                action_type="none",
                trigger_context={
                    "consecutive_errors": consecutive_errors,
                    "run_id": run_id,
                    "last_error": str(error)[:500],
                },
                trigger_reason="dayflow_scheduler_repeated_tick_failure",
                valid_hours=24,
            )
            if ticket is not None and hasattr(tm, "mark_proposed"):
                tm.mark_proposed(ticket.ticket_id)
        except Exception as notify_err:
            logger.error(
                "[DayflowScheduler] Failed to surface repeated-tick-failure ticket: %s", notify_err,
            )
            logger.debug("[DayflowScheduler] repeated-failure notify exception details", exc_info=True)

    def _execute_tick(self, reason: str, triggered_item_id: Optional[str] = None) -> None:
        if not setup_complete():
            logger.info("[DayflowScheduler] Setup not complete; skipping tick.")
            return

        try:
            from app.models.db_manager import get_db_manager
            from app.assistant.database.db_handler import UnifiedLog2026
            with get_db_manager().read_session() as session:
                has_history = session.query(UnifiedLog2026.id).limit(1).scalar() is not None
            if not has_history:
                logger.info("[DayflowScheduler] No chat history yet; skipping tick.")
                return
        except Exception as e:
            logger.debug("[DayflowScheduler] History check failed (skipping tick): %s", e, exc_info=True)
            return

        with self._lock:
            if self._running:
                self._pending_poke_reason = reason
                logger.info(
                    "[DayflowScheduler] Tick skipped (already running), queued poke: %s",
                    reason,
                )
                return
            self._running = True
            self._pending_poke_reason = None

        run_id = uuid.uuid4().hex[:8]
        fast_tick = reason == "item_timer" and bool(triggered_item_id)
        logger.info(
            "[DayflowScheduler] === TICK START === run_id=%s reason=%s fast_tick=%s triggered_item_id=%s",
            run_id, reason, fast_tick, triggered_item_id or "-",
        )
        try:
            with self._app.app_context():
                from app.assistant.dayflow_orchestrator.dayflow_tick import (
                    dayflow_orchestrator_cadence_tick,
                )
                dayflow_orchestrator_cadence_tick(
                    routine=_SchedulerRoutineStub(run_id),
                    wake_reason=reason,
                    fast_tick=fast_tick,
                    triggered_item_id=triggered_item_id,
                )
            record_tick("dayflow_scheduler", ok=True)
        except Exception as e:
            # No-silent-death (R3): APScheduler swallows the re-raise below, so without this a failing
            # dayflow tick would lose autonomy with NO signal. Record the failure for the health
            # surface and, on repeated failures, surface an owner-visible ticket.
            consecutive = record_tick("dayflow_scheduler", ok=False, error=e)
            logger.error(
                "[DayflowScheduler] Tick failed run_id=%s (consecutive=%d): %s", run_id, consecutive, e,
            )
            logger.debug("[DayflowScheduler] tick exception details", exc_info=True)
            self._maybe_notify_repeated_failure(consecutive_errors=consecutive, error=e, run_id=run_id)
            raise
        finally:
            with self._lock:
                self._running = False
                self._last_run_finished_utc = datetime.now(timezone.utc)
                pending = self._pending_poke_reason
                self._pending_poke_reason = None

            logger.info("[DayflowScheduler] === TICK END === run_id=%s", run_id)

            # Always (re)schedule the next item tick first so scheduled jobs fire on
            # time. If a delta arrived during the run, also schedule a throttled poke
            # — the "don't clobber a sooner run" guard keeps whichever is earlier, so
            # a due item still wins and the delta is absorbed by it.
            self._schedule_next_from_items()
            if pending:
                logger.info(
                    "[DayflowScheduler] Delta queued during run; scheduling throttled poke: %s", pending,
                )
                self._schedule_tick(delay_seconds=DEBOUNCE_SECONDS, reason=f"queued:{pending}", is_poke=True)

    def _subscribe_events(self) -> None:
        from app.assistant.ServiceLocator.service_locator import DI

        event_hub = DI.event_hub

        event_hub.register_event("repo_update", self._on_repo_update)
        event_hub.register_event("afk_state_changed", self._on_afk_state_changed)
        event_hub.register_event("dayflow_ticket_responded", self._on_ticket_responded)

        logger.info("[DayflowScheduler] Subscribed to event hub topics")

    def _schedule_next_from_items(self) -> None:
        """
        Scan dayflow items for the earliest reactivate_at_utc and schedule
        a tick at that time. If nothing is pending, schedule the ceiling tick.
        """
        try:
            from app.assistant.dayflow_orchestrator.state_store import load_existing_dayflow_items

            now_utc = datetime.now(timezone.utc)
            earliest: Optional[datetime] = None
            earliest_item_id: Optional[str] = None
            ancient_skipped = 0

            items = load_existing_dayflow_items()
            for item in items:
                meta = get_meta(item)
                state = str(meta.get("state") or "").strip().lower()
                if state not in ("waiting", "watching"):
                    continue
                raw = str(meta.get("reactivate_at_utc") or "").strip()
                if not raw:
                    continue
                try:
                    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    parsed = parsed.astimezone(timezone.utc)
                except Exception as e:
                    logger.error(
                        "[DayflowScheduler] Invalid reactivate_at_utc for item_id=%s: %r (%s)",
                        meta.get("item_id", "?"),
                        raw,
                        e,
                    )
                    logger.debug("[DayflowScheduler] reactivate_at_utc parse exception details", exc_info=True)
                    raise
                item_id = str(meta.get("item_id") or item.get("id") or "").strip()
                if parsed <= now_utc:
                    overdue_seconds = (now_utc - parsed).total_seconds()
                    if overdue_seconds > ANCIENT_ITEM_OVERDUE_SECONDS:
                        # Ancient stale item — should have fired long ago and
                        # didn't. Treat as broken; do NOT let it drive a
                        # 2-minute reschedule loop. Keep scanning for items
                        # that are either not stale or only-recently-overdue.
                        ancient_skipped += 1
                        logger.warning(
                            "[DayflowScheduler] Ignoring ancient stale item "
                            "(overdue %.1fh) item_id=%s state=%s reactivate_at_utc=%s",
                            overdue_seconds / 3600.0,
                            item_id or "?",
                            state,
                            raw,
                        )
                        continue
                    earliest = now_utc + timedelta(seconds=MIN_GAP_SECONDS)
                    earliest_item_id = item_id
                    break
                if earliest is None or parsed < earliest:
                    earliest = parsed
                    earliest_item_id = item_id
            if ancient_skipped:
                logger.warning(
                    "[DayflowScheduler] Skipped %d ancient stale item(s) when scheduling — "
                    "they are stuck in waiting/watching and need dispatcher attention.",
                    ancient_skipped,
                )

            if earliest is not None:
                delay = max(MIN_GAP_SECONDS, (earliest - now_utc).total_seconds())
                delay = min(delay, MAX_CEILING_SECONDS)
                self._schedule_tick(
                    delay_seconds=delay,
                    reason="item_timer",
                    triggered_item_id=earliest_item_id,
                )
                logger.info(
                    "[DayflowScheduler] Next item timer in %.0fs (%s) item=%s",
                    delay, earliest.isoformat(), earliest_item_id or "?",
                )
            else:
                self._schedule_tick(delay_seconds=MAX_CEILING_SECONDS, reason="ceiling")
                logger.info(
                    "[DayflowScheduler] No item timers found — ceiling tick in %ds",
                    MAX_CEILING_SECONDS,
                )
        except Exception as e:
            logger.error("[DayflowScheduler] Failed scanning items for next wake: %s", e)
            logger.debug("[DayflowScheduler] item scan exception details", exc_info=True)
            raise

    # Data types that justify waking the orchestrator.
    _ACTIONABLE_REPO_TYPES = {"email", "calendar", "todo_task", "scheduler_events"}

    def _on_repo_update(self, message: Message) -> None:
        data_type = str(getattr(message, "content", "") or "").strip().lower()
        if data_type not in self._ACTIONABLE_REPO_TYPES:
            return
        self.poke(reason=f"repo_update:{data_type}")

    def _on_afk_state_changed(self, message: Message) -> None:
        content = str(getattr(message, "content", "") or "").strip().lower()
        if content == "active":
            self.poke(reason="afk_returned")

    def _on_ticket_responded(self, message: Message) -> None:
        self.poke(reason="ticket_responded")


class _SchedulerRoutineStub:
    """Mimics the routine object that dayflow_orchestrator_cadence_tick expects."""

    def __init__(self, run_id: str) -> None:
        self.routine_id = f"dayflow_scheduler::{run_id}"
