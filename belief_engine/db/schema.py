"""
Belief Engine — SQLite schema.

Six tables:
  user_beliefs         — one row per belief, the canonical statement + metadata
  belief_evidence      — one row per piece of evidence that touched a belief
  belief_tags          — one row per (belief, tag); the standardized retrieval vocabulary
  belief_short_id      — stable, never-reused 'b<n>' handle per belief
  belief_merges        — provenance redirect from a merged-away belief to its survivor
  belief_distinct_pairs — durable "not the same" merge-verifier verdicts (statement-hash bound)

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
    locked              INTEGER NOT NULL DEFAULT 0,      -- owner lock: 1 = engine must not modify/deprecate (manual correction via /beliefs)
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

-- Standardized retrieval tags (configs/belief_tags.yaml). The ADDITIVE retrieval layer;
-- `domain` stays the derivation lane. Multi-label: a belief carries every tag that applies.
CREATE TABLE IF NOT EXISTS belief_tags (
    belief_id   TEXT NOT NULL REFERENCES user_beliefs(id) ON DELETE CASCADE,
    tag         TEXT NOT NULL,          -- from the standardized vocab
    assigned_at TEXT,                   -- ISO8601 (used to detect stale tags after a statement change)
    method      TEXT,                   -- categorizer | manual | domain
    PRIMARY KEY (belief_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_belief_tags_tag ON belief_tags(tag);

-- Stable short id (b<n>): a compact, LLM-citable handle assigned ONCE per belief, never reused or
-- changed. A merged-away belief keeps its short id so it stays citable for provenance.
CREATE TABLE IF NOT EXISTS belief_short_id (
    belief_id   TEXT PRIMARY KEY REFERENCES user_beliefs(id) ON DELETE CASCADE,
    short_id    INTEGER NOT NULL UNIQUE,
    assigned_at TEXT
);

-- Merge provenance: a redirect from a merged-away (loser) belief to the survivor it folded into.
CREATE TABLE IF NOT EXISTS belief_merges (
    loser_id    TEXT PRIMARY KEY REFERENCES user_beliefs(id) ON DELETE CASCADE,
    survivor_id TEXT NOT NULL REFERENCES user_beliefs(id) ON DELETE CASCADE,
    merged_at   TEXT,
    reason      TEXT
);

-- Durable merge-verifier "not the same" verdicts. A verdict is bound to the two STATEMENTS it
-- judged (their hashes): the pair is skipped while both statements are unchanged, and either
-- statement evolving invalidates the verdict so the pair is re-judged. This is what lets an
-- interrupted canonicalize sweep resume instead of re-paying every prior verdict.
CREATE TABLE IF NOT EXISTS belief_distinct_pairs (
    pair_key    TEXT PRIMARY KEY,   -- min(belief_id)|max(belief_id)
    hash_a      TEXT NOT NULL,      -- statement hash of the lexicographically-smaller id
    hash_b      TEXT NOT NULL,
    decided_at  TEXT,
    reason      TEXT
);
"""
