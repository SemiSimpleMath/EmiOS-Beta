"""Lifecycle terminus for deprecated beliefs.

Deprecation only flips `status='deprecated'` — it never evicts the row, so dead beliefs
otherwise accumulate in the live tables forever (they reached 94% before the first sweep).
This moves every deprecated belief (+ its evidence) OUT of the live tables into the archive
tables in the same emi.db, so the live store stays live-by-construction. Idempotent: archives
whatever is currently deprecated, no-ops when there's nothing.

Live `user_beliefs`/`belief_evidence` keep only active + contested beliefs. `belief_short_id`
(the monotonic short-id counter must never reuse an id) and `belief_merges` (write-only
provenance) stay live; their refs into the archive dangle harmlessly (FK enforcement is off,
so the deletes here don't cascade into them).
"""
from __future__ import annotations

import sqlite3
from typing import Dict, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)

# The deprecated set, reused so the copy and the delete target exactly the same rows.
_DEP = "(SELECT id FROM user_beliefs WHERE status='deprecated')"


def _db_path() -> str:
    return str(get_repo_root() / "emi.db")


def archive_deprecated_beliefs(*, conn: Optional[sqlite3.Connection] = None) -> Dict[str, int]:
    """Move every `status='deprecated'` belief (and its evidence) into the archive tables.

    Returns {'beliefs': n, 'evidence': n}. The move is atomic — a partial archive would split
    a belief from its evidence — so a failure raises and leaves the live tables untouched.
    """
    own = conn is None
    if own:
        conn = sqlite3.connect(_db_path(), timeout=30.0)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")  # deletes must not cascade into provenance
        n_beliefs = conn.execute("SELECT COUNT(*) FROM user_beliefs WHERE status='deprecated'").fetchone()[0]
        if not n_beliefs:
            return {"beliefs": 0, "evidence": 0}
        n_ev = conn.execute(f"SELECT COUNT(*) FROM belief_evidence WHERE belief_id IN {_DEP}").fetchone()[0]
        with conn:  # atomic: copy across, then drop from live
            conn.execute("CREATE TABLE IF NOT EXISTS user_beliefs_archive AS SELECT * FROM user_beliefs WHERE 0")
            conn.execute("CREATE TABLE IF NOT EXISTS belief_evidence_archive AS SELECT * FROM belief_evidence WHERE 0")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_uba_id ON user_beliefs_archive(id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bea_belief ON belief_evidence_archive(belief_id)")
            conn.execute(f"INSERT INTO belief_evidence_archive SELECT * FROM belief_evidence WHERE belief_id IN {_DEP}")
            conn.execute("INSERT INTO user_beliefs_archive SELECT * FROM user_beliefs WHERE status='deprecated'")
            conn.execute(f"DELETE FROM belief_evidence WHERE belief_id IN {_DEP}")
            conn.execute(f"DELETE FROM belief_tags WHERE belief_id IN {_DEP}")
            conn.execute("DELETE FROM user_beliefs WHERE status='deprecated'")
        logger.info("[belief_archive] archived %d deprecated beliefs + %d evidence rows", n_beliefs, n_ev)
        return {"beliefs": n_beliefs, "evidence": n_ev}
    finally:
        if own:
            conn.close()
