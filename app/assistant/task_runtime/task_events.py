"""The task event lane: watch registration → watch-match delivery → resume.

The compiler stamps wait nodes with `payload.watch_registration(s)` and `subscriptions`; this
module is what makes them LIVE (verification finding F1/F6: nothing registered the watches and
nothing subscribed to `signal_router_watch_match` after the task-IR runner was retired — every
event-wait task parked forever).

Three pieces:
- `register_task_watches(store, work_id)` — at run start: register each node's watch payload(s)
  with the signal router, with the watch_key INSTANCE-scoped (`task::<work_id>::<node_id>`) so
  concurrent runs of one template can't collide and the match handler can resolve the run from
  the key alone. Fails LOUDLY if the template needs watches but the router isn't available —
  a run whose waits could never fire must not start.
- `register_task_event_bridge()` — at boot: subscribe the match handler to the hub topic
  `signal_router_watch_match`. A match for a `task::` watch resumes the run on its own thread
  with `observed_event=<event_name>` (wake-promotion does the rest).
- `cancel_task_watches(work_id)` — at a run's terminal (done/failed/stalled): cancel the run's
  watches by prefix so a dead run stops consuming matches.

Time-flavored subscriptions (`clock.local.HH_MM` / `clock.after.HH_MM` / `clock.timer.<N><u>`)
are armed by task_scheduler on the base timing engine — the fire delivers the SAME observed_event
string, so a mixed gate (reply OR 22:00) releases identically from either side.
"""
from __future__ import annotations

import threading

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_WATCH_PREFIX = "task::"
_CLOCK_PREFIX = "clock."


def _router():
    return getattr(DI, "signal_router", None)


def _node_watch_payloads(node) -> list:
    regs = []
    if node.payload.get("watch_registration"):
        regs.append(node.payload["watch_registration"])
    if node.payload.get("watch_registrations"):
        regs.extend(node.payload["watch_registrations"])
    return [r for r in regs if isinstance(r, dict)]


def register_task_watches(store, work_id: str) -> int:
    """Register every node-carried watch for this run, instance-scoped. Returns the count.
    Raises if watches are needed but the signal router is unavailable (fail loud: the run's
    event waits would silently never fire)."""
    from work_objects.model import _TERMINAL_STATUSES
    wo = store.load(work_id)
    wanted = []
    for n in wo.nodes.values():
        if n.status in _TERMINAL_STATUSES:
            continue
        for reg in _node_watch_payloads(n):
            wanted.append((n.id, reg))
    if not wanted:
        return 0

    router = _router()
    if router is None:
        raise RuntimeError(
            f"task run {work_id} carries {len(wanted)} watch registration(s) but the signal "
            "router is unavailable — its event waits could never fire, refusing to start")

    from app.assistant.signal_router.contracts import WatchRegistrationRequest
    registered = 0
    for node_id, reg in wanted:
        event_name = str(reg.get("event_name") or "").strip()
        watch_type = str(reg.get("watch_type") or "").strip()
        predicate = reg.get("predicate") if isinstance(reg.get("predicate"), dict) else {}
        if not event_name or not watch_type or not predicate:
            raise ValueError(
                f"task run {work_id} node {node_id}: malformed watch_registration "
                f"(event_name={event_name!r}, watch_type={watch_type!r}, predicate keys="
                f"{sorted(predicate.keys())})")
        metadata = dict(reg.get("metadata") or {})
        metadata["work_id"] = work_id
        metadata["node_id"] = node_id
        router.register_watch(request=WatchRegistrationRequest(
            watch_key=f"{_WATCH_PREFIX}{work_id}::{node_id}",
            event_name=event_name,
            watch_type=watch_type,
            predicate=predicate,
            dedupe_window_seconds=int(reg.get("dedupe_window_seconds") or 300),
            metadata=metadata,
        ))
        registered += 1
    logger.info("[task_events] registered %d watch(es) for %s", registered, work_id)
    return registered


def cancel_task_watches(work_id: str) -> int:
    """Cancel this run's watches (terminal cleanup). Best-effort: a missing router means
    nothing was ever registered."""
    router = _router()
    if router is None:
        return 0
    try:
        cancelled = router.cancel_watches_by_prefix(prefix=f"{_WATCH_PREFIX}{work_id}::")
        if cancelled:
            logger.info("[task_events] cancelled %d watch(es) for terminal run %s", cancelled, work_id)
        return cancelled
    except Exception as e:
        logger.error("[task_events] cancel watches for %s failed: %s", work_id, e)
        return 0


def _resume_from_match(work_id: str, event_name: str) -> None:
    from app.assistant.task_runtime.entry import resume_task_run
    app = getattr(getattr(DI, "scheduler", None), "app", None)
    try:
        if app is not None:
            with app.app_context():
                resume_task_run(work_id, observed_event=event_name)
        else:
            resume_task_run(work_id, observed_event=event_name)
    except Exception as e:
        logger.error("[task_events] resume %s on %s failed: %s", work_id, event_name, e)


def _spawn_resume(work_id: str, event_name: str) -> None:
    """Run the resume on its own thread — a task drive must not block the hub dispatcher.
    (A seam: tests replace this to run synchronously without touching the shared threading
    module, which ThreadPoolExecutor also uses.)"""
    threading.Thread(target=_resume_from_match, args=(work_id, event_name),
                     name=f"task-resume-{work_id[-6:]}", daemon=True).start()


def deliver_watch_match(message) -> None:
    """Hub handler for `signal_router_watch_match`: a match on a `task::` watch resumes its run
    with the observed event. Non-task watches are ignored (other consumers own them)."""
    data = getattr(message, "data", None)
    if not isinstance(data, dict):
        return
    watch_key = str(data.get("watch_key") or "")
    if not watch_key.startswith(_WATCH_PREFIX):
        return
    parts = watch_key.split("::")
    if len(parts) < 3 or not parts[1]:
        logger.error("[task_events] malformed task watch_key %r — cannot resolve the run", watch_key)
        return
    work_id = parts[1]
    event_name = str(data.get("event_name") or "").strip()
    if not event_name:
        logger.error("[task_events] watch match for %s carries no event_name", work_id)
        return
    logger.info("[task_events] watch match %s -> resume %s", event_name, work_id)
    _spawn_resume(work_id, event_name)


def register_task_event_bridge() -> None:
    """Boot: wire the match handler onto the hub. Idempotent (the hub dedupes handlers)."""
    DI.event_hub.register_event("signal_router_watch_match", deliver_watch_match)
    logger.info("[task_events] task watch-match bridge registered on the event hub")
