"""The plan of the day, as data (2026-10-01).

The brain's day pass (subconscious/day_pass.py) places the owner's beliefs against today's calendar:
one hour slot per local hour, the calendar items that start in it, and the beliefs placed in it, each
as data: its time, its belief key, its statement. Beliefs placed "all day" are the day's flags. It is
stored here, one row per slot, and every reader renders it fresh from the rows with `render_day`: from
an hour before now to the end of the day, "Now" labelled at render time, runs of empty hours merged.
Nothing is rendered and then read back.

Until 2026-10-01 the routine stage wrote one markdown document and, as the day went on, parsed the
hour headings back out of it to drop the past ones.

Tables in emi.db (subconscious/db.py):
    day_plans       one row per day: the inputs fingerprint, the beliefs selected and why, the
                    writer's change summary, the day's backdrops and flags.
    day_plan_slots  one row per hour slot: its key (`YYYY-MM-DDTHH:00`, local), the calendar items
                    that start in it, the beliefs placed in it ({time, belief_key, statement,
                    concern_id: the concern that owns it}).
Past days stay: the table is the day archive.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.assistant.subconscious.db import connect as _connect

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS day_plans (
        day TEXT PRIMARY KEY,
        generated_at_utc TEXT NOT NULL,
        inputs_fingerprint TEXT NOT NULL,
        selected_belief_keys TEXT NOT NULL,
        qualifier_belief_keys TEXT NOT NULL,
        selection_reason TEXT NOT NULL,
        change_summary TEXT NOT NULL,
        backdrops TEXT NOT NULL,
        flags TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS day_plan_slots (
        day TEXT NOT NULL,
        slot TEXT NOT NULL,
        hour_local_iso TEXT NOT NULL,
        items TEXT NOT NULL,
        lines TEXT NOT NULL,
        PRIMARY KEY (day, slot))""",
]

_ITEM_FIELDS = ("title", "start_local", "end_local", "status")


def ensure_schema(connect=None) -> None:
    connect = connect or _connect
    with connect(True) as c:
        for statement in SCHEMA:
            c.execute(statement)


def save_plan(plan: Dict[str, Any], *, connect=None) -> None:
    """Replace the day's plan in one transaction."""
    connect = connect or _connect
    ensure_schema(connect)
    day = plan["day"]
    with connect(True) as c:
        c.execute("DELETE FROM day_plan_slots WHERE day = ?", (day,))
        c.execute(
            "INSERT OR REPLACE INTO day_plans (day, generated_at_utc, inputs_fingerprint, selected_belief_keys, "
            "qualifier_belief_keys, selection_reason, change_summary, backdrops, flags) VALUES (?,?,?,?,?,?,?,?,?)",
            (day, plan["generated_at_utc"], plan["inputs_fingerprint"], json.dumps(plan["selected_belief_keys"]),
             json.dumps(plan["qualifier_belief_keys"]), plan["selection_reason"], plan["change_summary"],
             json.dumps(plan["backdrops"], ensure_ascii=False), json.dumps(plan["flags"], ensure_ascii=False)))
        c.executemany(
            "INSERT INTO day_plan_slots (day, slot, hour_local_iso, items, lines) VALUES (?,?,?,?,?)",
            [(day, s["slot"], s["hour_local_iso"],
              json.dumps([{k: it.get(k) for k in _ITEM_FIELDS} for it in s["items"]], ensure_ascii=False),
              json.dumps(s["lines"], ensure_ascii=False)) for s in plan["slots"]])


