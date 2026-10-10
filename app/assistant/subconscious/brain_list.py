"""The brain's list: what needs doing, decided once and changed deliberately (2026-10-04).

Design: docs/design/brain_delegates_work_2026-10-04.md, section 1. Concerns are the matters in the owner's life;
the list holds the pieces of work the brain decides they, and today's calendar, need. One decider writes it: the day's
tasks (subconscious/day_tasks.py), which since 2026-10-09 also decides every concern's entries; the concern planner
proposes them. Each entry has a stable id,
what it serves (a calendar placement or a concern), the decision (needed, not needed, done elsewhere), who acts,
the assistant's part and when, and a status. Dayflow's list-item intake makes work from the entries
(dayflow_orchestrator/brain_list_intake.py); until 2026-10-10 only in a lane switched live (configs/brain_lanes.json),
and elsewhere they were recorded beside what dayflow's steward did.

Every change is recorded with its cause, so a changed decision reads as what it would be when live: entry X
created, modified (before, after, why), or cancelled. Concern entries get an id when first created and are then
changed by it. Until 2026-10-09 the day's placements had entries of their own, keyed by code to their belief and day
(`cal:<day>:<belief_key>`); since then each placement is a concern (subconscious/calendar_concerns.py) and its entry a
concern's.

Tables in emi.db (subconscious/db.py):
    brain_list_entries  one row per entry, its current state.
    brain_list_changes  one row per created, modified or cancelled entry: before, after, the cause.
What an entry's decision rests on is held as relations (subconscious/relations.py, rel `relies_on`), written with the
entry in the same transaction when it is created or changed (2026-10-08: until then the decider wrote its reasons'
references as labels inside `why`, and a later prompt read "W21 ... W29" that named nothing).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.assistant.subconscious.db import connect as _connect

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS brain_list_entries (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        day TEXT,
        belief_key TEXT,
        placement_time TEXT,
        concern_id TEXT,
        what TEXT NOT NULL,
        decision TEXT NOT NULL,
        who_acts TEXT NOT NULL,
        assistant_part TEXT NOT NULL,
        when_local TEXT NOT NULL,
        covers TEXT NOT NULL,
        why TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS brain_list_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_id TEXT NOT NULL,
        at TEXT NOT NULL,
        change TEXT NOT NULL,
        before TEXT,
        after TEXT,
        cause TEXT NOT NULL)""",
]

FIELDS = ("what", "decision", "who_acts", "assistant_part", "when_local", "covers", "why")
_TERMINAL = ("cancelled",)


def poke_dayflow() -> None:
    """Ask dayflow for a planning tick: the list changed, and the tick makes its work. Until 2026-10-10 this was
    concern_handoff._poke_dayflow, its event named for the handoffs to the steward."""
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import Message
    msg = Message(sender="brain_list", receiver=None, data_type="agent_msg", content="brain_list_changed")
    msg.event_topic = "brain_list_changed"
    DI.event_hub.publish(msg)


def ensure_schema(connect=None) -> None:
    from app.assistant.subconscious import relations
    with (connect or _connect)(True) as c:
        for statement in SCHEMA:
            c.execute(statement)
    relations.ensure_schema(connect)


def relies_on(entry_id: str, *, connect=None) -> List[Tuple[str, str]]:
    """What the entry's current decision rests on: (kind, id) of each object, in the order given."""
    from app.assistant.subconscious import relations
    return relations.targets(("list_entry", entry_id), "relies_on", connect=connect)


def _relies_on(c, entry_id: str, refs, *, cause: str, at: str) -> None:
    from app.assistant.subconscious import relations
    relations.replace(c, ("list_entry", entry_id), "relies_on", [tuple(r) for r in refs or []], at=at, by=cause)


def _row(r) -> Dict[str, Any]:
    d = dict(r)
    d["covers"] = json.loads(d["covers"])
    return d


def entries(*, day: Optional[str] = None, concern_id: Optional[str] = None, include_cancelled: bool = False,
            kind: str = "calendar", connect=None) -> List[Dict[str, Any]]:
    """The current entries for a day (its calendar placements, or with kind "day" the day's tasks' own entries) or
    for one concern."""
    connect = connect or _connect
    ensure_schema(connect)
    if kind not in ("calendar", "day"):
        raise ValueError(f"brain list: a day's entries are kind calendar or day, not {kind!r}")
    where, args = (f"kind = '{kind}' AND day = ?", [day]) if day is not None else ("concern_id = ?", [concern_id])
    if not include_cancelled:
        where += " AND status != 'cancelled'"
    with connect(False) as c:
        # rowid, not id: entries made in one change share created_at, and a concern entry's id is random.
        rows = c.execute(f"SELECT * FROM brain_list_entries WHERE {where} ORDER BY created_at, rowid", args).fetchall()
    return [_row(r) for r in rows]


