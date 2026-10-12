"""The day's record, in SHADOW (docs/design/day_record_2026-10-09.md).

What the day held and what happened, in calendar form, each claim with what showed it: the input daily insights
turns into beliefs. Owner, 2026-10-09: it "starts with nothing but blank google calendar at the start of the day ...
builds the day calendar then over the course of the day"; its main input is the concerns ("[the daughter] is out
sick -> leads to [her] late start at school being cancelled").

Today the daily context tracker (pipelines/dayflow/steps/daily_context_generator_stage.py) keeps the day; this runs
beside it in every brain pass (brain_wake.run_brain, after the day's tasks) and feeds nothing yet, so the two can be
compared (`python -m app.assistant.subconscious.day_record compare`).

The record (emi.db):
    day_record_events      each event of the day: a Google event (`cal:<calendar id>`) or one the day revealed
                           (`day:<day>:<hex>`); its time and its state: planned, happened, moved, cancelled,
                           did_not_happen, unconfirmed; the basis of the state (direct or inferred) and a note.
    day_record_changes     each change of an event: before, after, the cause.
    day_record_milestones  dated things that happened, each with its basis.
    day_record_runs        each model run: its fingerprint, the day theme and the current status.
What showed a state or a milestone is the relation `shown_by` (subconscious/relations.py).

Code does what code knows: the calendar's events enter by code; an event whose end passed with no state becomes
`unconfirmed` (time passing is not evidence; daily insights reads unconfirmed as unknown, owner 2026-10-09); the
night and the day's time at and away from the computer become milestones from the sleep pipeline's sleep periods and
the daily context tracker's own activity spans (pipelines/dayflow/utils/context_sources.activity_spans: raw keyboard
and mouse activity, blips absorbed, short runs collapsed to on-and-off), rebuilt each pass. Owner, 2026-10-09: the
brain's presence events (absences of 10 minutes or more) are coarser than the tracker's activity data. The model
(`subconscious::day_record`) reads the open concerns with today's notes, the brain's decisions today and the record,
and answers only with changes and milestones, each naming in relies_on what shows it.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from app.assistant.subconscious.db import connect as _connect
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_AGENT = "subconscious::day_record"
STATES = ("planned", "happened", "moved", "cancelled", "did_not_happen", "unconfirmed")
_MODEL_STATES = ("planned", "happened", "moved", "cancelled", "did_not_happen")
BASES = ("direct", "inferred")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS day_record_events (
        id TEXT PRIMARY KEY, day TEXT NOT NULL, title TEXT NOT NULL, start_utc TEXT, end_utc TEXT,
        origin TEXT NOT NULL, state TEXT NOT NULL, basis TEXT NOT NULL, note TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS day_record_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL, at TEXT NOT NULL, before TEXT, after TEXT,
        cause TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS day_record_milestones (
        id TEXT PRIMARY KEY, day TEXT NOT NULL, at_utc TEXT NOT NULL, description TEXT NOT NULL, basis TEXT NOT NULL,
        origin TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS day_record_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, at TEXT NOT NULL, fingerprint TEXT NOT NULL,
        day_theme TEXT NOT NULL, current_status TEXT NOT NULL)""",
]
_FIELDS = ("title", "start_utc", "end_utc", "state", "basis", "note")


def ensure_schema(connect=None) -> None:
    from app.assistant.subconscious import relations
    with (connect or _connect)(True) as c:
        for statement in SCHEMA:
            c.execute(statement)
    relations.ensure_schema(connect)


def _day(now_utc: datetime) -> str:
    from app.assistant.pipelines.context import boundary_date_local_str
    from app.assistant.utils.time_utils import get_local_timezone
    return boundary_date_local_str(now_utc.astimezone(get_local_timezone()))


def _window(day: str):
    from app.assistant.pipelines.context import day_window_local
    start, end = day_window_local(day)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def local_clock_utc(day: str, clock: str) -> str:
    """A local clock time ("6:30 PM") on the day (a time before the day's boundary hour is the next calendar date), as
    UTC ISO."""
    from app.assistant.pipelines.context import day_window_local
    from app.assistant.subconscious.day_pass import _clock_hour_minute
    hour, minute = _clock_hour_minute(clock)
    start, _ = day_window_local(day)
    at = start.replace(hour=hour, minute=minute)
    if at < start:
        at += timedelta(days=1)
    return at.astimezone(timezone.utc).isoformat()


