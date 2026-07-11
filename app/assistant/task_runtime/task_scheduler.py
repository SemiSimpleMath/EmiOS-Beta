"""Task wake scheduling on the BASE timing engine — NOT dayflow's DayflowScheduler.

A parked task run (a node with wake_kind='time' and a future wake_at) gets a precise one-shot on the
SHARED base APScheduler (`DI.scheduler.timing_engine`, the same engine dayflow consumes — a peer, not
a dependency). When it fires, the task run resumes. Re-armed on boot from the durable task store.
Independent of dayflow: no DayflowScheduler, no dayflow store, no discriminator.
"""
from __future__ import annotations

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_TASK_WAKE_PREFIX = "task_wake::"


def _base_scheduler():
    engine = getattr(getattr(DI, "scheduler", None), "timing_engine", None)
    return getattr(engine, "scheduler", None)


def arm_task_wake(store, work_id: str) -> None:
    """Arm a one-shot at this task run's soonest future time-wake, on the base timing engine.
    Best-effort: if the engine isn't up (e.g. a headless/test context) the boot re-arm + backstop
    catch it — a task's wait state is durable on the node, not held by a live timer."""
    from work_objects.model import utcnow
    try:
        wo = store.load(work_id)
    except Exception as e:
        logger.error("[task_scheduler] arm: cannot load %s: %s", work_id, e)
        return
    now = utcnow()
    futures = [n.wake_at for n in wo.nodes.values()
               if n.wake_kind == "time" and n.wake_at is not None and n.wake_at > now
               and n.status in ("proposed", "waiting", "actionable")]
    if not futures:
        return
    scheduler = _base_scheduler()
    if scheduler is None:
        logger.warning("[task_scheduler] base timing engine unavailable — wake for %s not armed now.", work_id)
        return
    run_date = min(futures)
    try:
        scheduler.add_job(func=_fire_task_wake, trigger="date", run_date=run_date, args=[work_id],
                          id=f"{_TASK_WAKE_PREFIX}{work_id}", replace_existing=True, misfire_grace_time=600)
        logger.info("[task_scheduler] armed task wake %s at %s", work_id, run_date.isoformat())
    except Exception as e:
        logger.error("[task_scheduler] failed to arm wake for %s: %s", work_id, e)


def _fire_task_wake(work_id: str) -> None:
    """A task run's time-wake fired: resume it, in the app context (like dayflow's fire handler runs in one)."""
    from app.assistant.task_runtime.entry import resume_task_run
    app = getattr(getattr(DI, "scheduler", None), "app", None)
    try:
        if app is not None:
            with app.app_context():
                resume_task_run(work_id)
        else:
            resume_task_run(work_id)
    except Exception as e:
        logger.error("[task_scheduler] fire/resume %s failed: %s", work_id, e)


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
