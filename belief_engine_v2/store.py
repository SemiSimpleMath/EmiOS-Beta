"""Event-sourcing core (#1).

Two tables:
- `observations` — append-only log, the SOURCE OF TRUTH. Each row records its resolved
  identity (`resolves_to`) + how it was resolved (`resolution_method`, `resolver_version`)
  and the extractor version, so a rebuild REPLAYS persisted decisions rather than
  re-adjudicating (per scratch/BELIEF-ENGINE-NEW.md §1).
- `beliefs` — a derived, rebuildable PROJECTION (a cache). `rebuild_projection()` folds the
  log deterministically; dropping + rebuilding it is the correctness test.

Reversibility: a `retract` observation (polarity='retract', retract_of=<observation_id>)
withdraws an earlier observation; the fold simply skips withdrawn observations, so a belief
whose only support is retracted disappears on rebuild.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from belief_engine_v2.decay import interval_from_cue, interval_from_spacing, survival
from belief_engine_v2.identity import identity_of
from belief_engine_v2.status import derive_status

# Surviving support at/below this (after decay) → the belief has faded to dormant (#5).
MIN_ACTIVE_SUPPORT = 0.05


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_now(now) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now
    return datetime.fromisoformat(str(now))


def _age_days(now_dt: datetime, occurred_iso: Optional[str]) -> float:
    """Age of an observation in days. Compared tz-naively so a mix of aware/naive timestamps
    (e.g. UTC-stamped appends vs naive test fixtures) never raises — decay is day-scale."""
    if not occurred_iso:
        return 0.0
    try:
        occ = datetime.fromisoformat(str(occurred_iso))
    except Exception:
        return 0.0
    a = now_dt.replace(tzinfo=None)
    b = occ.replace(tzinfo=None)
    return max(0.0, (a - b).total_seconds() / 86400.0)


@dataclass
class Claim:
    """The structured candidate meaning the extractor produces. In #1 subject/predicate/
    object are free strings (the stub identity hashes them); #2 makes them canonical IDs.
    raw_text + statement_nl + interpretation preserve the language (the hybrid record)."""
    subject: str = ""
    predicate: str = ""
    object: str = ""
    qualifiers: Dict[str, Any] = field(default_factory=dict)
    applies_when: Optional[dict] = None
    statement_nl: str = ""
    raw_text: str = ""
    interpretation: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


_DDL = """
CREATE TABLE IF NOT EXISTS observations (
    observation_id    TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL DEFAULT 'default',
    occurred_at       TEXT,                      -- valid-time (when it happened)
    recorded_at       TEXT NOT NULL,             -- transaction-time (when appended); fold order
    source            TEXT,
    polarity          TEXT NOT NULL,             -- affirm | contradict | retract
    strength          REAL NOT NULL DEFAULT 1.0,
    claim_json        TEXT,                      -- the extracted claim (null for retracts)
    raw_text          TEXT,
    extractor_version TEXT,
    resolves_to       TEXT,                      -- belief identity (persisted resolver decision)
    resolution_method TEXT,                      -- canonical | stub | override | retract
    resolver_version  TEXT,
    resolution_detail TEXT,                      -- JSON: chosen canonical concept ids
    retract_of        TEXT                       -- observation_id this withdraws
);
CREATE INDEX IF NOT EXISTS ix_obs_recorded ON observations(recorded_at);
CREATE INDEX IF NOT EXISTS ix_obs_resolves ON observations(resolves_to);
CREATE INDEX IF NOT EXISTS ix_obs_retract  ON observations(retract_of);