def _change(c, event_id: str, before, after, cause: str, at: str) -> None:
    c.execute("INSERT INTO day_record_changes (event_id, at, before, after, cause) VALUES (?,?,?,?,?)",
              (event_id, at, json.dumps(before, ensure_ascii=False) if before is not None else None,
               json.dumps(after, ensure_ascii=False) if after is not None else None, cause))


def _state(row) -> Dict[str, Any]:
    return {k: row[k] for k in _FIELDS}


# ── what code does ──────────────────────────────────────────────────────────

def sync_calendar(day: str, now_utc: datetime, *, events: Optional[List[Dict[str, Any]]] = None, connect=None) -> int:
    """The day's Google events onto the record: a new one enters `planned`; one whose title or times changed is
    updated; one gone from the calendar is `cancelled` (direct: the calendar itself shows it). Returns changes. An
    event's record is the day's (`cal:<day>:<calendar item id>`): an event running past the day's boundary is on both
    days' records (until 2026-10-12 the id was the calendar item's alone, and the second day's insert failed)."""
    if events is None:
        from app.assistant.pipelines.dayflow.utils.context_sources import get_calendar_events_structured_for_day
        events = get_calendar_events_structured_for_day(day)
    connect = connect or _connect
    at = now_utc.isoformat()
    changed = 0
    with connect(True) as c:
        existing = {r["id"]: r for r in c.execute(
            "SELECT * FROM day_record_events WHERE day = ? AND origin = 'calendar'", (day,)).fetchall()}
        seen = set()
        for ev in events:
            eid = f"cal:{day}:{ev['calendar_item_id']}"
            seen.add(eid)
            fields = {"title": ev["title"], "start_utc": ev["start_utc"], "end_utc": ev.get("end_utc")}
            row = existing.get(eid)
            if row is None:
                c.execute("INSERT INTO day_record_events (id, day, title, start_utc, end_utc, origin, state, basis, "
                          "note, created_at, updated_at) VALUES (?,?,?,?,?,'calendar','planned','','',?,?)",
                          (eid, day, fields["title"], fields["start_utc"], fields["end_utc"], at, at))
                _change(c, eid, None, {**fields, "state": "planned"}, "on the calendar", at)
                changed += 1
            elif any(row[k] != v for k, v in fields.items()):
                c.execute("UPDATE day_record_events SET title=?, start_utc=?, end_utc=?, updated_at=? WHERE id=?",
                          (fields["title"], fields["start_utc"], fields["end_utc"], at, eid))
                _change(c, eid, _state(row), {**_state(row), **fields}, "the calendar changed", at)
                changed += 1
        for eid, row in existing.items():
            if eid not in seen and row["state"] != "cancelled":
                after = {**_state(row), "state": "cancelled", "basis": "direct", "note": "removed from the calendar"}
                c.execute("UPDATE day_record_events SET state='cancelled', basis='direct', note=?, updated_at=? "
                          "WHERE id=?", (after["note"], at, eid))
                _change(c, eid, _state(row), after, "gone from the calendar", at)
                changed += 1
    return changed


def mark_passed(day: str, now_utc: datetime, *, connect=None) -> int:
    """An event still `planned` whose end has passed becomes `unconfirmed`: its time passed and nothing showed it
    happened or not. Time passing is not evidence (daily insights reads unconfirmed as unknown)."""
    connect = connect or _connect
    at = now_utc.isoformat()
    note = "its time passed; nothing showed whether it happened"
    with connect(True) as c:
        rows = c.execute("SELECT * FROM day_record_events WHERE day = ? AND state = 'planned' AND end_utc IS NOT NULL "
                         "AND end_utc < ?", (day, at)).fetchall()
        for row in rows:
            c.execute("UPDATE day_record_events SET state='unconfirmed', basis='', note=?, updated_at=? WHERE id=?",
                      (note, at, row["id"]))
            _change(c, row["id"], _state(row), {**_state(row), "state": "unconfirmed", "basis": "", "note": note},
                    "its time passed", at)
    return len(rows)


_SPAN_WORDS = {"Active": "at the computer", "AFK": "away from the computer", "Intermittent": "on and off at the computer"}


def _minutes(a: datetime, b: datetime) -> str:
    m = int(round((b - a).total_seconds() / 60))
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def _clock(value: datetime) -> str:
    from app.assistant.utils.time_utils import utc_to_local
    return utc_to_local(value).strftime("%I:%M %p").lstrip("0")