def load_plan(day: str, *, connect=None) -> Optional[Dict[str, Any]]:
    """The stored plan for one day, its slots in hour order, or None."""
    connect = connect or _connect
    ensure_schema(connect)
    with connect(False) as c:
        row = c.execute("SELECT * FROM day_plans WHERE day = ?", (day,)).fetchone()
        if row is None:
            return None
        slots = c.execute("SELECT slot, hour_local_iso, items, lines FROM day_plan_slots WHERE day = ? "
                          "ORDER BY hour_local_iso", (day,)).fetchall()
    return {"day": row["day"], "generated_at_utc": row["generated_at_utc"],
            "inputs_fingerprint": row["inputs_fingerprint"],
            "selected_belief_keys": json.loads(row["selected_belief_keys"]),
            "qualifier_belief_keys": json.loads(row["qualifier_belief_keys"]),
            "selection_reason": row["selection_reason"], "change_summary": row["change_summary"],
            "backdrops": json.loads(row["backdrops"]), "flags": json.loads(row["flags"]),
            "slots": [{"slot": s["slot"], "hour_local_iso": s["hour_local_iso"], "items": json.loads(s["items"]),
                       "lines": json.loads(s["lines"])} for s in slots]}


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def _owner(line: Dict[str, Any]) -> str:
    return f" [concern:{line['concern_id']}]" if line.get("concern_id") else ""


def _placement_text(line: Dict[str, Any]) -> str:
    return f"{line['time']} — {line['statement']} [belief:{line['belief_key']}]{_owner(line)}"


def _item_text(item: Dict[str, Any]) -> str:
    title = str(item.get("title") or "Untitled").strip()
    start, end = str(item.get("start_local") or "").strip(), str(item.get("end_local") or "").strip()
    return f"{title} ({start}–{end})" if start and end else (f"{title} ({start})" if start else title)


def render_day(now_utc: Optional[datetime] = None, *, connect=None) -> str:
    """Today's plan from an hour before now to the end of the day, rendered from the stored rows."""
    from app.assistant.pipelines.context import boundary_date_local_str
    from app.assistant.utils.time_utils import get_local_timezone
    now_utc = now_utc or datetime.now(timezone.utc)
    day = boundary_date_local_str(now_utc.astimezone(get_local_timezone()))
    plan = load_plan(day, connect=connect)
    if plan is None:
        return f"No plan of the day has been written for {day} yet."
    return render_plan(plan, now_utc)


def render_plan(plan: Dict[str, Any], now_utc: datetime) -> str:
    """One plan, rendered for the moment `now_utc` (pure)."""
    from app.assistant.utils.time_utils import get_local_timezone
    tz = get_local_timezone()
    now_local = now_utc.astimezone(tz)
    day = plan["day"]
    first_hour = (now_local - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    out = [f"## Logistics and what to look for — {now_local.strftime('%A')} {day}"]
    if plan["backdrops"]:
        out.append("Backdrops (context, not slots): " + "; ".join(_item_text(b) for b in plan["backdrops"]))

    def is_now(hour: datetime) -> bool:
        return hour <= now_local < hour + timedelta(hours=1)

    open_run: List[datetime] = []

    def close_run() -> None:
        if open_run:
            end = open_run[-1] + timedelta(hours=1)
            out.append(f"### {_clock(open_run[0])} – {_clock(end)} — open" if len(open_run) > 1
                       else f"### {_clock(open_run[0])} — open")
            open_run.clear()

    for slot in plan["slots"]:
        hour = datetime.fromisoformat(slot["hour_local_iso"]).astimezone(tz)
        if hour < first_hour:
            continue
        if not slot["items"] and not slot["lines"] and not is_now(hour):
            open_run.append(hour)
            continue
        close_run()
        label = f"Now ({_clock(now_local)})" if is_now(hour) else _clock(hour)
        items = "; ".join(_item_text(it) for it in slot["items"])
        out.append(f"### {label} — {items}" if items else f"### {label}" + ("" if slot["lines"] else " — open"))
        out.extend(f"- {_placement_text(line)}" for line in slot["lines"])
    close_run()
    if plan["flags"]:
        out.append("## All day")
        out.extend(f"- {flag['statement']} [belief:{flag['belief_key']}]{_owner(flag)}" for flag in plan["flags"])
    return "\n".join(out) + "\n"


def resource_provider(scope: Dict[str, Any]) -> str:
    """`resource_dayflow_routine`, computed per read (registered in app/bootstrap.py)."""
    return render_day()
