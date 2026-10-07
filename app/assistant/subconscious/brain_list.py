"""The brain's list: what needs doing, decided once and changed deliberately (2026-10-04).

Design: docs/design/brain_delegates_work_2026-10-04.md, section 1. Concerns are the matters in the owner's life;
the list holds the pieces of work the brain decides they, and today's calendar, need. Each entry has a stable id,
what it serves (a calendar placement or a concern), the decision (needed, not needed, done elsewhere), who acts,
the assistant's part and when, and a status. In a lane switched live (configs/brain_lanes.json) dayflow's list-item
intake makes work from the entries (dayflow_orchestrator/brain_list_intake.py); elsewhere they are recorded beside
what dayflow actually does.

Every change is recorded with its cause, so a changed decision reads as what it would be when live: entry X
created, modified (before, after, why), or cancelled. Calendar entries are keyed by code to their belief and day
(`cal:<day>:<belief_key>`; until 2026-10-05 also the time, so a placement the day pass moved was cancelled and made
again), the times being a field; concern entries get an id when first created and are then changed by it.

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


def calendar_id(day: str, belief_key: str) -> str:
    return f"cal:{day}:{belief_key}"


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
    """A day's-tasks answer onto the list: each decided placement's entry created or changed, keyed by code; a
    placement it did not decide keeps its entry as it is (silence is keep); covers become entry ids. An entry whose
    placement is no longer in the day's plan is kept: the day pass re-places the day as its inputs change and a
    belief can drop out of one run and return in the next (2026-10-05: the clock-in reminder was in 2 of 9 runs), so
    a drop is not the brain's decision to stop the work; an entry ends only by a decision. (Until 2026-10-05 a dropped
    placement's entry was cancelled, and with the calendar lane live its work with it.) Returns the counts of each
    change; `kept` counts the offered placements left undecided. The day's tasks name no one who acts: a list entry is the assistant's own task
    (who_acts stays empty)."""
    connect = connect or _connect
    ensure_schema(connect)
    when = at.isoformat()
    by_label = {it["label"]: it for it in items}
    ids = {label: calendar_id(day, it["belief_key"]) for label, it in by_label.items()}
    counts = {"created": 0, "modified": 0, "unchanged": 0, "kept": len(items) - len(decisions)}
    with connect(True) as c:
        for d in decisions:
            it = by_label[d["item"]]
            needed = d["decision"] == "needed"
            result = upsert(c, {
                "id": ids[d["item"]], "kind": "calendar", "day": day, "belief_key": it["belief_key"],
                "placement_time": ", ".join(it["times"]), "concern_id": None,
                "what": d["assistant_part"] if needed else it["statement"], "decision": d["decision"],
                "who_acts": "", "assistant_part": d["assistant_part"], "when_local": d["when"],
                "covers": [ids[x] for x in d.get("covers") or []], "why": d["why"]}, cause=cause, at=when)
            counts[result] += 1
    return counts


def apply_day_own(day: str, changes: List[Dict[str, Any]], *, cause: str, at: datetime,
                  connect=None) -> Dict[str, int]:
    """The day's tasks' own entries onto the list (2026-10-06): something today needs that no calendar item asks for
    (a calendar event starting, something said, a standing directive applying to the moment). New ones get an id
    (`day:<day>:<hex>`); modify and cancel name it. Kind "day": the calendar lane delivers them by their day. Until
    then the decider could only fill a belief's entry, and put other acts into unrelated beliefs' entries."""
    connect = connect or _connect
    ensure_schema(connect)
    when = at.isoformat()
    counts = {"created": 0, "modified": 0, "unchanged": 0, "cancelled": 0}
    with connect(True) as c:
        for a in changes:
            if a["action"] == "cancel":
                cancel(c, a["entry"], reason=a["why"], cause=cause, at=when)
                counts["cancelled"] += 1
                continue
            entry_id = a["entry"] if a["action"] == "modify" else f"day:{day}:{uuid.uuid4().hex[:8]}"
            result = upsert(c, {
                "id": entry_id, "kind": "day", "day": day, "belief_key": None, "placement_time": None,
                "concern_id": None, "what": a["what"], "decision": "needed", "who_acts": "",
                "assistant_part": a["assistant_part"], "when_local": a["when"], "covers": [], "why": a["why"]},
                cause=cause, at=when)
            counts[result] += 1
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
                    "concern_id": concern_id, "what": a["what"], "decision": "needed", "who_acts": "",
                    "assistant_part": a["assistant_part"], "when_local": a["when"], "covers": [], "why": a["why"]},
                    cause=cause, at=when)
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
        rows = c.execute("SELECT id, concern_id FROM brain_list_entries WHERE kind = 'concern' "
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
