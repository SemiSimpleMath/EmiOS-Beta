"""The brain's inbox: every event the subconscious should know about, and where the gate sent it.

Before this, the noticer saw the household through samples chosen by recipe — the last 4 days of
user messages between 60 and 220 characters, newest 12; chat clusters as one-line titles — and on
2026-09-28 "OK I HAVE GIVEN [THE DOGS] THEIR FLEA MEDICATION!" (55 characters) never reached
it, so the flea concern it settled stayed live. Nothing reported to the brain; it pulled a sample.

Now events REPORT. Each source writes one row per thing that happened, keyed by the source's own
id so it lands exactly once. Chat: every user message, verbatim, whatever its length, except in
rooms the brain does not read (EXCLUDED_ROOM_PREFIXES). Email: every email the parser scored at least
MIN_EMAIL_IMPORTANCE, from its pod, full body; the pod id is the ref. Camera: every frame
image_ingest flagged notable (CAMERA_SOURCE_KINDS), as the vision model described it; the pod id is
the ref, the camera the speaker (2026-10-05; until then they reached only dayflow's pod intake). Presence: the
owner leaving or coming back to the computer after an absence of PRESENCE_AWAY or more, from the AFK monitor's
keyboard spells (2026-10-07). Calendar: an event added to, changed on or removed from the calendar of the coming week
as the calendar sync keeps it, found by comparing it with what was last seen (2026-10-09, docs/design/
hub_and_spokes_2026-10-09.md gap 2; until then a calendar change woke dayflow and reached the brain only as context
beside some other event). Tickets: the owner's reply to every ticket the assistant raised (a scheduled reminder, a
notice, a question, a dayflow task's ask) (2026-10-09), except a reply to a task of work connected to a concern: that
work reports to the brain (concern_feedback.reports_to_brain) and its report carries the reply. Until then a reply
such as "you do this every night" to a question, or "stop reminding me" to a reminder, reached no decider. Noticer:
each issue the daily noticer raises about the long term (a pattern across weeks, a need a date ahead brings), as the
noticer worded it (2026-10-09; until then the noticer wrote concerns itself). The gate
(subconscious/gate.py) routes each row against the open concerns once it is ready (`ready`): an
email or camera event at once, a chat room's messages once the room has been quiet for QUIET — `concern` (bears
on named concerns), `new_matter`, or `none`. Work: dayflow's judgments and endings of work attached
to a concern (`add_work_event`) arrive routed to those concerns by id, never gated. The brain step
(subconscious/brain_step.py) reads what the gate passed on, one matter at a time, inside the
conversations it happened in, then marks it consumed. The `replying_to` column is no longer
written: the conversation shows what a message answers. `noticer_decision`/`noticer_reason` hold
the noticer's decisions from before 2026-09-30; the brain's decisions live in `brain_matters`.
Nothing is decided here: the inbox records, the gate proposes, the brain decides. A `none` row
stays in the table, so what the gate held back can be audited.

The brain's own output never enters: chat intake takes role='user' rows only, so the digest, the
noticer's questions and the assistant's replies cannot come back as events.

Table `brain_events` in emi.db; writes go through db_manager in one short transaction each.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.assistant.subconscious.db import connect as _connect
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

SCHEMA = """CREATE TABLE IF NOT EXISTS brain_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    room_id TEXT,
    speaker TEXT,
    text TEXT NOT NULL,
    replying_to TEXT,
    received_at TEXT NOT NULL,
    gate_status TEXT NOT NULL DEFAULT 'pending',
    route TEXT,
    concern_ids TEXT,
    gate_reasoning TEXT,
    gated_at TEXT,
    gate_error TEXT,
    consumed_at TEXT,
    noticer_decision TEXT,
    noticer_reason TEXT,
    UNIQUE (source, source_ref))"""
# gate_status: pending (not yet routed) | routed | failed (the gate could not route it; the noticer
# still sees it). route: concern | new_matter | none.

# First run only: how far back chat intake reaches when the inbox has no chat rows yet.
_FIRST_RUN_LOOKBACK = timedelta(hours=72)

# Owner, 2026-09-30: a conversation is finished when the owner has been quiet in the room this long;
# the brain reads it then, as one chunk, not in slices while it is still going.
QUIET = timedelta(minutes=5)
# Owner, 2026-09-30: the brain takes email the parser scored 6 or more. The fetch keeps 5 and up;
# over 2026-09-27..30 every 5 was a newspaper newsletter or sale, and the gate passed on none of them.
MIN_EMAIL_IMPORTANCE = 6
# Owner, 2026-09-30: Slack is relaxed talk with friends, not something to act on. Left out for now.
EXCLUDED_ROOM_PREFIXES = ("slack/",)
# Owner, 2026-10-05: the brain takes the camera frames image_ingest flags notable. Raw doorbell
# events (every ring and motion) stay out.
CAMERA_SOURCE_KINDS = ("ring_doorbell_significant", "ring_bedroom_notable")
# Owner, 2026-10-07: noticing the owner leave or come back to the computer is the brain's job, it being an event;
# an absence of this long or more is one. Shorter ones (a coffee, a call) only show in the situation's presence line.
# The gate's and the brain's system prompts state the same minutes in their presence paragraphs.
PRESENCE_AWAY = timedelta(minutes=10)
# First run only: how far back presence intake reaches (an older absence is history, not news).
_FIRST_PRESENCE_LOOKBACK = timedelta(hours=1)
# Every later run looks back this far; the event ids make it idempotent. The keyboard spells are kept 7 days.
_PRESENCE_LOOKBACK = timedelta(hours=24)
# First run only: how far back ticket intake reaches (older replies are history, not news).
_FIRST_TICKET_LOOKBACK = timedelta(hours=1)


# The calendar as the brain last saw it, one row per stored calendar event (an occurrence of a recurring series is its
# own event); `seen_at` is the time of the look that saw it.
CALENDAR_SCHEMA = """CREATE TABLE IF NOT EXISTS brain_calendar_seen (
    record_id TEXT PRIMARY KEY,
    series_id TEXT,
    title TEXT NOT NULL,
    start TEXT,
    end_ TEXT,
    all_day INTEGER NOT NULL,
    location TEXT NOT NULL,
    seen_at TEXT NOT NULL)"""


def ensure_schema(connect=None) -> None:
    connect = connect or _connect
    with connect(True) as c:
        c.execute(SCHEMA)
        c.execute(CALENDAR_SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(brain_events)")}
        for col in ("noticer_decision", "noticer_reason"):   # added after the first rows were written
            if col not in cols:
                c.execute(f"ALTER TABLE brain_events ADD COLUMN {col} TEXT")
        c.execute("CREATE INDEX IF NOT EXISTS brain_events_status ON brain_events(gate_status, consumed_at)")


def _iso(dt: datetime) -> str:
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()


# ── sources ─────────────────────────────────────────────────────────────────

def ingest_chat(*, now_utc: Optional[datetime] = None, connect=None, fetch=None) -> int:
    """Every user message since the last one ingested, verbatim, except in excluded rooms. Returns
    rows added.

    `fetch(since_utc)` returns the messages as dicts (id, timestamp, room_id, speaker, text);
    production reads unified_log_2026. Idempotent on the message id.
    """
    connect = connect or _connect
    ensure_schema(connect)
    now_utc = now_utc or datetime.now(timezone.utc)
    with connect(False) as c:
        last = c.execute("SELECT MAX(occurred_at) FROM brain_events WHERE source='chat'").fetchone()[0]
    since = datetime.fromisoformat(last) if last else now_utc - _FIRST_RUN_LOOKBACK
    rows = (fetch or _fetch_user_messages)(since)
    received = _iso(now_utc)
    added = 0
    with connect(True) as c:
        for m in rows:
            text = (m.get("text") or "").strip()
            if not text or str(m.get("room_id") or "").startswith(EXCLUDED_ROOM_PREFIXES):
                continue
            cur = c.execute(
                "INSERT OR IGNORE INTO brain_events (source, source_ref, occurred_at, room_id, speaker, text, "
                "received_at) VALUES ('chat', ?, ?, ?, ?, ?, ?)",
                (f"message:{m['id']}", _iso(m["timestamp"]), m.get("room_id"), m.get("speaker"), text,
                 received))
            added += cur.rowcount
    if added:
        logger.info("[brain_inbox] ingested %d chat message(s) since %s", added, since.isoformat())
    return added


def _fetch_user_messages(since_utc: datetime) -> List[Dict[str, Any]]:
    """User messages after `since_utc` (every room, oldest first)."""
    from app.assistant.database.db_handler import UnifiedLog2026
    from app.models.base import get_session
    since = since_utc.astimezone(timezone.utc)  # UTCDateTime binds aware values only
    session = get_session()
    try:
        rows = (session.query(UnifiedLog2026)
                .filter(UnifiedLog2026.role == "user", UnifiedLog2026.timestamp > since)
                .order_by(UnifiedLog2026.timestamp.asc()).all())
        return [{"id": r.id, "timestamp": r.timestamp, "room_id": r.room_id,
                 "speaker": r.speaker_name or "user", "text": r.message or ""} for r in rows]
    finally:
        session.close()


def ingest_email(*, now_utc: Optional[datetime] = None, connect=None, fetch=None) -> int:
    """Every email pod created since the last email ingested, scored at least MIN_EMAIL_IMPORTANCE.
    Returns rows added.

    `fetch(since_utc)` returns email records (pod_id, created_at, received_at_utc, sender_display,
    sender_email, subject, body); production reads the pod store. The cursor is the latest ingest
    time, taken before the fetch, so a pod created during a run is seen by the next one;
    idempotent on the pod id. Sender line, text and time come from email_pods, as every reader shows them.
    """
    from app.assistant.pod_store.email_pods import email_sender_line, email_text, email_time, emails_created_since
    connect = connect or _connect
    ensure_schema(connect)
    now_utc = now_utc or datetime.now(timezone.utc)
    with connect(False) as c:
        last = c.execute("SELECT MAX(received_at) FROM brain_events WHERE source='email'").fetchone()[0]
    since = datetime.fromisoformat(last) if last else now_utc - _FIRST_RUN_LOOKBACK
    records = (fetch or emails_created_since)(since)
    received = _iso(now_utc)
    added = 0
    with connect(True) as c:
        for r in records:
            if int(r["importance"]) < MIN_EMAIL_IMPORTANCE:
                continue
            cur = c.execute(
                "INSERT OR IGNORE INTO brain_events (source, source_ref, occurred_at, room_id, speaker, text, "
                "received_at) VALUES ('email', ?, ?, NULL, ?, ?, ?)",
                (r["pod_id"], _iso(datetime.fromisoformat(email_time(r))), email_sender_line(r), email_text(r),
                 received))
            added += cur.rowcount
    if added:
        logger.info("[brain_inbox] ingested %d email(s) since %s", added, since.isoformat())
    return added


def ingest_camera(*, now_utc: Optional[datetime] = None, connect=None, fetch=None) -> int:
    """Every camera frame image_ingest flagged notable (CAMERA_SOURCE_KINDS) since the last one
    ingested: the vision model's description of it, the camera as speaker, the pod id as the ref.
    Returns rows added.

    `fetch(since_utc)` returns the image pods created after `since_utc`; production reads the pod
    store. Cursor and idempotency as for email."""
    connect = connect or _connect
    ensure_schema(connect)
    now_utc = now_utc or datetime.now(timezone.utc)
    with connect(False) as c:
        last = c.execute("SELECT MAX(received_at) FROM brain_events WHERE source='camera'").fetchone()[0]
    since = datetime.fromisoformat(last) if last else now_utc - _FIRST_RUN_LOOKBACK
    pods = (fetch or _fetch_image_pods)(since)
    received = _iso(now_utc)
    added = 0
    with connect(True) as c:
        for pod in pods:
            meta = pod.metadata or {}
            if meta.get("source_kind") not in CAMERA_SOURCE_KINDS:
                continue
            text = (pod.body or pod.one_liner or "").strip()
            if not text:
                raise ValueError(f"camera pod {pod.pod_id} has no description")
            reason = meta.get("significance_reason") or meta.get("importance_reason")
            if reason:
                text += f"\n(flagged because: {reason})"
            cur = c.execute(
                "INSERT OR IGNORE INTO brain_events (source, source_ref, occurred_at, room_id, speaker, text, "
                "received_at) VALUES ('camera', ?, ?, NULL, ?, ?, ?)",
                (pod.pod_id, _iso(datetime.fromisoformat(meta["occurred_at_utc"])), meta["camera_name"], text,
                 received))
            added += cur.rowcount
    if added:
        logger.info("[brain_inbox] ingested %d camera event(s) since %s", added, since.isoformat())
    return added


def _fetch_image_pods(since_utc: datetime) -> list:
    from app.assistant.pod_store.pod_store import PodStore
    since = since_utc if since_utc.tzinfo else since_utc.replace(tzinfo=timezone.utc)
    return PodStore().query(kind="image", since_utc=since, limit=None)


def _minutes_text(delta: timedelta) -> str:
    minutes = int(round(delta.total_seconds() / 60))
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{minutes} minutes"
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def ingest_presence(*, now_utc: Optional[datetime] = None, connect=None, fetch=None) -> int:
    """The owner leaving or coming back to the computer, when the absence is PRESENCE_AWAY or more (2026-10-07).
    Returns rows added.

    Read from the keyboard spells the AFK monitor keeps (afk_db active_segments; an absence is the gap between two
    spells), so a restart loses nothing. `away`: the latest spell is closed and ended PRESENCE_AWAY or more ago; the
    event is at its end, when the owner was last at the computer (the monitor closes a spell once input has stopped
    for its confirmed-AFK minutes). `back`: a spell began PRESENCE_AWAY or more after the one before ended; the event
    is at its start, with how long the owner was away. `fetch(start_utc, end_utc)` returns the spells overlapping the
    range as dicts (id, start, end: aware UTC datetimes; provisional: still open). Idempotent on the spell id.
    Known limit: while the app is down no spell is recorded, so a downtime of more than the monitor's 30-minute
    recovery window reads as an absence."""
    connect = connect or _connect
    ensure_schema(connect)
    now_utc = now_utc or datetime.now(timezone.utc)
    with connect(False) as c:
        first = c.execute("SELECT MIN(received_at) FROM brain_events WHERE source='presence'").fetchone()[0]
    # Never before the first run's reach, so an absence it left out as history does not arrive on a later run.
    start = datetime.fromisoformat(first) if first else now_utc
    since = max(now_utc - _PRESENCE_LOOKBACK, start - _FIRST_PRESENCE_LOOKBACK)
    spells = sorted((fetch or _fetch_keyboard_spells)(since - timedelta(days=7), now_utc), key=lambda s: s["start"])
    rows = []
    for before, after in zip(spells, spells[1:]):
        gap = after["start"] - before["end"]
        if gap >= PRESENCE_AWAY and after["start"] >= since:
            rows.append((f"presence:back:{after['id']}", after["start"],
                         f"Came back to the computer after {_minutes_text(gap)} away."))
    if spells:
        last = spells[-1]
        if not last["provisional"] and now_utc - last["end"] >= PRESENCE_AWAY and last["end"] >= since:
            rows.append((f"presence:away:{last['id']}", last["end"],
                         "Left the computer: no keyboard or mouse use since."))
    received = _iso(now_utc)
    added = 0
    with connect(True) as c:
        for ref, at, text in rows:
            cur = c.execute(
                "INSERT OR IGNORE INTO brain_events (source, source_ref, occurred_at, room_id, speaker, text, "
                "received_at) VALUES ('presence', ?, ?, NULL, 'computer', ?, ?)",
                (ref, _iso(at), text, received))
            added += cur.rowcount
    if added:
        logger.info("[brain_inbox] ingested %d presence event(s)", added)
    return added


def _calendar_when(start: Optional[str], end: Optional[str], all_day: bool) -> str:
    from app.assistant.utils.time_utils import local_time_text, utc_to_local
    if not start:
        return "(no time recorded)"
    if all_day:
        return utc_to_local(start).strftime("%a %Y-%m-%d") + " (all day)"
    return local_time_text(start) + (f" to {local_time_text(end)}" if end else "")


def _calendar_line(title: str, start: Optional[str], end: Optional[str], all_day: bool, location: str) -> str:
    return f"{title}, {_calendar_when(start, end, all_day)}" + (f", at {location}" if location else "")


def ingest_calendar(*, now_utc: Optional[datetime] = None, connect=None, fetch=None) -> int:
    """What changed on the calendar of the coming week since the last look (2026-10-09). Returns rows added.

    `fetch()` returns the stored calendar events (the calendar sync's records: id, summary, start, end, location,
    is_all_day, recurring_event_id, created, updated; production reads the event repository). Compared with what was
    last seen (brain_calendar_seen):
    - an event not seen before: "added" when it was created since the last look; "on the calendar this week" when it
      was made earlier and has come into the week; nothing for another occurrence of a series already seen (a weekly
      series shows a new occurrence every day);
    - a seen event whose title, time, all-day or place changed: "changed", with what it was;
    - a seen event gone from the calendar while its time is still ahead of the sync's window start (local midnight
      today): "removed". One that ended before today leaves the window, which is not a change.
    The first look records the calendar and reports nothing. Idempotent: each event's version is its ref."""
    from app.assistant.utils.time_utils import get_local_timezone
    connect = connect or _connect
    ensure_schema(connect)
    now_utc = now_utc or datetime.now(timezone.utc)
    records = (fetch or _fetch_calendar)()
    with connect(False) as c:
        seen = {r["record_id"]: dict(r) for r in c.execute("SELECT * FROM brain_calendar_seen")}
        last = c.execute("SELECT MAX(seen_at) FROM brain_calendar_seen").fetchone()[0]
    now_iso = _iso(now_utc)
    current = {}
    for r in records:
        current[str(r["id"])] = {
            "record_id": str(r["id"]), "series_id": r.get("recurring_event_id") or None,
            "title": str(r.get("summary") or "(no title)"), "start": r.get("start") or None, "end_": r.get("end") or None,
            "all_day": 1 if r.get("is_all_day") else 0, "location": str(r.get("location") or ""),
            "created": r.get("created") or "", "updated": r.get("updated") or ""}
    rows = []
    if last is not None:
        known_series = {s["series_id"] for s in seen.values() if s["series_id"]}
        for rid, e in current.items():
            line = _calendar_line(e["title"], e["start"], e["end_"], bool(e["all_day"]), e["location"])
            version = "|".join(str(e[k]) for k in ("title", "start", "end_", "all_day", "location"))
            before = seen.get(rid)
            if before is None:
                if e["series_id"] and e["series_id"] in known_series:
                    continue
                created = datetime.fromisoformat(e["created"].replace("Z", "+00:00")) if e["created"] else None
                if created is not None and created >= datetime.fromisoformat(last):
                    rows.append((f"calendar:{rid}|added|{version}", f"Added to the calendar: {line}."))
                else:
                    rows.append((f"calendar:{rid}|in_week|{version}", f"On the calendar this week: {line}."))
            elif any(before[k] != e[k] for k in ("title", "start", "end_", "all_day", "location")):
                was = _calendar_line(before["title"], before["start"], before["end_"], bool(before["all_day"]),
                                     before["location"])
                rows.append((f"calendar:{rid}|changed|{version}", f"Changed on the calendar: {line}. It was: {was}."))
        window_start = now_utc.astimezone(get_local_timezone()).replace(hour=0, minute=0, second=0, microsecond=0)
        for rid, before in seen.items():
            if rid in current or not before["start"]:
                continue
            if datetime.fromisoformat(before["start"].replace("Z", "+00:00")) < window_start:
                continue
            line = _calendar_line(before["title"], before["start"], before["end_"], bool(before["all_day"]),
                                  before["location"])
            rows.append((f"calendar:{rid}|removed|{before['start']}", f"Removed from the calendar: {line}."))
    added = 0
    with connect(True) as c:
        for ref, text in rows:
            cur = c.execute(
                "INSERT OR IGNORE INTO brain_events (source, source_ref, occurred_at, room_id, speaker, text, "
                "received_at) VALUES ('calendar', ?, ?, NULL, 'calendar', ?, ?)",
                (ref, now_iso, text, now_iso))
            added += cur.rowcount
        c.execute("DELETE FROM brain_calendar_seen")
        c.executemany("INSERT INTO brain_calendar_seen (record_id, series_id, title, start, end_, all_day, location, "
                      "seen_at) VALUES (?,?,?,?,?,?,?,?)",
                      [(e["record_id"], e["series_id"], e["title"], e["start"], e["end_"], e["all_day"], e["location"],
                        now_iso) for e in current.values()])
    if added:
        logger.info("[brain_inbox] ingested %d calendar change(s)", added)
    return added


