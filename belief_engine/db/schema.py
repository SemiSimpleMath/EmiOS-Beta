"""
Belief Engine — SQLite schema.

Two tables:
  user_beliefs       — one row per belief, the canonical statement + metadata
  belief_evidence    — one row per piece of evidence that touched a belief

Completely independent of the existing memory system.
Run schema setup with:
    python -m belief_engine.db.ensure_schema
"""
from __future__ import annotations

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS user_beliefs (
    id                  TEXT PRIMARY KEY,       -- UUID
    domain              TEXT NOT NULL,          -- e.g. routine, health, communication
    belief_key          TEXT NOT NULL UNIQUE,   -- stable slug, e.g. routine.dog_walk.morning
    statement           TEXT NOT NULL,          -- prose, what agents read
    confidence          TEXT NOT NULL,          -- high | medium | low
    scope               TEXT NOT NULL,          -- chronic | temporary
    status              TEXT NOT NULL DEFAULT 'active',  -- active | contested | deprecated
    conditions          TEXT,                   -- JSON, reserved for future structured qualifiers
    observation_count   INTEGER NOT NULL DEFAULT 1,
    first_observed      TEXT,                   -- ISO8601
    last_confirmed      TEXT,                   -- ISO8601
    created_at          TEXT NOT NULL,          -- ISO8601
    updated_at          TEXT NOT NULL           -- ISO8601
);

CREATE INDEX IF NOT EXISTS idx_user_beliefs_domain   ON user_beliefs(domain);
CREATE INDEX IF NOT EXISTS idx_user_beliefs_status   ON user_beliefs(status);
CREATE INDEX IF NOT EXISTS idx_user_beliefs_key      ON user_beliefs(belief_key);

CREATE TABLE IF NOT EXISTS belief_evidence (
    id                  TEXT PRIMARY KEY,       -- UUID
    belief_id           TEXT NOT NULL REFERENCES user_beliefs(id) ON DELETE CASCADE,
    source_type         TEXT NOT NULL,          -- daily_insights | kg_edge | ticket_rejection | manual
    source_date         TEXT,                   -- YYYY-MM-DD of the source day
    source_ref          TEXT,                   -- e.g. message_id, edge_id, ticket_id
    signal_type         TEXT NOT NULL,          -- confirms | qualifies | contradicts | rejects
    summary             TEXT NOT NULL,          -- one-sentence digest of what this evidence says
    raw_text            TEXT,                   -- original sentence/quote if available
    weight              REAL NOT NULL DEFAULT 1.0,  -- 0.0–5.0
    created_at          TEXT NOT NULL           -- ISO8601
);

CREATE INDEX IF NOT EXISTS idx_belief_evidence_belief_id   ON belief_evidence(belief_id);
CREATE INDEX IF NOT EXISTS idx_belief_evidence_source_date ON belief_evidence(source_date);
CREATE INDEX IF NOT EXISTS idx_belief_evidence_signal_type ON belief_evidence(signal_type);
"""
