"""The brain runs when something happened, not on a clock (2026-09-30).

Owner, 2026-09-30: "I prefer it to be event driven", and "if I am quiet in the chat for that long I am
not interacting with it anymore. then you can analyze and if no more chat or items come in there is
no reason to run the brain until something happens again." Until then the brain_gate routine ran
every five minutes and read chat in five-minute slices while the conversation was still going.

A wake runs `run_brain` once: new chat and email into the inbox, the gate routes what is ready (an
email at once, a room's messages once the room has been quiet for brain_inbox.QUIET), the brain
decides each matter, the briefs of changed concerns are rewritten, and concerns ready to act on are
handed to dayflow. Each stage is free when it has nothing to do.

What wakes it:
- the ingest service (the gut) hands it every new chat message and email (`handle_envelope`);
- a write to the concern register from outside the brain (a work outcome, the noticer, an edit)
  calls `poke`, so the concern's brief and handoff follow it;
- a room still talking: the wake sleeps until that room goes quiet, then runs;
- a brief holding until a time: the wake sleeps until the earliest hold, when the brief is written
  again (concern_brief.next_hold_at);
- boot: one run at start picks up whatever arrived while the app was down.
Nothing else runs it. The wake's own register writes do not wake it again. A run that fails is
logged and not retried; the next thing that happens wakes it.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_poked = threading.Event()
_inside = threading.local()        # set on the wake's own thread: its register writes do not poke
_thread: Optional[threading.Thread] = None
_start_lock = threading.Lock()


def run_brain() -> Dict[str, Any]:
    """One pass: gate, brain step, briefs, handoffs."""
    from app.assistant.subconscious.brain_step import run_brain_step
    from app.assistant.subconscious.concern_brief import run_briefs
    from app.assistant.subconscious.concern_handoff import run_handoffs
    from app.assistant.subconscious.gate import run_gate
    from app.assistant.subconscious.concern_brief import next_hold_at
    from app.assistant.subconscious.concern_store import load_register
    gate = run_gate()
    brain = run_brain_step()
    briefs = run_briefs()
    handoffs = run_handoffs()
    due = [t for t in (gate["next_ready_at"], next_hold_at(load_register(), datetime.now(timezone.utc)))
           if t is not None]
    return {**gate, **{f"brain_{k}": v for k, v in brain.items()}, **briefs, **handoffs,
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
    _inside.run = True
    while True:
        _poked.clear()
        wait: Optional[float] = None
        try:
            summary = run_brain()
            logger.info("[brain_wake] ran: %s", summary)
            due = summary.get("next_wake_at")
            if due is not None:
                wait = max(0.0, (due - datetime.now(timezone.utc)).total_seconds())
        except Exception as exc:
            logger.error("[brain_wake] the brain run failed; it runs again on the next event: %s", exc,
                         exc_info=True)
        _poked.wait(timeout=wait)


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