def ingest_tickets(*, now_utc: Optional[datetime] = None, connect=None, fetch=None, reports=None) -> int:
    """Every ticket the owner answered since the last one ingested, except a dayflow task's ticket whose work reports
    to the brain (`reports(work_id)`, concern_feedback.reports_to_brain): its report carries the reply. Returns rows
    added.

    `fetch(since_utc)` returns the answered tickets as dicts (ticket_id, ticket_type, title, message, user_action,
    user_text, responded_at: aware UTC, work_id: the work that raised it or None); production reads the tickets
    table. The event is the ticket's words and the
    owner's reply, at the reply's time; ref `ticket:<id>:<responded_at>`, so an answer given again lands again."""
    connect = connect or _connect
    ensure_schema(connect)
    now_utc = now_utc or datetime.now(timezone.utc)
    with connect(False) as c:
        last = c.execute("SELECT MAX(occurred_at) FROM brain_events WHERE source='ticket'").fetchone()[0]
    since = datetime.fromisoformat(last) if last else now_utc - _FIRST_TICKET_LOOKBACK
    rows = (fetch or _fetch_answered_tickets)(since)
    if reports is None:
        from app.assistant.subconscious.concern_feedback import reports_to_brain as reports
    # Decided before the write: `reports` reads the concern register, and a read inside the write would wait on the
    # writer slot this write holds (2026-10-09: the brain's thread waited on itself and every writer queued behind it).
    rows = [t for t in rows if not (t.get("work_id") and reports(t["work_id"]))]
    received = _iso(now_utc)
    added = 0
    with connect(True) as c:
        for t in rows:
            reply = str(t.get("user_text") or "").strip()
            action = str(t.get("user_action") or "").strip()
            text = (f"Ticket ({t['ticket_type']}): {str(t.get('title') or '').strip()}\n"
                    f"{str(t.get('message') or '').strip()}\n"
                    f"The owner replied: {reply or '(no words)'}" + (f" (action: {action})" if action else ""))
            cur = c.execute(
                "INSERT OR IGNORE INTO brain_events (source, source_ref, occurred_at, room_id, speaker, text, "
                "received_at) VALUES ('ticket', ?, ?, NULL, 'owner', ?, ?)",
                (f"ticket:{t['ticket_id']}:{_iso(t['responded_at'])}", _iso(t["responded_at"]), text, received))
            added += cur.rowcount
    if added:
        logger.info("[brain_inbox] ingested %d ticket reply(ies) since %s", added, since.isoformat())
    return added