def _sleep_periods(day: str) -> List[Dict[str, Any]]:
    """The sleep pipeline's periods of the night that led into the day (resource_sleep_output, when it is the day's),
    each with its start and end as UTC datetimes."""
    from app.assistant.subconscious.situation import _resource, _scope
    from app.assistant.utils.time_utils import get_local_timezone
    sleep = _resource(_scope(), "resource_sleep_output")
    if sleep.get("date") != day:
        return []
    periods = sleep.get("sleep_periods") or []
    if isinstance(periods, str):
        import ast
        periods = ast.literal_eval(periods)
    out = []
    for p in periods:
        start, end = datetime.fromisoformat(p["start"]), datetime.fromisoformat(p["end"])
        # The pipeline stores naive UTC.
        out.append({"start": start.replace(tzinfo=timezone.utc) if start.tzinfo is None else start,
                    "end": end.replace(tzinfo=timezone.utc) if end.tzinfo is None else end,
                    "kind": p.get("type") or "sleep"})
    return out


def _activity_from(day: str, sleep: List[Dict[str, Any]]) -> datetime:
    """Where the day's activity spans begin: the end of the night's first sleep period (time at the computer between
    two sleep periods is part of the record; 2026-10-09 a 4:35-4:54 AM spell fell between them), else the day's
    boundary."""
    return sleep[0]["end"] if sleep else _window(day)[0]


def time_milestones(day: str, now_utc: datetime, *, sleep: Optional[List[Dict[str, Any]]] = None,
                    spans: Optional[List[Dict[str, Any]]] = None, connect=None) -> Optional[Dict[str, Any]]:
    """The night and the day's time at and away from the computer, as milestones by code, rebuilt each pass (spans
    are re-segmented as activity arrives): the sleep pipeline's sleep periods (inferred: sleep is read from the
    absence of activity) and the finished activity spans since the day began (direct: the keyboard and mouse data).
    Returns the span still going (the owner's state now), or None. Ids are the day's (`sleep:<day>:<start>`,
    `activity:<day>:<start>`): the night leading into the day is on both days' records (until 2026-10-12 the ids were
    the start alone, and the second day's rebuild failed on a span the first day held)."""
    if sleep is None:
        sleep = _sleep_periods(day)
    if spans is None:
        from app.assistant.pipelines.dayflow.utils.context_sources import activity_spans
        spans = activity_spans(_activity_from(day, sleep))
    connect = connect or _connect
    at = now_utc.isoformat()
    rows = [(f"sleep:{day}:{p['start'].isoformat()}", p["start"].isoformat(),
             f"asleep ({p['kind'].replace('_', ' ')}) {_clock(p['start'])} to {_clock(p['end'])}, "
             f"{_minutes(p['start'], p['end'])}", "inferred", "sleep") for p in sleep]
    current = None
    for s in spans:
        if s["ongoing"]:
            current = s
            continue
        rows.append((f"activity:{day}:{s['start_utc'].isoformat()}", s["start_utc"].isoformat(),
                     f"{_SPAN_WORDS[s['type']]} {_clock(s['start_utc'])} to {_clock(s['end_utc'])}, "
                     f"{_minutes(s['start_utc'], s['end_utc'])}", "direct", "activity"))
    with connect(True) as c:
        c.execute("DELETE FROM day_record_milestones WHERE day = ? AND origin IN ('sleep', 'activity')", (day,))
        c.executemany("INSERT INTO day_record_milestones (id, day, at_utc, description, basis, origin, created_at) "
                      "VALUES (?,?,?,?,?,?,?)", [(mid, day, when, words, basis, origin, at)
                                                 for mid, when, words, basis, origin in rows])
    return None if current is None else {
        "words": f"{_SPAN_WORDS[current['type']]} since {_clock(current['start_utc'])} "
                 f"({_minutes(current['start_utc'], now_utc)})"}


# ── what the model reads ───────────────────────────────────────────────────

