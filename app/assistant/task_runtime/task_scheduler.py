"""Task wake scheduling on the BASE timing engine — NOT dayflow's DayflowScheduler.

A parked task run (a node with wake_kind='time' and a future wake_at) gets a precise one-shot on the
SHARED base APScheduler (`DI.scheduler.timing_engine`, the same engine dayflow consumes — a peer, not
a dependency). When it fires, the task run resumes. Re-armed on boot from the durable task store.
Independent of dayflow: no DayflowScheduler, no dayflow store, no discriminator.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_TASK_WAKE_PREFIX = "task_wake::"
_CLOCK_LOCAL_RE = re.compile(r"^clock\.(local|after)\.(\d{1,2})_(\d{2})$")
_CLOCK_TIMER_RE = re.compile(r"^clock\.timer\.(\d+)([smh])$")


def _next_clock_fire(sub: str, now_utc):
    """When a clock.* subscription next fires, as an aware-UTC datetime; None if `sub` isn't a
    recognized clock event. clock.local.HH_MM -> next local HH:MM (tomorrow if passed);
    clock.after.HH_MM -> local HH:MM, firing IMMEDIATELY if already past (gate semantics);
    clock.timer.<N><s|m|h> -> now + N (relative to arm time = park time)."""
    m = _CLOCK_LOCAL_RE.match(sub)
    if m:
        kind, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
        local_now = datetime.now().astimezone()
        target = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= local_now:
            if kind == "after":
                return now_utc + timedelta(seconds=2)   # fire-if-past
            target = target + timedelta(days=1)
        return target.astimezone(timezone.utc)
    m = _CLOCK_TIMER_RE.match(sub)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return now_utc + timedelta(seconds=n * {"s": 1, "m": 60, "h": 3600}[unit])
    return None


def _base_scheduler():
    engine = getattr(getattr(DI, "scheduler", None), "timing_engine", None)
    return getattr(engine, "scheduler", None)


def arm_task_wake(store, work_id: str) -> None:
    """Arm a one-shot at this task run's soonest time-wake, on the base timing engine.
    Best-effort: if the engine isn't up (e.g. a headless/test context) the boot re-arm + backstop
    catch it — a task's wait state is durable on the node, not held by a live timer.

    A PAST-DUE wake (it came due during downtime, or between park and arm) fires at now+2s
    instead of being filtered out — filtering silently left the run permanently parked
    (verification finding: the boot re-arm's whole point is exactly this crossing)."""
    from datetime import timedelta
    from work_objects.model import utcnow
    try:
        wo = store.load(work_id)
    except Exception as e:
        logger.error("[task_scheduler] arm: cannot load %s: %s", work_id, e)
        return
    now = utcnow()
    wakes = [n.wake_at for n in wo.nodes.values()
             if n.wake_kind == "time" and n.wake_at is not None
             and n.status in ("proposed", "waiting", "actionable")]
    # clock.* subscriptions on event-parked nodes fire on the SAME timing engine, delivering the
    # subscription string as observed_event — the time side of a mixed gate (reply OR 22:00)
    # releases identically to a router match (verification finding F1: no deliverer existed).
    clock_subs: set[str] = set()
    for n in wo.nodes.values():
        if n.status not in ("proposed", "waiting", "actionable"):
            continue
        if n.wake_kind not in ("event", "signal", "user_reply"):
            continue
        for sub in (n.payload.get("subscriptions") or []):
            if str(sub).startswith("clock."):
                clock_subs.add(str(sub))
    if not wakes and not clock_subs:
        return
    scheduler = _base_scheduler()
    if scheduler is None:
        logger.warning("[task_scheduler] base timing engine unavailable — wake for %s not armed now.", work_id)
        return
    if wakes:
        soonest = min(wakes)
        run_date = soonest if soonest > now else now + timedelta(seconds=2)
        try:
            scheduler.add_job(func=_fire_task_wake, trigger="date", run_date=run_date, args=[work_id],
                              id=f"{_TASK_WAKE_PREFIX}{work_id}", replace_existing=True, misfire_grace_time=600)
            logger.info("[task_scheduler] armed task wake %s at %s%s", work_id, run_date.isoformat(),
                        " (past-due — firing now)" if soonest <= now else "")
        except Exception as e:
            logger.error("[task_scheduler] failed to arm wake for %s: %s", work_id, e)
    for sub in sorted(clock_subs):
        fire_at = _next_clock_fire(sub, now)
        if fire_at is None:
            # unrecognized clock event on a parked node = that side of the gate can never fire —
            # loud, per-sub (siblings still arm; the run may still release via a router match)
            logger.error("[task_scheduler] %s subscribes to unrecognized clock event %r — "
                         "that wake will NOT fire.", work_id, sub)
            continue
        try:
            scheduler.add_job(func=_fire_task_wake, trigger="date", run_date=fire_at,
                              args=[work_id, sub],
                              id=f"{_TASK_WAKE_PREFIX}{work_id}::{sub}", replace_existing=True,
                              misfire_grace_time=600)
            logger.info("[task_scheduler] armed clock wake %s %s at %s", work_id, sub, fire_at.isoformat())
        except Exception as e:
            logger.error("[task_scheduler] failed to arm clock wake %s for %s: %s", sub, work_id, e)


def _fire_task_wake(work_id: str, observed_event: str | None = None, is_retry: bool = False) -> None:
    """A task run's wake fired: resume it, in the app context (like dayflow's fire handler runs in
    one). A clock wake carries its subscription string as `observed_event` so the release matches a
    router-delivered event exactly. The wake is a consumed one-shot, so a resume failure re-arms
    ONE delayed retry (+300s); if that also fails, the durable node state remains and the next boot
    re-arm is the backstop — the run is never lost, only late (verification finding: fire-failure
    consumed the wake forever)."""
    from work_objects.model import utcnow
    from app.assistant.task_runtime.entry import resume_task_run
    app = getattr(getattr(DI, "scheduler", None), "app", None)
    try:
        if app is not None:
            with app.app_context():
                resume_task_run(work_id, observed_event=observed_event)
        else:
            resume_task_run(work_id, observed_event=observed_event)
    except Exception as e:
        logger.error("[task_scheduler] fire/resume %s (event=%s) failed%s: %s",
                     work_id, observed_event, " (retry)" if is_retry else "", e)
        if is_retry:
            logger.error("[task_scheduler] %s retry also failed — the run stays parked until the "
                         "next boot re-arm.", work_id)
            return
        scheduler = _base_scheduler()
        if scheduler is None:
            return
        retry_id = f"{_TASK_WAKE_PREFIX}{work_id}::{observed_event}" if observed_event \
            else f"{_TASK_WAKE_PREFIX}{work_id}"
        try:
            scheduler.add_job(func=_fire_task_wake, trigger="date",
                              run_date=utcnow() + timedelta(seconds=300),
                              args=[work_id, observed_event, True],
                              id=retry_id, replace_existing=True, misfire_grace_time=600)
            logger.info("[task_scheduler] re-armed one retry for %s at +300s", work_id)
        except Exception as arm_err:
            logger.error("[task_scheduler] could not re-arm retry for %s: %s", work_id, arm_err)


def re_arm_parked_task_runs() -> None:
    """Boot: re-arm wakes for every non-terminal task run from the durable task store (re-derivation —
    the task store survives restart; a parked run's wake is re-armed, not lost with an in-memory timer)."""
    from app.assistant.task_runtime.task_store import get_task_work_store
    store = get_task_work_store()
    try:
        summaries = store.list_work_objects()
    except Exception as e:
        logger.error("[task_scheduler] boot re-arm: cannot list task work objects: %s", e)
        return
    for s in summaries:
        if str(s.get("status") or "") in ("done", "abandoned"):
            continue
        arm_task_wake(store, str(s.get("id")))