def add_noticer_issues(issues: List[Dict[str, Any]], *, run_at: datetime, connect=None) -> int:
    """The issues one noticer pass raised (subconscious/routine_handlers noticer_run, 2026-10-09): each an event for the
    brain, in the noticer's words: its title, what it is and why now, with its kind and whom it is about. The brain
    decides whether it becomes a concern. Ref `noticer:<run>:<n>`, so a pass lands once. Returns rows added."""
    connect = connect or _connect
    ensure_schema(connect)
    at = _iso(run_at)
    added = 0
    with connect(True) as c:
        for n, issue in enumerate(issues, 1):
            text = (f"{issue['title']}\n{issue['what']}\nWhy now: {issue['why_now']}\n"
                    f"({str(issue['kind']).replace('_', ' ')}; about {issue['subject']})")
            cur = c.execute(
                "INSERT OR IGNORE INTO brain_events (source, source_ref, occurred_at, room_id, speaker, text, "
                "received_at) VALUES ('noticer', ?, ?, NULL, 'noticer', ?, ?)",
                (f"noticer:{at}:{n}", at, text, at))
            added += cur.rowcount
    if added:
        logger.info("[brain_inbox] the noticer raised %d issue(s)", added)
    return added


def _fetch_answered_tickets(since_utc: datetime) -> List[Dict[str, Any]]:
    """Tickets answered after `since_utc`, oldest first. responded_at is stored as naive UTC."""
    since = since_utc.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    with _connect(False) as c:
        rows = c.execute("SELECT ticket_id, ticket_type, title, message, user_action, user_text, responded_at, "
                         "json_extract(trigger_context, '$.work_node') AS work_node "
                         "FROM tickets WHERE responded_at IS NOT NULL AND responded_at > ? ORDER BY responded_at",
                         (since,)).fetchall()
    return [{**{k: r[k] for k in r.keys() if k != "work_node"},
             "work_id": str(r["work_node"]).split("::")[0] if r["work_node"] else None,
             "responded_at": datetime.fromisoformat(str(r["responded_at"])).replace(tzinfo=timezone.utc)}
            for r in rows]


