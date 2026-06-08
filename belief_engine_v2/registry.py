"""Per-user concept registry (#2) — the canonical, evolving vocabulary.

The anti-proliferation core (scratch/BELIEF-ENGINE-NEW.md §2b, §5). A phrase resolves to a
canonical `concept_id` by: (1) exact alias hit (an already-verified decision), else (2)
embedding nearest-neighbor produces merge CANDIDATES above a recall threshold — *which the
embedding never decides on its own* — and an injected LLM `verifier` checks each candidate
and DECIDES "same or not"; the first verified "yes" wins and the phrase is recorded as an
alias (a persisted "yes"), else (3) mint a new candidate concept. So "kids"/"children"/
"Sam and Robin" collapse to one subject concept ONLY when the verifier confirms it.

The cosine threshold is a candidate-RECALL gate, not a merge: lower τ just hands the verifier
more candidates. **No verifier ⇒ never merge (mint).** This is the fail-safe inverse of an
auto-merge — similarity alone may never collapse two concepts (per the design's principle 5;
this is our native mechanism, not the KG's).

Embedder is injected (`embedder(text)->vector`); with None it's exact-alias-only. Verifier is
injected (`verifier(phrase, candidate_label, ctype)->bool`). Embeddings are held in-memory
(numpy) per concept type for fast NN.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

try:
    import numpy as np
except Exception:  # numpy is a sentence-transformers dep, but stay graceful
    np = None  # type: ignore

_DDL = """
CREATE TABLE IF NOT EXISTS concepts (
    concept_id     TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL DEFAULT 'default',
    ctype          TEXT NOT NULL,            -- subject | predicate | object | qualifier
    canonical_label TEXT NOT NULL,
    embedding_json TEXT,
    status         TEXT NOT NULL DEFAULT 'candidate',   -- candidate | active | merged_into
    created_at     TEXT
);
CREATE TABLE IF NOT EXISTS aliases (
    ctype          TEXT NOT NULL,
    alias_text     TEXT NOT NULL,            -- normalized phrase
    concept_id     TEXT NOT NULL,
    embedding_json TEXT,                     -- this phrasing's unit vector (cluster-recall)
    PRIMARY KEY (ctype, alias_text)
);
CREATE INDEX IF NOT EXISTS ix_concepts_type ON concepts(ctype);
"""


def _norm(s) -> str:
    return (str(s) if s is not None else "").strip().lower()


class Registry:
    def __init__(self, conn: sqlite3.Connection, *,
                 embedder: Optional[Callable[[str], List[float]]] = None,
                 verifier: Optional[Callable[[str, str, str], bool]] = None,
                 threshold: float = 0.86,
                 max_candidates: int = 5) -> None:
        self.conn = conn
        conn.row_factory = sqlite3.Row    # named access for concept/alias rows (#8 review)
        self.embedder = embedder
        self.verifier = verifier          # verifier(phrase, candidate_label, ctype) -> bool
        self.threshold = float(threshold)  # candidate-RECALL gate (not a merge decision)
        self.max_candidates = int(max_candidates)
        conn.executescript(_DDL)
        conn.commit()
        self._vecs: Dict[str, Tuple[List[str], "np.ndarray"]] = {}
        self._load_vectors()

    def _load_vectors(self) -> None:
        """Hold ONE row per ALIAS (every accepted phrasing), mapped to its concept — so recall
        scores a concept by max-cosine over its whole cluster, not its lone minting vector."""
        if np is None:
            return
        for ctype in ("subject", "predicate", "object", "qualifier"):
            cids, vs = [], []
            for cid, ej in self.conn.execute(
                "SELECT concept_id, embedding_json FROM aliases WHERE ctype=? AND embedding_json IS NOT NULL",
                (ctype,),
            ):
                v = np.asarray(json.loads(ej), dtype=float)
                n = np.linalg.norm(v)
                if n > 0:
                    cids.append(cid)
                    vs.append(v / n)
            self._vecs[ctype] = (cids, np.vstack(vs) if vs else None)

    def resolve(self, phrase, ctype: str) -> Optional[str]:
        """Resolve a phrase to a canonical concept_id (None for empty).

        (1) exact alias = a stored, already-verified decision; (2) else embedding NN proposes
        candidates and the verifier decides; (3) else mint. Similarity never merges by itself.
        """
        norm = _norm(phrase)
        if not norm:
            return None
        row = self.conn.execute(
            "SELECT concept_id FROM aliases WHERE ctype=? AND alias_text=?", (ctype, norm)
        ).fetchone()
        if row:
            return row[0]
        if self.embedder is not None and np is not None:
            v = np.asarray(self.embedder(phrase), dtype=float)
            n = np.linalg.norm(v)
            if n > 0:
                v = v / n
                cid = self._verify_and_alias(phrase, norm, ctype, v)   # candidate → verify
                if cid is not None:
                    return cid
                return self._mint(phrase, norm, ctype, v.tolist())
        return self._mint(phrase, norm, ctype, None)

    def _verify_and_alias(self, phrase, norm: str, ctype: str, v: "np.ndarray") -> Optional[str]:
        """Embedding NN proposes merge candidates; the verifier DECIDES. Recall is scored per
        CONCEPT as the max cosine over all its alias embeddings (cluster recall), so a near-dup
        of any accepted phrasing surfaces — not just one near the minting vector. Returns a
        concept_id only on a verified 'yes'. No verifier ⇒ None (caller mints) — the fail-safe."""
        cids, mat = self._vecs.get(ctype, ([], None))
        if mat is None or not len(cids):
            return None
        sims = mat @ v
        best: Dict[str, float] = {}
        for i, cid in enumerate(cids):           # collapse per-alias sims to a per-concept max
            s = float(sims[i])
            if s > best.get(cid, -1.0):
                best[cid] = s
        candidates = sorted(
            ((s, cid) for cid, s in best.items() if s >= self.threshold), reverse=True
        )[: self.max_candidates]
        if not candidates or self.verifier is None:
            return None
        for _s, cid in candidates:
            if self.verifier(phrase, self._label(cid), ctype):
                self._add_alias(norm, ctype, cid, v)   # persist the verified 'yes' + its vector
                return cid
        return None

    def _label(self, cid: str) -> str:
        row = self.conn.execute(
            "SELECT canonical_label FROM concepts WHERE concept_id=?", (cid,)
        ).fetchone()
        return row[0] if row else ""

    def _mint(self, label, norm: str, ctype: str, vec: Optional[List[float]]) -> str:
        cid = "c_" + uuid.uuid4().hex[:16]
        self.conn.execute(
            "INSERT INTO concepts (concept_id,ctype,canonical_label,embedding_json,status,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (cid, ctype, str(label), json.dumps(vec) if vec is not None else None,
             "candidate", datetime.now(timezone.utc).isoformat()),
        )
        self._add_alias(norm, ctype, cid, vec)   # the minting phrasing is the cluster's first alias
        return cid

    def _add_alias(self, norm: str, ctype: str, cid: str, embedding=None) -> None:
        """Record a phrasing as an alias of a concept, persisting its unit vector and adding it
        to the in-memory cluster so it counts toward future recall."""
        vec_json = None
        if embedding is not None and np is not None:
            v = np.asarray(embedding, dtype=float)
            n = np.linalg.norm(v)
            if n > 0:
                v = v / n
                vec_json = json.dumps(v.tolist())
                cids, mat = self._vecs.get(ctype, ([], None))
                cids = cids + [cid]
                mat = v.reshape(1, -1) if mat is None else np.vstack([mat, v])
                self._vecs[ctype] = (cids, mat)
        self.conn.execute(
            "INSERT OR IGNORE INTO aliases (ctype, alias_text, concept_id, embedding_json) VALUES (?,?,?,?)",
            (ctype, norm, cid, vec_json),
        )

    def concept_count(self, ctype: Optional[str] = None) -> int:
        if ctype:
            return self.conn.execute("SELECT COUNT(*) FROM concepts WHERE ctype=?", (ctype,)).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM concepts").fetchone()[0]

    # ── emergent-vocabulary support (#8) ─────────────────────────────────────
    def concepts_by_status(self, status: str, ctype: Optional[str] = None) -> List[sqlite3.Row]:
        if ctype:
            return self.conn.execute(
                "SELECT * FROM concepts WHERE status=? AND ctype=? ORDER BY created_at, concept_id",
                (status, ctype)).fetchall()
        return self.conn.execute(
            "SELECT * FROM concepts WHERE status=? ORDER BY created_at, concept_id", (status,)).fetchall()

    def alias_count(self, concept_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM aliases WHERE concept_id=?", (concept_id,)).fetchone()[0]

    def promote(self, concept_id: str, *, commit: bool = True) -> None:
        """A candidate concept that has proven itself becomes part of the canonical vocabulary."""
        self.conn.execute("UPDATE concepts SET status='active' WHERE concept_id=?", (concept_id,))
        if commit:
            self.conn.commit()

    def merge_concepts(self, loser: str, survivor: str, *, commit: bool = True) -> None:
        """Fold a duplicate concept into the survivor: repoint its aliases (so future phrases
        resolve to the survivor) and mark it merged_into. Reload vectors so the loser drops out
        of candidate generation."""
        if not loser or not survivor or loser == survivor:
            return
        self.conn.execute("UPDATE aliases SET concept_id=? WHERE concept_id=?", (survivor, loser))
        self.conn.execute("UPDATE concepts SET status='merged_into' WHERE concept_id=?", (loser,))
        if commit:
            self.conn.commit()
            self._load_vectors()