def open_concerns() -> List[Dict[str, Any]]:
    """Every open concern as it stands now: its title, and its record as every reader gets it, the summary of its older
    notes and every note since, each with when (persist.history_for_prompt). Until 2026-10-11 it showed only the note
    the concern was raised with, undated unless raised today, and the notes made today: a concern raised on 10-07 said
    "She has a doctor appointment at 10:30 today.", and on 10-10 and 10-11 the record put the appointment on that day,
    while the summary it never read said the appointment was on 10-07."""
    from app.assistant.subconscious.concern_store import load_register
    from app.assistant.subconscious.persist import history_for_prompt
    register = load_register()
    return [{"concern_id": c["concern_id"], "title": c.get("title") or "", "subject": c.get("subject") or "household",
             "history": history_for_prompt(c)}
            for bucket in ("active", "addressing") for c in register.get(bucket) or []]


def records(day: str, *, connect=None):
    connect = connect or _connect
    with connect(False) as c:
        events = [dict(r) for r in c.execute("SELECT * FROM day_record_events WHERE day = ? ORDER BY start_utc, id",
                                             (day,)).fetchall()]
        milestones = [dict(r) for r in c.execute("SELECT * FROM day_record_milestones WHERE day = ? ORDER BY at_utc",
                                                 (day,)).fetchall()]
    return events, milestones


def gather(now_utc: datetime, *, connect=None) -> Dict[str, Any]:
    """Everything a run reads, with the code-made parts brought up to date first, labelled for the call."""
    from app.assistant.subconscious import day_tasks, relations
    from app.assistant.subconscious.projection import Projection
    connect = connect or _connect
    ensure_schema(connect)
    day = _day(now_utc)
    sync_calendar(day, now_utc, connect=connect)
    mark_passed(day, now_utc, connect=connect)
    current = time_milestones(day, now_utc, connect=connect)
    concerns = open_concerns()
    matters = day_tasks.matters_today(day, now_utc, connect=connect)
    events, milestones = records(day, connect=connect)
    p = Projection()
    for e in events:
        e["ref"] = p.label("K", "day_event", e["id"])
        e["time_is_now"] = bool(e["state"] == "planned" and e["start_utc"] and e["start_utc"] <= now_utc.isoformat()
                                and (not e["end_utc"] or now_utc.isoformat() < e["end_utc"]))
    for c in concerns:
        c["ref"] = p.label("C", "concern", c["concern_id"])
    for m in matters:
        m["ref"] = p.label("B", "brain_matter", str(m["id"]))
    for m in milestones:
        m["shown_by"] = [p.label_of(k, i) or "" for k, i in relations.targets(("milestone", m["id"]), "shown_by",
                                                                               connect=connect)]
    for e in events:
        e["shown_by"] = [p.label_of(k, i) or "" for k, i in relations.targets(("day_event", e["id"]), "shown_by",
                                                                               connect=connect)]
    return {"day": day, "now": now_utc.isoformat(), "current": current, "events": events, "milestones": milestones,
            "concerns": concerns, "matters": matters, "labels": p.labels, "correction": None}


def fingerprint(inputs: Dict[str, Any]) -> str:
    """What a run reads from outside, minus the clock and the labels: the concerns, the brain's decisions and the
    calendar's events. A run's own changes and milestones are left out, so a run never triggers the next; the
    code-made sleep and activity milestones too (they change every few minutes; the brain's decisions carry what a
    return or an absence meant)."""
    read = {"day": inputs["day"],
            "concerns": [(c["concern_id"], c["history"]) for c in inputs["concerns"]],
            "matters": [m["id"] for m in inputs["matters"]],
            "calendar": [(e["id"], e["title"], e["start_utc"], e["end_utc"]) for e in inputs["events"]
                         if e["origin"] == "calendar"]}
    return hashlib.sha256(json.dumps(read, sort_keys=True, default=str).encode("utf-8")).hexdigest()


# ── the answer ─────────────────────────────────────────────────────────────

