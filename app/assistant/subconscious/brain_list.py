"""The brain's list: what needs doing, decided once and changed deliberately. SHADOW ONLY (2026-10-04).

Design: docs/design/brain_delegates_work_2026-10-04.md, section 1. Concerns are the matters in the owner's life;
the list holds the pieces of work the brain decides they, and today's calendar, need. Each entry has a stable id,
what it serves (a calendar placement or a concern), the decision (needed, not needed, done elsewhere), who acts,
the assistant's part and when, and a status. Nothing is handed to dayflow yet: the entries are what the brain would
delegate, recorded beside what dayflow actually does.

Every change is recorded with its cause, so a changed decision reads as what it would be when live: entry X
created, modified (before, after, why), or cancelled. Calendar entries are keyed by code to their placement
(`cal:<day>:<belief_key>:<time>`); concern entries get an id when first created and are then changed by it.

Tables in emi.db (subconscious/db.py):
    brain_list_entries  one row per entry, its current state.
    brain_list_changes  one row per created, modified or cancelled entry: before, after, the cause.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

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


def ensure_schema(connect=None) -> None:
    with (connect or _connect)(True) as c:
        for statement in SCHEMA:
            c.execute(statement)


def calendar_id(day: str, belief_key: str, time: str) -> str:
    return f"cal:{day}:{belief_key}:{'-'.join(time.lower().split())}"


def _row(r) -> Dict[str, Any]:
    d = dict(r)
    d["covers"] = json.loads(d["covers"])
    return d


def entries(*, day: Optional[str] = None, concern_id: Optional[str] = None, include_cancelled: bool = False,
            connect=None) -> List[Dict[str, Any]]:
    """The current entries for a day's calendar or for one concern."""
    connect = connect or _connect
    ensure_schema(connect)
    where, args = ("kind = 'calendar' AND day = ?", [day]) if day is not None else ("concern_id = ?", [concern_id])
    if not include_cancelled:
        where += " AND status != 'cancelled'"
    with connect(False) as c:
        rows = c.execute(f"SELECT * FROM brain_list_entries WHERE {where} ORDER BY created_at, id", args).fetchall()
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


def apply_calendar(day: str, items: List[Dict[str, Any]], decisions: List[Dict[str, Any]], *, cause: str,
                   at: datetime, connect=None) -> Dict[str, int]:
    """A day decision onto the list: one entry per placement, keyed by code; covers become entry ids; a placement
    no longer on the day is cancelled. Returns the counts of each change."""
    connect = connect or _connect
    ensure_schema(connect)
    when = at.isoformat()
    by_label = {it["label"]: it for it in items}
    ids = {label: calendar_id(day, it["belief_key"], it["time"]) for label, it in by_label.items()}
    counts = {"created": 0, "modified": 0, "unchanged": 0, "cancelled": 0}
    with connect(True) as c:
        for d in decisions:
            it = by_label[d["item"]]
            needed = d["decision"] == "needed"
            result = upsert(c, {
                "id": ids[d["item"]], "kind": "calendar", "day": day, "belief_key": it["belief_key"],
                "placement_time": it["time"], "concern_id": None,
                "what": d["assistant_part"] if needed else it["statement"], "decision": d["decision"],
                "who_acts": d["who_acts"], "assistant_part": d["assistant_part"], "when_local": d["when"],
                "covers": [ids[x] for x in d.get("covers") or []], "why": d["why"]}, cause=cause, at=when)
            counts[result] += 1
        live = set(ids.values())
        for r in c.execute("SELECT id FROM brain_list_entries WHERE kind='calendar' AND day=? AND status!='cancelled'",
                           (day,)).fetchall():
            if r["id"] not in live:
                cancel(c, r["id"], reason="no longer placed on the day's calendar", cause=cause, at=when)
                counts["cancelled"] += 1
    return counts


def apply_concern(concern_id: str, actions: List[Dict[str, Any]], *, cause: str, at: datetime,
                  connect=None) -> Dict[str, int]:
    """A concern decision onto the list: keep, modify or cancel each existing entry by its id, and create the new
    ones. Returns the counts of each change."""
    connect = connect or _connect
    ensure_schema(connect)
    when = at.isoformat()
    counts = {"created": 0, "modified": 0, "unchanged": 0, "cancelled": 0}
    with connect(True) as c:
        for a in actions:
            if a["action"] == "keep":
                counts["unchanged"] += 1
            elif a["action"] == "cancel":
                cancel(c, a["entry"], reason=a["why"], cause=cause, at=when)
                counts["cancelled"] += 1
            else:
                entry_id = a["entry"] if a["action"] == "modify" else f"con:{uuid.uuid4().hex[:10]}"
                result = upsert(c, {
                    "id": entry_id, "kind": "concern", "day": None, "belief_key": None, "placement_time": None,
                    "concern_id": concern_id, "what": a["what"], "decision": "needed", "who_acts": a["who_acts"],
                    "assistant_part": a["assistant_part"], "when_local": a["when"], "covers": [], "why": a["why"]},
                    cause=cause, at=when)
                counts[result] += 1
    return counts
