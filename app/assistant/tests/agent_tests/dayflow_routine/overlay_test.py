"""
Controlled overlay test for dayflow_routine_writer.

Pins a FIXED schedule + belief set + a fixed "now", runs the writer, and checks
that the output is an OVERLAY on the schedule — belief-grounded logistics
attached to the right schedule anchors — NOT a second schedule.

The scenario reproduces the cooling-belief bug: now = 05:57, so the 6:00 AM
"stop cooling / set ~75F" step falls inside the opening ("Now -> ...") ramp,
and the 9:00 PM "AC to 70F" step is a later-today tail item.

Run from repo root:
    python app/assistant/tests/agent_tests/dayflow_routine/overlay_test.py
Add --dry to only assemble + print the prompt inputs (no LLM call).
"""
from __future__ import annotations

import sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.pipelines.dayflow.steps.dayflow_routine_stage import _format_daily_context
from app.assistant.scope.loader import load_scope_for_source
from app.assistant.utils.pydantic_classes import Message

# ---- fixed clock: Wednesday 2026-06-17, local PDT (UTC-7). Override the local
# HH:MM via OVERLAY_TEST_NOW (e.g. "03:00" simulates the overnight regen). ----
_h, _m = (int(x) for x in os.environ.get("OVERLAY_TEST_NOW", "05:57").split(":"))
NOW_UTC = (datetime(2026, 6, 17, _h, _m) + timedelta(hours=7)).replace(tzinfo=timezone.utc)
DAY_OF_WEEK = "Wednesday"
BOUNDARY = "2026-06-17"
DATE_TIME = f"2026-06-17 {_h:02d}:{_m:02d}"

# CRITICAL: the context injector fills every agent's "Current Time" from the
# live clock (context_injector.get_local_time_str), overriding anything passed
# in agent_input. Without pinning it, the model sees the real wall-clock, which
# contradicts the scaffold's simulated "Now (...)". Patch it so the whole prompt
# reflects our simulated NOW — otherwise we are not testing the pinned time at all.
import app.assistant.agent_runtime.services.context_injector as _context_injector
_context_injector.get_local_time_str = lambda *a, **k: f"{BOUNDARY} {_h:02d}:{_m:02d}:00 PDT"


def _item(title, start_utc, end_utc, start_local, end_local, status="upcoming"):
    return {
        "title": title,
        "start_utc": start_utc,
        "end_utc": end_utc,
        "start_local": start_local,
        "end_local": end_local,
        "status": status,
    }


# The exact schedule under test (PDT local; UTC = local + 7h).
EXPECTED_SCHEDULE = [
    _item("Birthday party", "2026-06-17T07:00:00+00:00", "2026-06-18T06:59:00+00:00",
          "12:00 AM", "11:59 PM", status="ongoing"),
    _item("Take out the dogs", "2026-06-17T15:00:00+00:00", "2026-06-17T15:30:00+00:00",
          "8:00 AM", "8:30 AM"),
    _item("Wednesday 8:30 AM - Kids Late Start", "2026-06-17T15:30:00+00:00", "2026-06-17T16:00:00+00:00",
          "8:30 AM", "9:00 AM"),
    _item("Work Hours", "2026-06-17T16:00:00+00:00", "2026-06-17T23:30:00+00:00",
          "9:00 AM", "4:30 PM"),
    _item("Analytics team standup meeting", "2026-06-17T16:15:00+00:00", "2026-06-17T17:15:00+00:00",
          "9:15 AM", "10:15 AM"),
    _item("Family time", "2026-06-18T01:00:00+00:00", "2026-06-18T03:00:00+00:00",
          "6:00 PM", "8:00 PM"),
    _item("Dinner", "2026-06-18T01:30:00+00:00", "2026-06-18T02:00:00+00:00",
          "6:30 PM", "7:00 PM"),
    _item("Trash Night!", "2026-06-18T03:00:00+00:00", "2026-06-18T07:00:00+00:00",
          "8:00 PM", "12:00 AM"),
    _item("Walk the dogs", "2026-06-18T04:00:00+00:00", "2026-06-18T04:30:00+00:00",
          "9:00 PM", "9:30 PM"),
]

CTX_DATA = {
    "day_theme": "A normal Wednesday workday with morning dog logistics, a late-start kids window, "
                 "a 9:15 AM standup, then evening family time, dinner, trash night, and the late dog walk.",
    "expected_schedule": EXPECTED_SCHEDULE,
    "current_status": "AFK (overnight)",
    "milestones": [{"time": "10:45 PM", "description": "AFK", "ongoing": True}],
}

