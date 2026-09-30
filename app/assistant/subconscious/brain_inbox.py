"""The brain's inbox: every event the subconscious should know about, and where the gate sent it.

Before this, the noticer saw the household through samples chosen by recipe — the last 4 days of
user messages between 60 and 220 characters, newest 12; chat clusters as one-line titles — and on
2026-09-28 "OK I HAVE GIVEN [THE DOGS] THEIR FLEA MEDICATION!" (55 characters) never reached
it, so the flea concern it settled stayed live. Nothing reported to the brain; it pulled a sample.

Now events REPORT. Each source writes one row per thing that happened, keyed by the source's own
id so it lands exactly once. Chat: every user message, verbatim, whatever its length. Email: every
email the fetch keeps (email_parser importance >= 5), from its pod, full body; the pod id is the
ref. The gate (subconscious/gate.py) routes each row against the open concerns —
`concern` (bears on named concerns), `new_matter`, or `none` — and the brain step
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
    """Every user message since the last one ingested, verbatim. Returns rows added.

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
            if not text:
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
    """Every email pod created since the last email ingested. Returns rows added.

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
            cur = c.execute(
                "INSERT OR IGNORE INTO brain_events (source, source_ref, occurred_at, room_id, speaker, text, "
                "received_at) VALUES ('email', ?, ?, NULL, ?, ?, ?)",
                (r["pod_id"], _iso(datetime.fromisoformat(email_time(r))), email_sender_line(r), email_text(r),
                 received))
            added += cur.rowcount
    if added:
        logger.info("[brain_inbox] ingested %d email(s) since %s", added, since.isoformat())
    return added


# ── the gate's side ─────────────────────────────────────────────────────────

def pending(connect=None) -> List[Dict[str, Any]]:
    """Events the gate has not routed yet, oldest first."""
    connect = connect or _connect
    ensure_schema(connect)
    with connect(False) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM brain_events WHERE gate_status='pending' ORDER BY occurred_at, id")]


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