def _fetch_calendar() -> List[Dict[str, Any]]:
    from app.assistant.event_repository.event_repository import EventRepositoryManager
    return [r["data"] for r in json.loads(EventRepositoryManager().search_events(data_type="calendar"))]


def next_presence_due(*, now_utc: datetime, fetch=None) -> Optional[datetime]:
    """When the current absence reaches PRESENCE_AWAY, so the brain wakes for it then; None while the owner is at the
    computer or once it has."""
    spells = sorted((fetch or _fetch_keyboard_spells)(now_utc - timedelta(days=1), now_utc), key=lambda s: s["start"])
    if not spells or spells[-1]["provisional"]:
        return None
    due = spells[-1]["end"] + PRESENCE_AWAY
    return due if due > now_utc else None


def _fetch_keyboard_spells(start_utc: datetime, end_utc: datetime) -> List[Dict[str, Any]]:
    from app.assistant.afk_manager.afk_db import get_active_segments_overlapping_range

    def aware(dt: datetime) -> datetime:
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return [{"id": s.id, "start": aware(s.start_time), "end": aware(s.end_time), "provisional": bool(s.is_provisional)}
            for s in get_active_segments_overlapping_range(start_utc, end_utc, include_provisional=True)]


def add_work_event(*, work_id: str, receipt_id: str, occurred_at: str, text: str, connect=None) -> bool:
    """What dayflow did reports here: each finalizer judgment and each ending of every work object
    (concern_feedback.py). The event is pending like a chat message or an email: the gate routes it to
    every open concern its outcome bears on. The text names the concerns the work was made for, as the
    gate's hint (docs/design/what_was_done_reaches_the_brain_2026-10-03.md; until then work reported only
    when it cited a concern, already routed to those). Idempotent on the receipt. Returns True when the
    row is new."""
    connect = connect or _connect
    ensure_schema(connect)
    now = _iso(datetime.now(timezone.utc))
    with connect(True) as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO brain_events (source, source_ref, occurred_at, room_id, speaker, text, "
            "received_at) VALUES ('work', ?, ?, NULL, 'dayflow', ?, ?)",
            (f"work:{work_id}:{receipt_id}", occurred_at, text, now))
    return cur.rowcount == 1


