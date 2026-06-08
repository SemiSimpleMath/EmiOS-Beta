"""Demo of #3 contextual retrieval: a high-recall RANKED candidate set the routine LLM judges —
NOT a hard filter. The deterministic layer only reorders; the only thing it removes is deprecated
(dormant) beliefs. So the Monday timesheet floats to the top on Monday and SINKS (but is still
present) on Tuesday — the LLM, with full day context, makes the final relevance call.

This is the lesson from merging applied to retrieval: deterministic means propose/rank, the LLM
decides. Nothing relevant is ever silently excluded (silent exclusion was the original bug).
No LLM, no model load (scores are deterministic; relevance term is off without an embedder).

Run: .venv/Scripts/python.exe -m belief_engine_v2.demo_retrieval
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta

from belief_engine_v2.retrieval import _first_business_day, beliefs_for_context
from belief_engine_v2.store import Claim, Store


def _add(store, subject, predicate, obj, statement, applies_when, occ, *,
         polarity="affirm", strength=1.0):
    store.append(Claim(subject=subject, predicate=predicate, object=obj,
                       statement_nl=statement, applies_when=applies_when),
                 source="seed", polarity=polarity, occurred_at=occ, recorded_at=occ, strength=strength)


def _slot(b):
    aw = json.loads(b["applies_when"]) if b["applies_when"] else None
    if not aw:
        return "evergreen"
    bits = []
    if aw.get("weekday"):
        bits.append(str(aw["weekday"]))
    if aw.get("anchor"):
        bits.append(str(aw["anchor"]))
    for key in ("at", "after", "before"):
        if aw.get(key):
            bits.append(f"{key} {aw[key]}")
    return " ".join(bits) or "evergreen"


def _show(label, store, now, horizon="day"):
    print(f"\n{label}  ({now:%A %Y-%m-%d %H:%M}, horizon={horizon})")
    got = beliefs_for_context(store, now, k=8, horizon=horizon)
    if not got:
        print("     (nothing applies)")
    for b in got:
        print(f"     [{b['score']:.2f}] ({_slot(b)})  {b['statement_nl']}")


def main() -> None:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd); os.remove(path)
    store = Store(path)

    mon = datetime(2026, 6, 8, 9, 0)
    mon = mon - timedelta(days=mon.weekday())          # a plain Monday (not the first business day)
    occ = (mon - timedelta(days=2)).isoformat()

    _add(store, "alex", "does_routine", "weekly timesheet",
         "Monday is weekly timesheet day; submit by ~10:00 AM.", {"weekday": "MON", "at": "10:00"}, occ)
    _add(store, "alex", "does_routine", "monthly timesheet",
         "First business day of the month: monthly timesheet, hard deadline 17:00.",
         {"anchor": "first_business_day", "at": "17:00"}, occ)
    _add(store, "home", "sets", "cooling off",
         "6 AM: turn cooling off and let the house warm naturally.", {"at": "06:00"}, occ)
    _add(store, "home", "sets", "cooling on",
         "9 PM: turn the cooling on.", {"at": "21:00"}, occ)
    _add(store, "alex", "avoids_after", "caffeine",
         "Avoid caffeine after 16:00.", {"after": "16:00"}, occ)
    # a deprecated belief: affirmed once, then contradicted harder → dormant → must never surface
    _add(store, "alex", "considers_closed", "project setup thread",
         "The Project timesheet-setup thread is aborted/closed.", None, occ)
    _add(store, "alex", "considers_closed", "project setup thread",
         "The Project timesheet-setup thread is aborted/closed.", None, occ,
         polarity="contradict", strength=2.0)
    store.rebuild_projection()

    total = store.conn.execute("SELECT COUNT(*) FROM beliefs WHERE status='active'").fetchone()[0]
    print(f"active beliefs: {total}  (none excluded except deprecated; below is the RANKED set the LLM judges)")

    fbd = datetime(2026, 7, 1, 9, 0)
    fbd = fbd.replace(day=_first_business_day(fbd))     # first business day of July, morning

    # DAY-PLANNING (the routine, in the morning): ranked; day-fit floats up, nothing dropped.
    _show("MONDAY MORNING - weekly timesheet in play; off-day monthly sinks (still listed)", store, mon)
    _show("TUESDAY MORNING - SAME beliefs re-ranked: weekly sinks too (not dropped)", store, mon + timedelta(days=1))
    _show("FIRST BUSINESS DAY MORNING - monthly floats up; weekly sinks", store, fbd)

    # INSTANT (a live point-in-time check): what's firing now rises; the rest sinks but stays.
    _show("LIVE @ 09:00 - nothing firing now, so setpoints sink to the bottom", store, mon.replace(hour=9), horizon="instant")
    _show("LIVE @ 21:00 - the 9 PM cooling-on rises to the top", store, mon.replace(hour=21), horizon="instant")


if __name__ == "__main__":
    main()