def open_before(day: str, *, connect=None) -> List[Dict[str, Any]]:
    """Open calendar and day entries of days before `day` (needed, not cancelled), oldest first: the ones whose work
    may still be in motion (the day's tasks lists those whose work is, day_tasks.gather)."""
    connect = connect or _connect
    ensure_schema(connect)
    with connect(False) as c:
        rows = c.execute("SELECT * FROM brain_list_entries WHERE kind IN ('calendar', 'day') AND day < ? "
                         "AND status = 'open' ORDER BY created_at, rowid", (day,)).fetchall()
    return [_row(r) for r in rows]


def changes(entry_id: str, *, connect=None) -> List[Dict[str, Any]]:
    connect = connect or _connect
    ensure_schema(connect)
    with connect(False) as c:
        rows = c.execute("SELECT * FROM brain_list_changes WHERE entry_id = ? ORDER BY id", (entry_id,)).fetchall()
    return [{**dict(r), "before": json.loads(r["before"]) if r["before"] else None,
             "after": json.loads(r["after"]) if r["after"] else None} for r in rows]


def _state(e: Dict[str, Any]) -> Dict[str, Any]:
    return {k: e[k] for k in FIELDS}


def _status(decision: str) -> str:
    return "open" if decision == "needed" else decision


def _write(c, entry_id: str, change: str, before, after, cause: str, at: str) -> None:
    c.execute("INSERT INTO brain_list_changes (entry_id, at, change, before, after, cause) VALUES (?,?,?,?,?,?)",
              (entry_id, at, change, json.dumps(before, ensure_ascii=False) if before is not None else None,
               json.dumps(after, ensure_ascii=False) if after is not None else None, cause))


def upsert(c, entry: Dict[str, Any], *, cause: str, at: str) -> str:
    """Create the entry, or modify it when any of its fields changed. Returns created, modified or unchanged."""
    row = c.execute("SELECT * FROM brain_list_entries WHERE id = ?", (entry["id"],)).fetchone()
    new = {k: entry[k] for k in FIELDS}
    if row is None:
        c.execute("INSERT INTO brain_list_entries (id, kind, day, belief_key, placement_time, concern_id, what, decision, "
                  "who_acts, assistant_part, when_local, covers, why, status, created_at, updated_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (entry["id"], entry["kind"], entry.get("day"), entry.get("belief_key"), entry.get("placement_time"),
                   entry.get("concern_id"), new["what"], new["decision"], new["who_acts"], new["assistant_part"],
                   new["when_local"], json.dumps(new["covers"]), new["why"], _status(new["decision"]), at, at))
        _write(c, entry["id"], "created", None, new, cause, at)
        return "created"
    old = _state(_row(row))
    # A changed reason alone does not change the entry: the decision and what it asks for are the entry.
    if {k: v for k, v in old.items() if k != "why"} == {k: v for k, v in new.items() if k != "why"} \
            and row["status"] != "cancelled":
        return "unchanged"
    c.execute("UPDATE brain_list_entries SET what=?, decision=?, who_acts=?, assistant_part=?, when_local=?, covers=?, "
              "why=?, status=?, updated_at=? WHERE id=?",
              (new["what"], new["decision"], new["who_acts"], new["assistant_part"], new["when_local"],
               json.dumps(new["covers"]), new["why"], _status(new["decision"]), at, entry["id"]))
    _write(c, entry["id"], "modified", old, new, cause, at)
    return "modified"


