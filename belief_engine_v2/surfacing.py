"""Surfacing-outcome log — the contextual-bandit SEED (BELIEF-ENGINE-NEW.md §5, item #12).

The doc's verdict on learned surfacing was "premature — start by just LOGGING
surfaced/used/ignored outcomes." This is that start: every consumer of
`beliefs_for_context` records what it surfaced, to whom, with what score.
`outcome` stays NULL until a feedback path (explicit use/ignore signals, or a
downstream judge) fills it in — the table is the training data any future
bandit (or even a simple per-belief usefulness prior) needs, and it costs a
few rows per planning run.

Same SQLite db as the store; append-only.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable

_DDL = """
CREATE TABLE IF NOT EXISTS surfacing_log (
    id           TEXT PRIMARY KEY,
    surfaced_at  TEXT NOT NULL,
    agent        TEXT NOT NULL,            -- consumer, e.g. 'weekly_meal_planner'
    belief_id    TEXT NOT NULL,
    score        REAL,                     -- retrieval score at surfacing time
    rank         INTEGER,                  -- position in the surfaced list (0-based)
    outcome      TEXT                      -- NULL until used|ignored|rejected lands
);
CREATE INDEX IF NOT EXISTS ix_surfacing_agent_time ON surfacing_log(agent, surfaced_at);
CREATE INDEX IF NOT EXISTS ix_surfacing_belief ON surfacing_log(belief_id);
"""


def log_surfaced(conn, *, agent: str, rows: Iterable[Dict[str, Any]]) -> int:
    """Record one surfacing event per belief row (as returned by
    `beliefs_for_context`). Returns the number of rows logged."""
    conn.executescript(_DDL)
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    for rank, r in enumerate(rows):
        belief_id = r.get("belief_id")
        if not belief_id:
            continue
        conn.execute(
            "INSERT INTO surfacing_log (id, surfaced_at, agent, belief_id, score, rank) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, now, agent, belief_id, r.get("score"), rank),
        )
        n += 1
    conn.commit()
    return n