# Fixed belief block in the production "[domain/confidence] statement" shape.
# Includes the exact (muddy) cooling belief + the crisp 9PM one + two anchors to test placement.
BELIEFS_BLOCK = """
### Routine beliefs
[routine/high] Lighting is whole-house/global only. The whole-house lights routine runs at 07:00 AM (on) and 08:00 AM (off) unless overridden.
[routine/high] Do not fire micro-reminders or nudges during meetings.

### General beliefs
[general/high] Default home cooling schedule for the user: as a recurring nightly routine around 9:00 PM, automatically set the home AC to 70F (handle this as an automation/action - do it - rather than a reminder). At 6:00 AM, stop active cooling and let the house naturally drift warmer toward ~75F; do not try to force the system to reach 75F by 6:00 AM - the intent is simply that 6:00 AM is when cooling stops so it does not keep running in the morning. If the user explicitly requests an exception/override, follow that instead.

### Sleep beliefs
[sleep/medium] Set or confirm the home/bedroom AC to 70F at 9:00 PM every night, using the Nest integration when available.
""".strip()


def _checks(md: str) -> None:
    low = md.lower()
    heading_lines = [ln for ln in md.splitlines() if ln.strip().startswith("###")]
    headings_low = "\n".join(heading_lines).lower()
    print("\n=== heuristic checks (hour-by-hour overlay, not a second schedule) ===")

    def ck(label, ok):
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")

    # 6 AM cooling: its OWN actionable slot in the empty pre-first-item gap (the bug under test)
    ck("6 AM cooling as actionable slot ('6' + 75/stop)",
       ("6:00" in md or "6 am" in low) and ("75" in md or "stop active cooling" in low))
    # 7 AM lights: clock-anchored belief filled into an otherwise-empty hour
    ck("7 AM lights slot filled ('7' + lights)",
       ("7:00" in md or "07:00" in md or "7 am" in low) and "lights" in low)
    # NOTE: the 9 PM AC-to-70 step is intentionally OUT of a 05:57 morning window —
    # it belongs to the evening regeneration (its own 21:00 slot), not here.
    # in-window schedule anchors present (tolerant of hyphenation, e.g. "late-start")
    for anchor in ("dogs", "kids late", "standup"):
        ck(f"anchor present: '{anchor}'", anchor in low)
    # whole-day generic belief surfaces (no nudges during meetings)
    ck("whole-day generic surfaced (meeting nudge rule)",
       "meeting" in low and ("nudge" in low or "micro" in low or "reminder" in low or "suppress" in low))
    # did not parrot the self-negating phrasing
    ck("did NOT copy 'do not try to force' phrasing", "do not try to force" not in low)
    # regression guard: upcoming hours must NOT be framed as already-done
    past_framing = any(p in low for p in (
        "what has happened", "should already have fired", "should have fired", "already fired",
    ))
    ck("no past-framing of upcoming hours", not past_framing)


def main() -> None:
    dry = "--dry" in sys.argv
    daily_context_block, tail_anchors_block = _format_daily_context(CTX_DATA, now_utc=NOW_UTC)

    print("=" * 70)
    print("INPUT: daily_context_block (the windowed schedule the writer receives)")
    print("=" * 70)
    print(daily_context_block)
    print("\n--- tail_anchors_block ---")
    print(tail_anchors_block or "(none)")
    print("\n--- beliefs_block ---")
    print(BELIEFS_BLOCK)

    # Deterministic scaffold check (no LLM): the empty pre-first-event hours
    # must exist as (open) slots for timed beliefs to land in.
    print("\n=== scaffold checks (deterministic, no LLM) ===")
    sb_low = daily_context_block.lower()
    scaffold_ok = (
        "(open)" in daily_context_block
        and ("6:00 am" in sb_low or "6 am" in sb_low)
        and ("7:00 am" in sb_low or "7 am" in sb_low)
        and ("11:00 pm" in sb_low or "10:00 pm" in sb_low)  # spans to end of day
    )
    print(f"  [{'PASS' if scaffold_ok else 'FAIL'}] open 6/7 AM slots + scaffold spans to end of day")

    if dry:
        print("\n[--dry] skipping LLM call.")
        return

    scope = load_scope_for_source(kind="pipeline", source_id="dayflow", actor_id="dayflow_routine_overlay_test")
    agent = DI.agent_factory.create_agent("dayflow_routine_writer")
    if agent is None:
        raise RuntimeError("dayflow_routine_writer agent not found")

    msg = Message(
        scope_context=scope,
        agent_input={
            "date_time": DATE_TIME,
            "day_of_week": DAY_OF_WEEK,
            "boundary_date_local": BOUNDARY,
            "daily_context": daily_context_block,
            "tail_anchors_block": tail_anchors_block,
            "beliefs_block": BELIEFS_BLOCK,
            "weekly_insights_block": "",
        },
    )
    result = agent.action_handler(msg)
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        raise RuntimeError(f"Agent returned unexpected result type: {type(result)}")

    print("\n" + "=" * 70)
    print("OUTPUT: generated routine overlay")
    print("=" * 70)
    print(data.get("markdown", ""))
    _checks(data.get("markdown", "") or "")


if __name__ == "__main__":
    main()
