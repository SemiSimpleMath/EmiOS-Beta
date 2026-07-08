"""Durable "not the same" verdicts from the merge_verifier (belief_distinct_pairs, emi.db).

The canonicalizer's verifier calls are ~95% "not the same" — and those verdicts used to be
re-paid on every sweep. Each one is now recorded, bound to the two STATEMENTS it judged
(their hashes): a pair is skipped while both statements are unchanged, and either statement
evolving (the updater/reevaluator rewrite-in-place, a merge's canonical rewrite) invalidates
the verdict so the pair is re-judged. Same reconsideration discipline as the entity-card
try-and-mark drops.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from belief_engine.db.paths import belief_db_path

_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS belief_distinct_pairs ("
    " pair_key TEXT PRIMARY KEY, hash_a TEXT NOT NULL, hash_b TEXT NOT NULL,"
    " decided_at TEXT, reason TEXT)"
)


def statement_hash(statement: str) -> str:
    normalized = " ".join((statement or "").split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _ordered(id_a: str, stmt_a: str, id_b: str, stmt_b: str) -> Tuple[str, str, str]:
    """(pair_key, hash of smaller-id statement, hash of larger-id statement)."""
    if id_a <= id_b:
        return f"{id_a}|{id_b}", statement_hash(stmt_a), statement_hash(stmt_b)
    return f"{id_b}|{id_a}", statement_hash(stmt_b), statement_hash(stmt_a)


def open_conn(path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or belief_db_path(), timeout=30.0)
    conn.execute(_TABLE_SQL)
    conn.commit()
    return conn


def load_distinct_map(conn: sqlite3.Connection) -> Dict[str, Tuple[str, str]]:
    """One read per pass: {pair_key: (hash_a, hash_b)}."""
    return {r[0]: (r[1], r[2]) for r in
            conn.execute("SELECT pair_key, hash_a, hash_b FROM belief_distinct_pairs")}


def is_recorded_distinct(distinct_map: Dict[str, Tuple[str, str]],
                         id_a: str, stmt_a: str, id_b: str, stmt_b: str) -> bool:
    """True when a 'not the same' verdict exists for this pair AND both statements are
    unchanged since it was rendered."""
    key, ha, hb = _ordered(id_a, stmt_a, id_b, stmt_b)
    recorded = distinct_map.get(key)
    return recorded is not None and recorded == (ha, hb)


def record_distinct(conn: sqlite3.Connection, id_a: str, stmt_a: str, id_b: str, stmt_b: str,
                    reason: str = "") -> None:
    """Persist one 'not the same' verdict immediately — surviving interruption is the point."""
    key, ha, hb = _ordered(id_a, stmt_a, id_b, stmt_b)
    conn.execute(
        "INSERT OR REPLACE INTO belief_distinct_pairs (pair_key, hash_a, hash_b, decided_at, reason)"
        " VALUES (?,?,?,?,?)",
        (key, ha, hb, datetime.now(timezone.utc).isoformat(), (reason or "")[:300]))
    conn.commit()