def problems(data: Any, labels: Dict[str, List[str]]) -> List[str]:
    """Each change names an event by its label (or "new" with a title and a time), a state the model may set, a basis,
    and in relies_on what shows it; a milestone has its time, words, a basis and what shows it."""
    if not isinstance(data, dict):
        return ["the answer is not an object"]
    out: List[str] = []

    def shows(where, refs):
        refs = [str(x).strip() for x in refs or []]
        if not refs:
            out.append(f"{where}: relies_on names nothing; name what shows it")
        unknown = [x for x in refs if x not in labels]
        if unknown:
            out.append(f"{where}: relies_on {unknown} are not labels given")

    for ch in data.get("changes") or []:
        event = str(ch.get("event") or "").strip()
        where = f"change {event}"
        if event != "new" and (labels.get(event) or [None])[0] != "day_event":
            out.append(f"{where}: not an event given (its K label) or \"new\"")
        if event == "new" and not (str(ch.get("title") or "").strip() and str(ch.get("start") or "").strip()):
            out.append(f"{where}: a new event names its title and its start")
        if ch.get("state") not in _MODEL_STATES:
            out.append(f"{where}: state {ch.get('state')!r} is not one of {list(_MODEL_STATES)}")
        if ch.get("state") == "moved" and not str(ch.get("start") or "").strip():
            out.append(f"{where}: moved names the new start")
        if ch.get("basis") not in BASES:
            out.append(f"{where}: basis {ch.get('basis')!r} is not direct or inferred")
        shows(where, ch.get("relies_on"))
    for i, m in enumerate(data.get("milestones") or [], 1):
        where = f"milestone {i}"
        if not str(m.get("time") or "").strip() or not str(m.get("description") or "").strip():
            out.append(f"{where}: a milestone names its time and what happened")
        if m.get("basis") not in BASES:
            out.append(f"{where}: basis {m.get('basis')!r} is not direct or inferred")
        shows(where, m.get("relies_on"))
    return out


def apply(inputs: Dict[str, Any], data: Dict[str, Any], *, now_utc: datetime, fp: str, connect=None) -> Dict[str, int]:
    """The answer onto the record: each change and its evidence, each new milestone, the run (theme, status)."""
    from app.assistant.subconscious import relations
    from app.assistant.subconscious.projection import resolve
    connect = connect or _connect
    day, labels, at = inputs["day"], inputs["labels"], now_utc.isoformat()
    by = f"day record {at}"
    counts = {"changed": 0, "new": 0, "milestones": 0}
    with connect(True) as c:
        for ch in data.get("changes") or []:
            shown = [resolve(labels, x) for x in ch.get("relies_on") or []]
            note = str(ch.get("note") or "").strip()
            start = local_clock_utc(day, ch["start"]) if str(ch.get("start") or "").strip() else None
            end = local_clock_utc(day, ch["end"]) if str(ch.get("end") or "").strip() else None
            if ch["event"] == "new":
                eid = f"day:{day}:{uuid.uuid4().hex[:8]}"
                after = {"title": ch["title"].strip(), "start_utc": start, "end_utc": end, "state": ch["state"],
                         "basis": ch["basis"], "note": note}
                c.execute("INSERT INTO day_record_events (id, day, title, start_utc, end_utc, origin, state, basis, "
                          "note, created_at, updated_at) VALUES (?,?,?,?,?,'day',?,?,?,?,?)",
                          (eid, day, after["title"], start, end, after["state"], after["basis"], note, at, at))
                _change(c, eid, None, after, by, at)
                counts["new"] += 1
            else:
                eid = resolve(labels, ch["event"])[1]
                row = c.execute("SELECT * FROM day_record_events WHERE id = ?", (eid,)).fetchone()
                after = {**_state(row), "state": ch["state"], "basis": ch["basis"], "note": note}
                if ch["state"] == "moved":
                    after.update(start_utc=start, end_utc=end or row["end_utc"])
                c.execute("UPDATE day_record_events SET start_utc=?, end_utc=?, state=?, basis=?, note=?, updated_at=? "
                          "WHERE id=?", (after["start_utc"], after["end_utc"], after["state"], after["basis"], note,
                                         at, eid))
                _change(c, eid, _state(row), after, by, at)
                counts["changed"] += 1
            relations.replace(c, ("day_event", eid), "shown_by", shown, at=at, by=by)
        for m in data.get("milestones") or []:
            mid = f"m:{day}:{uuid.uuid4().hex[:8]}"
            c.execute("INSERT INTO day_record_milestones (id, day, at_utc, description, basis, origin, created_at) "
                      "VALUES (?,?,?,?,?,'model',?)",
                      (mid, day, local_clock_utc(day, m["time"]), m["description"].strip(), m["basis"], at))
            relations.replace(c, ("milestone", mid), "shown_by", [resolve(labels, x) for x in m["relies_on"]],
                              at=at, by=by)
            counts["milestones"] += 1
        c.execute("INSERT INTO day_record_runs (day, at, fingerprint, day_theme, current_status) VALUES (?,?,?,?,?)",
                  (day, at, fp, str(data.get("day_theme") or "").strip(), str(data.get("current_status") or "").strip()))
    return counts