CREATE TABLE IF NOT EXISTS beliefs (
    belief_id      TEXT PRIMARY KEY,
    subject        TEXT,
    predicate      TEXT,
    object         TEXT,
    statement_nl   TEXT,
    applies_when   TEXT,                         -- JSON RRULE-like temporal cue (retrieval gate; #3)
    kind           TEXT,                         -- semantic_fact|preference|procedural_routine|episodic
    support        REAL NOT NULL DEFAULT 0,
    contradiction  REAL NOT NULL DEFAULT 0,
    net            REAL NOT NULL DEFAULT 0,
    status         TEXT NOT NULL,                -- active | contested | dormant  (derived from justifications, #4)
    confidence     REAL NOT NULL DEFAULT 0,      -- |net| / total evidence (#4)
    valid_from     TEXT,                         -- world-time the claim began holding (#7)
    valid_to       TEXT,                         -- world-time it lapsed (NULL while active/contested) (#7)
    first_observed TEXT,
    last_observed  TEXT,
    obs_count      INTEGER NOT NULL DEFAULT 0
);

-- The justification set (#4): one row per surviving observation that supports/contradicts a
-- belief. status + confidence are RECOMPUTED from this set, never mutated. Rebuilt each fold.
CREATE TABLE IF NOT EXISTS justifications (
    belief_id      TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    sign           INTEGER NOT NULL,             -- +1 supports | -1 contradicts
    raw_weight     REAL NOT NULL,                -- the observation's original strength
    weight         REAL NOT NULL,                -- effective weight = raw_weight × survival(...)  (#5)
    occurred_at    TEXT,
    source         TEXT,
    PRIMARY KEY (belief_id, observation_id)
);
CREATE INDEX IF NOT EXISTS ix_just_belief ON justifications(belief_id);

-- Consolidation (#6): a REVERSIBLE redirect from a duplicate belief to its survivor. The fold
-- attributes a redirected belief's observations to the survivor, so their justifications union;
-- deleting the row un-merges them. Populated by consolidate() (embedding NN proposes, LLM verifies).
CREATE TABLE IF NOT EXISTS belief_merges (
    belief_id   TEXT PRIMARY KEY,             -- the duplicate (redirected away)
    merged_into TEXT NOT NULL,                -- the survivor it folds into
    method      TEXT,                         -- llm_verified | manual
    detail      TEXT,                         -- JSON (verifier reason, similarity)
    created_at  TEXT
);
"""

_RESOLVER_VERSION = "v0-stub"


class Store:
    def __init__(self, db_path: str) -> None:
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_DDL)
        self.conn.commit()
        self.resolver = None    # set to a Resolver (#2) to use canonical identity

    # ── write side (log only) ────────────────────────────────────────────────
    def append(
        self,
        claim: Optional[Claim],
        *,
        source: str,
        polarity: str = "affirm",
        occurred_at: Optional[str] = None,
        recorded_at: Optional[str] = None,
        strength: float = 1.0,
        user_id: str = "default",
        extractor_version: str = "v0",
        retract_of: Optional[str] = None,
        identity_override: Optional[str] = None,
        commit: bool = True,
    ) -> str:
        """Append one immutable observation. Returns its observation_id.

        Resolution is persisted at append time: `identity_override` (a known resolution,
        e.g. seeding from the old store) wins; else the stub `identity_of(claim)`; a
        `retract` resolves to nothing (it acts on `retract_of`)."""
        oid = uuid.uuid4().hex
        rec = recorded_at or _now()
        occ = occurred_at or rec
        detail = None
        if polarity == "retract":
            resolves_to, method = None, "retract"
        elif identity_override is not None:
            resolves_to, method = identity_override, "override"
        elif self.resolver is not None and claim is not None:
            resolves_to, detail = self.resolver.resolve(claim)   # canonical concept resolution (#2)
            method = "canonical"
        else:
            resolves_to, method = (identity_of(claim) if claim else None), "stub"
        self.conn.execute(
            "INSERT INTO observations (observation_id,user_id,occurred_at,recorded_at,source,"
            "polarity,strength,claim_json,raw_text,extractor_version,resolves_to,resolution_method,"
            "resolver_version,resolution_detail,retract_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, user_id, occ, rec, source, polarity, float(strength),
             json.dumps(asdict(claim)) if claim else None,
             (claim.raw_text if claim else None), extractor_version,
             resolves_to, method, _RESOLVER_VERSION,
             json.dumps(detail) if detail else None, retract_of),
        )
        if commit:
            self.conn.commit()
        return oid

    def all_observations(self) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM observations ORDER BY recorded_at, observation_id"
        ).fetchall()

    # ── read side (rebuildable projection) ───────────────────────────────────
    def _project(self, now_dt: datetime, as_of: Optional[str] = None):
        """Pure fold of the log → (belief_dicts, justification_rows). Honors consolidation
        redirects (#6). For transaction-time travel (#7), an `as_of` cutoff keeps only observations
        RECORDED at or before it — so you see what the engine believed as it knew things then; a
        contradiction recorded later isn't in scope yet. Cadence decay (#5) ages weights as of
        `now_dt` (the world clock), giving the bitemporal pair: as_of = system time, now = valid
        time. No writes — both rebuild and beliefs_as_of() consume this."""
        cutoff = str(as_of) if as_of is not None else None

        def _in_tx(rec) -> bool:
            return cutoff is None or (rec is not None and str(rec) <= cutoff)

        retracted = {
            r["retract_of"] for r in self.conn.execute(
                "SELECT retract_of, recorded_at FROM observations "
                "WHERE polarity='retract' AND retract_of IS NOT NULL"
            ) if _in_tx(r["recorded_at"])
        }
        merges = {r["belief_id"]: r["merged_into"] for r in   # consolidation redirects (#6)
                  self.conn.execute("SELECT belief_id, merged_into FROM belief_merges")}

        def _final(bid: str) -> str:                          # follow the redirect chain (cycle-safe)
            seen = set()
            while bid in merges and bid not in seen:
                seen.add(bid)
                bid = merges[bid]
            return bid

        acc: Dict[str, Dict[str, Any]] = {}
        for r in self.conn.execute("SELECT * FROM observations ORDER BY recorded_at, observation_id"):
            if not _in_tx(r["recorded_at"]):
                continue
            if r["polarity"] == "retract" or r["observation_id"] in retracted:
                continue
            bid = _final(r["resolves_to"]) if r["resolves_to"] else None
            if not bid:
                continue
            claim = json.loads(r["claim_json"]) if r["claim_json"] else {}
            b = acc.setdefault(bid, {
                "belief_id": bid, "subject": claim.get("subject", ""),
                "predicate": claim.get("predicate", ""), "object": claim.get("object", ""),
                "statement_nl": "", "applies_when": None, "kind": None,
                "support": 0.0, "contradiction": 0.0, "obs_count": 0, "justifications": [],
                "first_observed": r["occurred_at"], "last_observed": r["occurred_at"],
            })
            s = r["strength"] if r["strength"] is not None else 1.0
            sign = 0
            if r["polarity"] == "affirm":
                sign = 1
                b["support"] += s
                if claim.get("statement_nl"):
                    b["statement_nl"] = claim["statement_nl"]   # latest affirm wins (fold order)
                if claim.get("applies_when") is not None:
                    b["applies_when"] = claim["applies_when"]
                k = (claim.get("extra") or {}).get("kind")
                if k:
                    b["kind"] = k
            elif r["polarity"] == "contradict":
                sign = -1
                b["contradiction"] += s
            if sign:
                b["justifications"].append({                    # the JTMS justification set (#4)
                    "observation_id": r["observation_id"], "sign": sign, "weight": s,
                    "occurred_at": r["occurred_at"], "source": r["source"],
                })
            b["obs_count"] += 1
            if r["occurred_at"] and (b["first_observed"] is None or r["occurred_at"] < b["first_observed"]):
                b["first_observed"] = r["occurred_at"]
            if r["occurred_at"] and (b["last_observed"] is None or r["occurred_at"] > b["last_observed"]):
                b["last_observed"] = r["occurred_at"]

        belief_dicts: List[Dict[str, Any]] = []
        just_rows = []
        for bid, b in acc.items():
            if b["support"] <= 0:          # no surviving affirmation → not a belief
                continue
            # --- cadence-aware decay (#5): age each justification's weight as of `now` ---
            kind = b["kind"]
            ages = {j["observation_id"]: _age_days(now_dt, j["occurred_at"]) for j in b["justifications"]}
            interval = None
            if (kind or "").strip().lower() == "procedural_routine":
                affirm_ages = [ages[j["observation_id"]] for j in b["justifications"] if j["sign"] > 0]
                interval = interval_from_cue(b["applies_when"]) or interval_from_spacing(affirm_ages)
            decayed = []
            for j in b["justifications"]:
                w = j["weight"] * survival(kind, ages[j["observation_id"]], interval_days=interval)
                decayed.append({**j, "raw_weight": j["weight"], "weight": w})

            st = derive_status(decayed, min_support=MIN_ACTIVE_SUPPORT)   # status from DECAYED set (#4+#5)
            valid_to = None if st.status in ("active", "contested") else b["last_observed"]
            for j in decayed:
                just_rows.append((bid, j["observation_id"], j["sign"], j["raw_weight"], j["weight"],
                                  j["occurred_at"], j["source"]))
            belief_dicts.append({
                "belief_id": bid, "subject": b["subject"], "predicate": b["predicate"],
                "object": b["object"], "statement_nl": b["statement_nl"],
                "applies_when": json.dumps(b["applies_when"]) if b["applies_when"] is not None else None,
                "kind": b["kind"], "support": st.pos, "contradiction": st.neg, "net": st.net,
                "status": st.status, "confidence": st.confidence,
                "valid_from": b["first_observed"], "valid_to": valid_to,
                "first_observed": b["first_observed"], "last_observed": b["last_observed"],
                "obs_count": b["obs_count"],
            })
        return belief_dicts, just_rows

    _BELIEF_COLS = ("belief_id", "subject", "predicate", "object", "statement_nl", "applies_when",
                    "kind", "support", "contradiction", "net", "status", "confidence",
                    "valid_from", "valid_to", "first_observed", "last_observed", "obs_count")

    def rebuild_projection(self, now=None, as_of=None) -> int:
        """Fold the log into `beliefs` as of `now` (and optionally a transaction-time `as_of`).
        Drop + rebuild = the cache is a pure function of (log, now, as_of); the log is immutable.
        Returns the belief count."""
        now_dt = _resolve_now(now)
        belief_dicts, just_rows = self._project(now_dt, as_of)
        rows = [tuple(b[c] for c in self._BELIEF_COLS) for b in belief_dicts]
        self.conn.execute("DELETE FROM beliefs")
        self.conn.execute("DELETE FROM justifications")
        self.conn.executemany(
            f"INSERT INTO beliefs ({','.join(self._BELIEF_COLS)}) "
            f"VALUES ({','.join('?' for _ in self._BELIEF_COLS)})", rows,
        )
        self.conn.executemany(
            "INSERT INTO justifications (belief_id,observation_id,sign,raw_weight,weight,occurred_at,source) "
            "VALUES (?,?,?,?,?,?,?)", just_rows,
        )
        self.conn.commit()
        return len(rows)

    def beliefs_as_of(self, as_of, now=None) -> List[Dict[str, Any]]:
        """Transaction-time travel (#7): what the engine believed as of system time `as_of`,
        WITHOUT touching the live projection. Replays the immutable log up to `as_of` — event
        sourcing makes this free, so no SCD-2 version table is needed."""
        now_dt = _resolve_now(now)
        belief_dicts, _ = self._project(now_dt, as_of)
        return belief_dicts

    def justifications_for(self, belief_id: str) -> List[sqlite3.Row]:
        """The signed justification set behind a belief — the 'why' of its status (#4)."""
        return self.conn.execute(
            "SELECT * FROM justifications WHERE belief_id=? ORDER BY occurred_at, observation_id",
            (belief_id,),
        ).fetchall()

    def beliefs(self) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM beliefs ORDER BY belief_id").fetchall()

    # ── consolidation (#6) — reversible belief merges ────────────────────────
    def merge_beliefs(self, loser: str, survivor: str, *, method: str = "llm_verified",
                      detail: Optional[dict] = None, commit: bool = True) -> None:
        """Redirect `loser` into `survivor`. The next rebuild folds loser's observations into the
        survivor (justifications union). Reversible: drop the row to split them again."""
        if not loser or not survivor or loser == survivor:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO belief_merges (belief_id, merged_into, method, detail, created_at) "
            "VALUES (?,?,?,?,?)",
            (loser, survivor, method, json.dumps(detail) if detail else None, _now()),
        )
        if commit:
            self.conn.commit()

    def unmerge_belief(self, loser: str, *, commit: bool = True) -> None:
        """Withdraw a consolidation redirect; the belief splits back out on the next rebuild."""
        self.conn.execute("DELETE FROM belief_merges WHERE belief_id=?", (loser,))
        if commit:
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()