def cancel(c, entry_id: str, *, reason: str, cause: str, at: str) -> None:
    row = c.execute("SELECT * FROM brain_list_entries WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        raise KeyError(f"brain list: no entry {entry_id}")
    c.execute("UPDATE brain_list_entries SET status='cancelled', why=?, updated_at=? WHERE id=?", (reason, at, entry_id))
    _write(c, entry_id, "cancelled", _state(_row(row)), None, cause, at)


def apply_entries(day: str, changes: List[Dict[str, Any]], *, cause: str, at: datetime,
                  connect=None) -> Dict[str, int]:
    """The list decider's entries onto the list (subconscious/day_tasks.py): every entry not keyed to a calendar
    placement. A new entry that serves a concern is the concern's (`con:<hex>`, kind concern); one that serves none is
    the day's (`day:<day>:<hex>`, kind day). Modify and cancel name any entry (resolved to its id by the caller): the
    day's, an earlier day's, or a concern's. A concern's entry keeps its concern; a day's entry keeps the concern the
    decider last named for it (2026-10-08: intake then attaches its work to the concern, and it ends when the concern
    closes). A change's `relies_on` ((kind, id) pairs, resolved by the caller) becomes the entry's relations when
    the entry is created, changed or cancelled (a cancellation's since 2026-10-10: the day's tasks are woken when work
    a decision relied on ends, and a cancellation "covered by the work already scheduled" relies on that work).

    The only writer of these entries since 2026-10-09 (owner: one decider, docs/design/hub_and_spokes_2026-10-09.md).
    Until then the concern planner wrote a concern's entries (apply_concern) and the day's tasks wrote its own, and
    neither could change the other's: a request about work made from a calendar entry could only become a concern
    entry making new work to stop the old."""
    connect = connect or _connect
    ensure_schema(connect)
    when = at.isoformat()
    counts = {"created": 0, "modified": 0, "unchanged": 0, "cancelled": 0}
    with connect(True) as c:
        for a in changes:
            if a["action"] == "cancel":
                cancel(c, a["entry"], reason=a["why"], cause=cause, at=when)
                _relies_on(c, a["entry"], a.get("relies_on"), cause=cause, at=when)
                counts["cancelled"] += 1
                continue
            concern_id = a.get("concern_id")
            if a["action"] == "new":
                kind = "concern" if concern_id else "day"
                entry_id = f"con:{uuid.uuid4().hex[:10]}" if concern_id else f"day:{day}:{uuid.uuid4().hex[:8]}"
                entry_day = None if concern_id else day
            else:
                row = c.execute("SELECT kind, day FROM brain_list_entries WHERE id = ?", (a["entry"],)).fetchone()
                if row is None:
                    raise KeyError(f"brain list: no entry {a['entry']}")
                kind, entry_id, entry_day = row["kind"], a["entry"], row["day"]
            result = upsert(c, {
                "id": entry_id, "kind": kind, "day": entry_day, "belief_key": None, "placement_time": None,
                "concern_id": concern_id, "what": a["what"], "decision": "needed", "who_acts": "",
                "assistant_part": a["assistant_part"], "when_local": a["when"], "covers": [], "why": a["why"]},
                cause=cause, at=when)
            if kind != "concern":
                # The concern a day's entry serves is the entry's own link, kept as the decider last named it.
                c.execute("UPDATE brain_list_entries SET concern_id = ? WHERE id = ?", (concern_id, entry_id))
            if result != "unchanged":
                _relies_on(c, entry_id, a.get("relies_on"), cause=cause, at=when)
            counts[result] += 1
    return counts


def cancel_for_closed_concerns(register: Dict[str, Any], *, at: datetime, connect=None) -> List[str]:
    """Cancel every open entry whose concern is no longer open (resolved, dormant, or gone from the register), with
    the concern's resolution as the reason; dayflow's list-item intake then ends the entry's work like any cancelled
    entry's. Run on every brain pass, so it covers every way a concern closes (the brain step, the noticer).
    Until 2026-10-05 a resolution touched no entry: the brain could hold a matter settled while dayflow still had
    an active goal for it. Returns the ids cancelled."""
    connect = connect or _connect
    ensure_schema(connect)
    open_ids = {c["concern_id"] for b in ("active", "addressing") for c in register.get(b) or []}
    closed = {c["concern_id"]: (b, c) for b in ("resolved", "dormant") for c in register.get(b) or []}
    with connect(False) as c:
        # Every entry serving a concern: the concern's own, and a day's entry made for it (2026-10-08).
        rows = c.execute("SELECT id, concern_id FROM brain_list_entries WHERE concern_id IS NOT NULL "
                         "AND status != 'cancelled'").fetchall()
    stale = [(r["id"], r["concern_id"]) for r in rows if r["concern_id"] not in open_ids]
    if not stale:
        return []
    when = at.isoformat()
    with connect(True) as c:
        for entry_id, concern_id in stale:
            if concern_id in closed:
                bucket, concern = closed[concern_id]
                why = concern.get("resolution_reason") or concern.get("dormant_reason") or "(no reason recorded)"
                reason = f"its concern was {bucket}: {why}"
            else:
                reason = "its concern is no longer in the register"
            cancel(c, entry_id, reason=reason, cause=f"concern closed {when}", at=when)
    return [entry_id for entry_id, _ in stale]