def _last_fingerprint(day: str, connect) -> Optional[str]:
    with connect(False) as c:
        row = c.execute("SELECT fingerprint FROM day_record_runs WHERE day = ? ORDER BY id DESC LIMIT 1",
                        (day,)).fetchone()
    return row["fingerprint"] if row else None


def _agent_call(payload: Dict[str, Any]) -> Any:
    from app.assistant.routine_handlers.subconscious import _run_subconscious_agent
    return _run_subconscious_agent(handler_label="day_record", agent_name=_AGENT, context=payload,
                                   scope_id="subconscious::day_record", actor_id="subconscious::day_record")


def run_day_record(*, now_utc: Optional[datetime] = None, connect=None,
                   gather_inputs: Callable[[datetime], Dict[str, Any]] = None,
                   call: Callable[[Dict[str, Any]], Any] = None) -> Dict[str, Any]:
    """Bring the record up to date; ask the model when anything it reads changed. Raises when the answer is still
    malformed after one correction. The keyword arguments are test seams."""
    from app.assistant.subconscious.brain_trace import step
    now = now_utc or datetime.now(timezone.utc)
    connect = connect or _connect
    ensure_schema(connect)
    inputs = (gather_inputs or (lambda n: gather(n, connect=connect)))(now)
    fp = fingerprint(inputs)
    if fp == _last_fingerprint(inputs["day"], connect):
        return {"day_record": "unchanged"}
    call = call or _agent_call
    data = call(inputs)
    found = problems(data, inputs["labels"])
    if found:
        step("day_record", "answer refused, one correction", subject=inputs["day"], problems=found)
        data = call({**inputs, "correction": "; ".join(found)})
        found = problems(data, inputs["labels"])
        if found:
            raise ValueError(f"day_record answer still invalid after one correction: {found}")
    counts = apply(inputs, data, now_utc=now, fp=fp, connect=connect)
    step("day_record", "recorded (shadow)", subject=inputs["day"], **counts)
    logger.info("[day_record] %s: %s (shadow)", inputs["day"], counts)
    return {"day_record": counts}


# ── the comparison ─────────────────────────────────────────────────────────

def compare(day: str) -> str:
    """The old tracker's day (the stored resources) beside the record, for the owner to judge."""
    from app.assistant.subconscious import relations
    from app.assistant.subconscious.situation import _resource, _scope
    from app.assistant.utils.time_utils import local_time_text
    scope = _scope()
    old_cal = _resource(scope, "resource_expected_calendar")
    old_ctx = _resource(scope, "resource_daily_context_generator_output")
    lines = [f"=== OLD tracker ({old_cal.get('date')}, last updated {old_ctx.get('last_updated')}) ===",
             f"theme: {old_ctx.get('day_theme')}"]
    for e in old_cal.get("expected_schedule") or []:
        lines.append(f"  {e.get('start_local'):>8} - {e.get('end_local') or '':8} {e.get('title')} ({e.get('status')}; "
                     f"source {e.get('source')})")
    for m in old_ctx.get("milestones") or []:
        lines.append(f"  milestone {m.get('time')}: {m.get('description')} [{m.get('evidence')}]")
    events, milestones = records(day)
    with _connect(False) as c:
        run = c.execute("SELECT * FROM day_record_runs WHERE day = ? ORDER BY id DESC LIMIT 1", (day,)).fetchone()
    lines += ["", f"=== NEW record ({day}) ===", f"theme: {run['day_theme'] if run else '(no run yet)'}"]
    for e in events:
        shown = relations.targets(("day_event", e["id"]), "shown_by")
        lines.append(f"  {local_time_text(e['start_utc']) if e['start_utc'] else '':>18} {e['title']}: {e['state']}"
                     f"{' (' + e['basis'] + ')' if e['basis'] else ''}{': ' + e['note'] if e['note'] else ''}"
                     f"{'  <- ' + ', '.join(f'{k}:{i}' for k, i in shown) if shown else ''}")
    for m in milestones:
        lines.append(f"  milestone {local_time_text(m['at_utc'])}: {m['description']} [{m['basis']}, {m['origin']}]")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "compare":
        import app.assistant.tests.test_setup  # noqa: F401
        print(compare(sys.argv[2] if len(sys.argv) > 2 else _day(datetime.now(timezone.utc))))
