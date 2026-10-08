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
keyboard spells (2026-10-07). The gate
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


def ensure_schema(connect=None) -> None:
    connect = connect or _connect
    with connect(True) as c:
        c.execute(SCHEMA)
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


def add_day_ended_events(register: Dict[str, Any], *, now_utc: Optional[datetime] = None, connect=None) -> int:
    """An open concern whose horizon is `today` and whose day has ended reaches the brain, once per concern per day
    (2026-10-08). The brain reads a concern only when an event bears on it, so a concern about a moment ("the pickup
    of [the son] from BAS tonight") stayed open into the next day while nothing new arrived; the time-bound rule
    (shared/brain/time_bound.j2) resolves it once the moment has passed. The event is routed to its concern by code,
    without the gate. Its day is the one the concern was raised on (first_observed, by the day boundary); a concern
    the brain keeps open is asked again on each later day. Returns rows added."""
    from app.assistant.pipelines.context import boundary_date_local_str, day_window_local
    from app.assistant.utils.time_utils import get_local_timezone, local_time_text, parse_iso_utc
    connect = connect or _connect
    ensure_schema(connect)
    now_utc = now_utc or datetime.now(timezone.utc)
    tz = get_local_timezone()
    today = boundary_date_local_str(now_utc.astimezone(tz))
    rows = []
    for bucket in ("active", "addressing"):
        for c in register.get(bucket) or []:
            if str(c.get("horizon") or "").lower() != "today":
                continue
            raised = parse_iso_utc(c["first_observed"])
            day = boundary_date_local_str(raised.astimezone(tz))
            if day >= today:
                continue
            ended = day_window_local(day)[1]
            rows.append((f"day_ended:{c['concern_id']}:{today}", c["concern_id"],
                         f"The day this concern was raised for has ended: raised {local_time_text(raised)} with horizon "
                         f"today; that day ended {local_time_text(ended)}."))
    received = _iso(now_utc)
    added = 0
    with connect(True) as c:
        for ref, cid, text in rows:
            cur = c.execute(
                "INSERT OR IGNORE INTO brain_events (source, source_ref, occurred_at, room_id, speaker, text, received_at, "
                "gate_status, route, concern_ids, gate_reasoning, gated_at) "
                "VALUES ('time', ?, ?, NULL, 'clock', ?, ?, 'routed', 'concern', ?, ?, ?)",
                (ref, received, text, received, json.dumps([cid]), "routed by code: the concern's day has ended",
                 received))
            added += cur.rowcount
    if added:
        logger.info("[brain_inbox] %d concern(s) past their day handed to the brain", added)
    return added


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
    """The events whose matter is complete: every email, camera, presence and work event, and the chat of every room whose latest
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
