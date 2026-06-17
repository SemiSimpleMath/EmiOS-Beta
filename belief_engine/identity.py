"""v1 belief identity helpers — stable short ids + merge provenance (emi.db).

Short id: a compact, LLM-citable handle 'b<n>' assigned ONCE per belief and never reused or
changed (monotonic counter). A merged-away belief keeps its short id, so it stays citable for
provenance. `belief_merges` records the redirect from a merged-away (loser) belief to the survivor
it folded into.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from app.assistant.utils.path_utils import get_repo_root


def _db_path() -> str:
    return str(get_repo_root() / "emi.db")


def ensure_tables(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS belief_short_id ("
        " belief_id TEXT PRIMARY KEY, short_id INTEGER NOT NULL UNIQUE, assigned_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS belief_merges ("
        " loser_id TEXT PRIMARY KEY, survivor_id TEXT NOT NULL, merged_at TEXT, reason TEXT)"
    )
    conn.commit()


def assign_short_ids(conn=None) -> int:
    """Assign a monotonic short_id to every belief (active OR deprecated) that lacks one. Idempotent
    — existing ids are never reused or changed. Returns how many were newly assigned."""
    own = conn is None
    if own:
        conn = sqlite3.connect(_db_path())
    try:
        ensure_tables(conn)
        missing = [r[0] for r in conn.execute(
            "SELECT b.id FROM user_beliefs b LEFT JOIN belief_short_id s ON s.belief_id=b.id "
            "WHERE s.belief_id IS NULL ORDER BY b.created_at, b.id")]
        if not missing:
            return 0
        start = int(conn.execute(
            "SELECT COALESCE(MAX(short_id),0) FROM belief_short_id").fetchone()[0]) + 1
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            "INSERT INTO belief_short_id (belief_id, short_id, assigned_at) VALUES (?,?,?)",
            [(bid, start + i, now) for i, bid in enumerate(missing)])
        conn.commit()
        return len(missing)
    finally:
        if own:
            conn.close()


def ensure_short_id(belief_id: str, conn=None) -> str:
    """Assign a short id to ONE belief if it lacks one (cheap, indexed). Returns 'b<n>'. Used at
    belief creation so a new belief is citable immediately; idempotent on an existing belief."""
    own = conn is None
    if own:
        conn = sqlite3.connect(_db_path())
    try:
        ensure_tables(conn)
        r = conn.execute("SELECT short_id FROM belief_short_id WHERE belief_id=?", (belief_id,)).fetchone()
        if r:
            return f"b{r[0]}"
        nxt = int(conn.execute("SELECT COALESCE(MAX(short_id),0) FROM belief_short_id").fetchone()[0]) + 1
        conn.execute("INSERT INTO belief_short_id (belief_id, short_id, assigned_at) VALUES (?,?,?)",
                     (belief_id, nxt, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        return f"b{nxt}"
    finally:
        if own:
            conn.close()


def short_id_for(conn, belief_id: str) -> Optional[str]:
    r = conn.execute("SELECT short_id FROM belief_short_id WHERE belief_id=?", (belief_id,)).fetchone()
    return f"b{r[0]}" if r else None


def belief_id_for_short(conn, short: str) -> Optional[str]:
    s = str(short or "").strip().lower()
    if s.startswith("b"):
        s = s[1:]
    if not s.isdigit():
        return None
    r = conn.execute("SELECT belief_id FROM belief_short_id WHERE short_id=?", (int(s),)).fetchone()
    return r[0] if r else None


def record_merge(loser_id: str, survivor_id: str, reason: str = "", conn=None) -> None:
    """Record a merge redirect (loser -> survivor) for provenance."""
    own = conn is None
    if own:
        conn = sqlite3.connect(_db_path())
    try:
        ensure_tables(conn)
        conn.execute(
            "INSERT OR REPLACE INTO belief_merges (loser_id, survivor_id, merged_at, reason) "
            "VALUES (?,?,?,?)",
            (loser_id, survivor_id, datetime.now(timezone.utc).isoformat(), (reason or "")[:300]))
        conn.commit()
    finally:
        if own:
            conn.close()