# ── the gate's side ─────────────────────────────────────────────────────────

def pending(connect=None) -> List[Dict[str, Any]]:
    """Events the gate has not routed yet, oldest first."""
    connect = connect or _connect
    ensure_schema(connect)
    with connect(False) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM brain_events WHERE gate_status='pending' ORDER BY occurred_at, id")]


def ready(events: List[Dict[str, Any]], now_utc: datetime) -> List[Dict[str, Any]]:
    """The events whose matter is complete: every email, camera, presence, calendar, ticket, noticer and work event, and the chat of every room whose latest
    message is at least QUIET old. Messages of a room still talking wait, all of them together."""
    last = _last_message_by_room(events)
    return [e for e in events
            if e["source"] != "chat" or last[e["room_id"]] + QUIET <= now_utc]


def next_ready_at(events: List[Dict[str, Any]], now_utc: datetime) -> Optional[datetime]:
    """When the next room still talking goes quiet, or None when no chat is waiting."""
    waiting = [t + QUIET for t in _last_message_by_room(events).values() if t + QUIET > now_utc]
    return min(waiting) if waiting else None


def _last_message_by_room(events: List[Dict[str, Any]]) -> Dict[str, datetime]:
    last: Dict[str, datetime] = {}
    for e in events:
        if e["source"] == "chat":
            at = datetime.fromisoformat(e["occurred_at"])
            last[e["room_id"]] = max(at, last.get(e["room_id"], at))
    return last


def record_route(event_id: int, route: str, concern_ids: List[str], reasoning: str, connect=None) -> None:
    connect = connect or _connect
    if route not in ("concern", "new_matter", "none"):
        raise ValueError(f"unknown route {route!r}")
    with connect(True) as c:
        c.execute("UPDATE brain_events SET gate_status='routed', route=?, concern_ids=?, gate_reasoning=?, "
                  "gated_at=?, gate_error=NULL WHERE id=?",
                  (route, json.dumps(concern_ids), reasoning, _iso(datetime.now(timezone.utc)), event_id))


def record_failure(event_ids: List[int], error: str, connect=None) -> None:
    """The gate could not route these. They stay visible to the noticer (a failed route must not
    drop an event) and are not retried automatically."""
    connect = connect or _connect
    with connect(True) as c:
        c.executemany("UPDATE brain_events SET gate_status='failed', gate_error=?, gated_at=? WHERE id=?",
                      [(error, _iso(datetime.now(timezone.utc)), i) for i in event_ids])
