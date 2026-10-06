"""The brain runs when something happened, not on a clock (2026-09-30).

Owner, 2026-09-30: "I prefer it to be event driven", and "if I am quiet in the chat for that long I am
not interacting with it anymore. then you can analyze and if no more chat or items come in there is
no reason to run the brain until something happens again." Until then the brain_gate routine ran
every five minutes and read chat in five-minute slices while the conversation was still going.

A wake runs `run_brain` once: new chat and email into the inbox, the gate routes what is ready (an
email at once, a room's messages once the room has been quiet for brain_inbox.QUIET), the brain
decides each matter, the briefs of changed concerns are rewritten, the concern planner works out what
each changed matter concern needs (subconscious/concern_planner.py), and concerns ready to act on are
handed to dayflow. Each stage is free when it has nothing to do.

What wakes it:
- the ingest service (the gut) hands it every new chat message and email (`handle_envelope`);
- a write to the concern register from outside the brain (a work outcome, the noticer, an edit)
  calls `poke`, so the concern's brief and handoff follow it;
- a room still talking: the wake sleeps until that room goes quiet, then runs;
- a plan holding until a time: the wake sleeps until the earliest hold, when the concern is planned
  again (concern_planner.next_hold_at);
- boot: one run at start picks up whatever arrived while the app was down;
- the heartbeat (2026-10-01): a run at least every HEARTBEAT_SECONDS, for what changes without an
  event, such as the inputs of the day pass (subconscious/day_pass.py) and the start of a new day.
Nothing else runs it. Each pass is recorded as a run (subconscious/brain_trace.py) with what started it:
boot, an event (a poke) or the timer (a hold or a quiet room coming due, or the heartbeat). The wake's own register writes do not wake it again. A run that fails is
logged and not retried; the next thing that happens wakes it.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

HEARTBEAT_SECONDS = 600

_poked = threading.Event()
_inside = threading.local()        # set on the wake's own thread: its register writes do not poke
_thread: Optional[threading.Thread] = None
_start_lock = threading.Lock()


def run_brain() -> Dict[str, Any]:
    """One pass: gate, brain step, the entries of closed concerns cancelled, briefs, the concern planner, handoffs,
    the day pass and its occurrences, the stories, and the day's tasks."""
    from app.assistant.subconscious.brain_step import run_brain_step
    from app.assistant.subconscious.concern_brief import run_briefs
    from app.assistant.subconscious.concern_handoff import run_handoffs
    from app.assistant.subconscious import concern_planner
    from app.assistant.subconscious.day_pass import run_day_pass
    from app.assistant.subconscious.gate import run_gate
    from app.assistant.subconscious.concern_store import load_register
    from app.assistant.subconscious.belief_occurrences import run_occurrence_handoffs
    gate = run_gate()
    brain = run_brain_step()
    # A concern the brain (or the noticer) closed takes its open list entries with it, so dayflow ends their work.
    from app.assistant.subconscious import brain_list
    closed_entries = brain_list.cancel_for_closed_concerns(load_register(), at=datetime.now(timezone.utc))
    briefs = run_briefs()
    plans = concern_planner.run_planners()
    handoffs = run_handoffs()
    register, now = load_register(), datetime.now(timezone.utc)
    due = [t for t in (gate["next_ready_at"], concern_planner.next_hold_at(register, now)) if t is not None]
    # Last: a day pass that fails does not hold up the concerns' briefs and handoffs. Today's calendar is
    # handed to dayflow one placement at a time (subconscious/belief_occurrences.py).
    from app.assistant.subconscious.brain_trace import trace
    with trace(stage="day_pass"):
        day = run_day_pass()
        occurrences = run_occurrence_handoffs()
    # The owner's stories of what changed (subconscious/brain_stories.py), told from the records above.
    from app.assistant.subconscious.brain_stories import run_stories
    with trace(stage="stories"):
        stories = run_stories()
    # Last: the day's tasks (subconscious/day_tasks.py; docs/design/brain_delegates_work_2026-10-04.md), which read
    # the concern entries the planner made earlier in this pass.
    from app.assistant.subconscious.day_tasks import run_day_tasks
    with trace(stage="day_tasks"):
        decided = run_day_tasks()
    # A live lane takes its work from the list: the planning tick makes it, so the tick is asked to run when the
    # list changed (dayflow_orchestrator/brain_list_intake.py).
    from app.assistant.dayflow_orchestrator.brain_list_intake import lanes
    config = lanes()
    if (closed_entries or plans.get("plans_written") or decided.get("day_list_changes")) and "live" in (
            config["calendar"]["mode"], config["concern"]["mode"]):
        from app.assistant.subconscious.concern_handoff import _poke_dayflow
        _poke_dayflow()
    return {**gate, **{f"brain_{k}": v for k, v in brain.items()}, **briefs, **plans, **handoffs, **day, **occurrences,
            **stories, **decided, "closed_concern_entries": closed_entries,
            "next_wake_at": min(due) if due else None}


def poke() -> None:
    """Something happened: run the brain. A poke from the wake's own run is ignored."""
    if getattr(_inside, "run", False):
        return
    _poked.set()


def handle_envelope(envelope: Any) -> None:
    """The gut's subscriber: every new chat message or email wakes the brain."""
    poke()


def _loop() -> None:
    from app.assistant.subconscious import brain_trace
    _inside.run = True
    trigger = "boot"
    while True:
        _poked.clear()
        wait: float = HEARTBEAT_SECONDS
        try:
            with brain_trace.run(trigger) as recorded:
                summary = run_brain()
                recorded["summary"] = summary
            logger.info("[brain_wake] ran: %s", summary)
            due = summary.get("next_wake_at")
            if due is not None:
                wait = min(wait, max(0.0, (due - datetime.now(timezone.utc)).total_seconds()))
        except Exception as exc:
            logger.error("[brain_wake] the brain run failed; it runs again on the next event or heartbeat: %s",
                         exc, exc_info=True)
        trigger = "event" if _poked.wait(timeout=wait) else "timer"


def start() -> None:
    """Start the wake thread and run once for whatever arrived while the app was down."""
    global _thread
    from app.assistant.runtime import start_monitored_thread
    with _start_lock:
        if _thread is not None and _thread.is_alive():
            raise RuntimeError("brain_wake already started")
        _thread = start_monitored_thread(owner="brain", name="brain-wake", target=_loop, daemon=True,
                                         kind="service_loop", metadata={"component": "brain_wake"})
    logger.info("[brain_wake] started")
