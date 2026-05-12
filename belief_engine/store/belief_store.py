"""
BeliefStore — the single access point for reading and writing beliefs.

All DB operations use the project-standard SQLAlchemy get_session() pattern:
  - Each method opens its own short-lived session.
  - Sessions are never held open during ChromaDB or LLM calls.
  - rollback() on exception, close() in finally.
  - ORM objects never escape their session — callers receive plain dataclasses.

Usage:
    from belief_engine.store.belief_store import BeliefStore

    store = BeliefStore()
    belief = store.get_by_key("routine.dog_walk.morning")
    store.upsert_belief(belief_data, evidence_items)
    matches = store.find_similar("morning dog walk routine", k=5)
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.models.base import get_session
from belief_engine.chroma.belief_chroma import get_belief_chroma
from belief_engine.db.models import BeliefEvidence, UserBelief

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class BeliefRecord:
    id: str
    domain: str
    belief_key: str
    statement: str
    confidence: str          # high | medium | low (stored at extraction)
    scope: str               # chronic | temporary
    status: str              # active | contested | deprecated
    conditions: Optional[Dict[str, Any]]
    observation_count: int
    first_observed: Optional[str]
    last_confirmed: Optional[str]
    created_at: str
    updated_at: str
    # Decay v2 (2026-05-11): kind drives half-life; snapshot cols populated by
    # RecomputeBeliefSnapshotStep. Optional so legacy callers don't break.
    kind: Optional[str] = None
    last_contradicted_at: Optional[str] = None
    current_support_weight: Optional[float] = None
    current_contradiction_weight: Optional[float] = None
    current_net_weight: Optional[float] = None
    current_confidence_band: Optional[str] = None


@dataclass
class EvidenceRecord:
    id: str
    belief_id: str
    source_type: str         # daily_insights | kg_edge | ticket_rejection | manual
    source_date: Optional[str]
    source_ref: Optional[str]
    signal_type: str         # confirms | qualifies | contradicts | rejects
    summary: str
    raw_text: Optional[str]
    weight: float
    created_at: str


@dataclass
class BeliefUpsertRequest:
    """Input to store.upsert_belief()."""
    domain: str
    belief_key: str
    statement: str
    confidence: str
    scope: str
    status: str = "active"
    conditions: Optional[Dict[str, Any]] = None
    first_observed: Optional[str] = None
    last_confirmed: Optional[str] = None
    # `kind` drives belief decay half-life. None preserves any existing kind
    # on update; if NULL after upsert, recompute step falls back to heuristic.
    kind: Optional[str] = None


@dataclass
class EvidenceInput:
    """One piece of evidence to attach to a belief."""
    source_type: str
    source_date: Optional[str]
    signal_type: str
    summary: str
    source_ref: Optional[str] = None
    raw_text: Optional[str] = None
    weight: float = 1.0
    # Optional explicit valence override. If None, derived from signal_type
    # at insert time. Use 'support' / 'contradict' / 'qualify'.
    valence: Optional[str] = None
    # Provenance — which agent / pipeline step wrote this evidence row.
    extracted_by: Optional[str] = None


class BeliefStore:

    def __init__(self) -> None:
        self._chroma = get_belief_chroma()

    # ------------------------------------------------------------------
    # ORM → dataclass helpers (no session needed)
    # ------------------------------------------------------------------

    @staticmethod
    def _orm_to_belief(row: UserBelief) -> BeliefRecord:
        conditions_raw = row.conditions
        conditions = json.loads(conditions_raw) if conditions_raw else None
        return BeliefRecord(
            id=row.id,
            domain=row.domain,
            belief_key=row.belief_key,
            statement=row.statement,
            confidence=row.confidence,
            scope=row.scope,
            status=row.status,
            conditions=conditions,
            observation_count=row.observation_count,
            first_observed=row.first_observed,
            last_confirmed=row.last_confirmed,
            created_at=row.created_at,
            updated_at=row.updated_at,
            kind=getattr(row, "kind", None),
            last_contradicted_at=getattr(row, "last_contradicted_at", None),
            current_support_weight=getattr(row, "current_support_weight", None),
            current_contradiction_weight=getattr(row, "current_contradiction_weight", None),
            current_net_weight=getattr(row, "current_net_weight", None),
            current_confidence_band=getattr(row, "current_confidence_band", None),
        )

    @staticmethod
    def _orm_to_evidence(row: BeliefEvidence) -> EvidenceRecord:
        return EvidenceRecord(
            id=row.id,
            belief_id=row.belief_id,
            source_type=row.source_type,
            source_date=row.source_date,
            source_ref=row.source_ref,
            signal_type=row.signal_type,
            summary=row.summary,
            raw_text=row.raw_text,
            weight=row.weight,
            created_at=row.created_at,
        )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_by_id(self, belief_id: str) -> Optional[BeliefRecord]:
        session = get_session()
        try:
            row = session.query(UserBelief).filter(UserBelief.id == belief_id).first()
            return self._orm_to_belief(row) if row else None
        finally:
            session.close()

    def get_by_key(self, belief_key: str) -> Optional[BeliefRecord]:
        session = get_session()
        try:
            row = session.query(UserBelief).filter(UserBelief.belief_key == belief_key).first()
            return self._orm_to_belief(row) if row else None
        finally:
            session.close()

    def list_by_domain(self, domain: str, *, status: str = "active") -> List[BeliefRecord]:
        session = get_session()
        try:
            rows = (
                session.query(UserBelief)
                .filter(UserBelief.domain == domain, UserBelief.status == status)
                .order_by(UserBelief.belief_key)
                .all()
            )
            return [self._orm_to_belief(r) for r in rows]
        finally:
            session.close()

    def list_all(self, *, status: str = "active") -> List[BeliefRecord]:
        session = get_session()
        try:
            rows = (
                session.query(UserBelief)
                .filter(UserBelief.status == status)
                .order_by(UserBelief.domain, UserBelief.belief_key)
                .all()
            )
            return [self._orm_to_belief(r) for r in rows]
        finally:
            session.close()

    def get_evidence(self, belief_id: str) -> List[EvidenceRecord]:
        session = get_session()
        try:
            rows = (
                session.query(BeliefEvidence)
                .filter(BeliefEvidence.belief_id == belief_id)
                .order_by(BeliefEvidence.created_at.desc())
                .all()
            )
            return [self._orm_to_evidence(r) for r in rows]
        finally:
            session.close()

    def find_similar(
        self,
        query: str,
        *,
        k: int = 5,
        domain: Optional[str] = None,
        threshold: float = 0.55,
    ) -> List[Tuple[BeliefRecord, float]]:
        """
        Semantic search — returns (BeliefRecord, similarity) pairs above threshold.
        ChromaDB is queried first (no DB session), then each hit is fetched by ID
        in its own short session.
        """
        if self._chroma.count() == 0:
            return []
        hits = self._chroma.search(query, k=k, domain=domain)
        results: List[Tuple[BeliefRecord, float]] = []
        for belief_id, score, _ in hits:
            if score < threshold:
                continue
            belief = self.get_by_id(belief_id)
            if belief and belief.status == "active":
                results.append((belief, score))
        return results

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def upsert_belief(
        self,
        req: BeliefUpsertRequest,
        evidence: Optional[List[EvidenceInput]] = None,
    ) -> BeliefRecord:
        """
        Create or update a belief by belief_key in one atomic statement.

        On insert: a new row with observation_count=1.
        On conflict (belief_key already exists): updates statement, confidence,
        scope, status, conditions, last_confirmed, updated_at, and increments
        observation_count by 1 at the DB level — no read-modify-write race.

        Attaches any provided evidence records (each in its own short session).
        """
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        now = _now_iso()
        new_id = str(uuid.uuid4())  # used on insert; ignored on conflict

        # Resolve kind for insert: use supplied or heuristic-classify so new
        # rows always have a kind (drives decay half-life).
        kind_value = req.kind
        if kind_value is None:
            from belief_engine.decay import classify_kind_heuristic
            kind_value = classify_kind_heuristic(
                belief_key=req.belief_key, domain=req.domain, scope=req.scope,
            )

        stmt = sqlite_insert(UserBelief).values(
            id=new_id,
            domain=req.domain,
            belief_key=req.belief_key,
            statement=req.statement,
            confidence=req.confidence,
            scope=req.scope,
            status=req.status,
            kind=kind_value,
            conditions=json.dumps(req.conditions) if req.conditions else None,
            observation_count=1,
            first_observed=req.first_observed or now,
            last_confirmed=req.last_confirmed or now,
            created_at=now,
            updated_at=now,
        )
        # On conflict: preserve existing kind unless caller explicitly supplies one.
        # The COALESCE-style preserve is what differentiates "the LLM reclassified
        # this belief's kind" from "the LLM didn't say anything about kind."
        update_set = {
            "statement":         stmt.excluded.statement,
            "confidence":        stmt.excluded.confidence,
            "scope":             stmt.excluded.scope,
            "status":            stmt.excluded.status,
            "conditions":        stmt.excluded.conditions,
            "observation_count": UserBelief.observation_count + 1,
            "last_confirmed":    stmt.excluded.last_confirmed,
            "updated_at":        stmt.excluded.updated_at,
        }
        if req.kind is not None:
            update_set["kind"] = stmt.excluded.kind
        stmt = stmt.on_conflict_do_update(
            index_elements=["belief_key"],
            set_=update_set,
        )

        session = get_session()
        try:
            session.execute(stmt)
            session.commit()
        except Exception:
            session.rollback()
            logger.debug("[BeliefStore] upsert failed key=%s", req.belief_key, exc_info=True)
            raise
        finally:
            session.close()

        # Read back the canonical row in a separate short session.
        belief = self.get_by_key(req.belief_key)
        if belief is None:
            raise RuntimeError(f"[BeliefStore] upsert failed for key={req.belief_key!r}")

        # Sync embedding (ChromaDB, no DB session).
        self._chroma.upsert(
            belief_id=belief.id,
            statement=belief.statement,
            domain=belief.domain,
        )

        # Attach evidence — each in its own short session.
        for ev in (evidence or []):
            self._insert_evidence(belief.id, ev, now)

        logger.info(
            "[BeliefStore] upsert key=%s status=%s obs=%d",
            belief.belief_key,
            belief.status,
            belief.observation_count,
        )
        return belief

    def deprecate(self, belief_key: str, *, reason: str = "") -> None:
        """
        Flip a belief to status='deprecated' and record the reason as a
        belief_evidence row so the audit trail survives the deprecation.

        Idempotent: re-calling on an already-deprecated belief is a no-op
        (no second evidence row).
        """
        now = _now_iso()

        # Look up id first so we can attach evidence after the status flip.
        belief = self.get_by_key(belief_key)
        if belief is None:
            logger.warning("[BeliefStore] deprecate: key=%s not found", belief_key)
            return

        # Flip status — guarded by status != 'deprecated' so re-deprecation
        # affects 0 rows and we skip the evidence write.
        session = get_session()
        try:
            rowcount = session.query(UserBelief).filter(
                UserBelief.belief_key == belief_key,
                UserBelief.status != "deprecated",
            ).update(
                {"status": "deprecated", "updated_at": now},
                synchronize_session=False,
            )
            session.commit()
        except Exception:
            session.rollback()
            logger.debug("[BeliefStore] deprecate failed key=%s", belief_key, exc_info=True)
            raise
        finally:
            session.close()

        if rowcount == 0:
            logger.info("[BeliefStore] deprecate key=%s already deprecated; no-op", belief_key)
            return

        # Record the deprecation as a separate short-session evidence write.
        self._insert_evidence(
            belief.id,
            EvidenceInput(
                source_type="deprecation",
                source_date=datetime.now(timezone.utc).date().isoformat(),
                signal_type="rejects",
                summary=(f"Deprecated: {reason}" if reason else "Deprecated (no reason given)")[:800],
                weight=1.0,
            ),
            now,
        )
        logger.info("[BeliefStore] deprecated key=%s reason=%s", belief_key, reason)

    def merge_belief(
        self,
        *,
        surviving_key: str,
        surviving_statement: str,
        surviving_confidence: str,
        surviving_scope: str,
        deprecated_keys: List[str],
        domain: str,
        merge_reasoning: str,
    ) -> BeliefRecord:
        """
        Consolidate a cluster of near-duplicate beliefs into one canonical belief.

        - Rewrites the surviving belief with the canonical statement.
        - Attaches a canonicalization evidence record to the surviving belief so its
          history shows the merge event and the combined observation weight.
        - Deprecates all redundant beliefs and removes their Chroma embeddings.
        - observation_count on the surviving belief is incremented by the sum of all
          deprecated beliefs' observation counts (their support is transferred).
        """
        store = self

        # Sum up observation counts from deprecated beliefs before deprecating them.
        transferred_obs = 0
        deprecated_statements: List[str] = []
        for dk in deprecated_keys:
            dep = store.get_by_key(dk)
            if dep:
                transferred_obs += dep.observation_count
                deprecated_statements.append(f"{dk}: {dep.statement}")

        # Rewrite surviving belief.
        surviving = store.get_by_key(surviving_key)
        if surviving is None:
            # Surviving key may be one we're creating fresh from a cluster.
            req = BeliefUpsertRequest(
                domain=domain,
                belief_key=surviving_key,
                statement=surviving_statement,
                confidence=surviving_confidence,
                scope=surviving_scope,
                status="active",
                last_confirmed=datetime.now(timezone.utc).date().isoformat(),
            )
        else:
            req = BeliefUpsertRequest(
                domain=domain,
                belief_key=surviving_key,
                statement=surviving_statement,
                confidence=surviving_confidence,
                scope=surviving_scope,
                status="active",
                last_confirmed=datetime.now(timezone.utc).date().isoformat(),
            )

        surviving_record = store.upsert_belief(req)

        # Boost observation count by transferred support. The increment is a
        # SQL-level expression (UserBelief.observation_count + transferred_obs)
        # so SQLite reads-and-adds in one statement — no read-modify-write race
        # against another writer touching the same key.
        if transferred_obs > 0:
            now = _now_iso()
            session = get_session()
            try:
                session.query(UserBelief).filter(
                    UserBelief.belief_key == surviving_key
                ).update(
                    {"observation_count": UserBelief.observation_count + transferred_obs,
                     "updated_at": now},
                    synchronize_session=False,
                )
                session.commit()
            except Exception:
                session.rollback()
                logger.debug("[BeliefStore] merge obs boost failed key=%s", surviving_key, exc_info=True)
                raise
            finally:
                session.close()

        # Attach a canonicalization evidence record.
        merged_summary = (
            f"Canonicalization merge: absorbed {len(deprecated_keys)} redundant belief(s). "
            f"Reasoning: {merge_reasoning[:300]}"
        )
        if deprecated_statements:
            merged_summary += " Absorbed: " + " | ".join(deprecated_statements)[:400]

        surviving_record = store.get_by_key(surviving_key)
        if surviving_record:
            self._insert_evidence(
                surviving_record.id,
                EvidenceInput(
                    source_type="canonicalization",
                    source_date=datetime.now(timezone.utc).date().isoformat(),
                    signal_type="confirms",
                    summary=merged_summary[:800],
                    weight=2.0 * len(deprecated_keys),
                ),
                _now_iso(),
            )

        # Deprecate redundant beliefs and remove their Chroma embeddings.
        for dk in deprecated_keys:
            store.deprecate(dk, reason=f"merged into {surviving_key}: {merge_reasoning[:200]}")
            dep = store.get_by_key(dk)
            if dep:
                try:
                    self._chroma.delete(dep.id)
                except Exception as exc:
                    logger.debug("[BeliefStore] chroma delete failed for %s: %s", dk, exc)

        surviving_final = store.get_by_key(surviving_key)
        if surviving_final is None:
            raise RuntimeError(f"[BeliefStore] merge failed — surviving key {surviving_key!r} not found after write")

        logger.info(
            "[BeliefStore] merge complete surviving=%s deprecated=%s transferred_obs=%d",
            surviving_key, deprecated_keys, transferred_obs,
        )
        return surviving_final

    def mark_contested(self, belief_key: str) -> None:
        now = _now_iso()
        session = get_session()
        try:
            session.query(UserBelief).filter(UserBelief.belief_key == belief_key).update(
                {"status": "contested", "updated_at": now},
                synchronize_session=False,
            )
            session.commit()
        except Exception:
            session.rollback()
            logger.debug("[BeliefStore] mark_contested failed key=%s", belief_key, exc_info=True)
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Decay (time-based)
    # ------------------------------------------------------------------

    def decay_temporary_beliefs(
        self,
        domain: str,
        *,
        now_utc: datetime,
        threshold_days: int = 30,
    ) -> List[str]:
        """
        Auto-deprecate active+temporary beliefs in this domain whose last_confirmed
        is older than threshold_days. The agent already tagged these as ephemeral
        ("will decay" per the updater prompt) — this honors that tag.

        Each deprecation goes through the canonical deprecate() path so an audit
        evidence row is written. Returns the list of deprecated belief_keys.

        NULL last_confirmed values are skipped defensively (no current rows have
        them, but a defensive predicate avoids tablescan surprises later).
        """
        cutoff_iso = (now_utc - timedelta(days=threshold_days)).isoformat()
        stale_keys: List[str] = []

        session = get_session()
        try:
            rows = (
                session.query(UserBelief.belief_key)
                .filter(
                    UserBelief.domain == domain,
                    UserBelief.status == "active",
                    UserBelief.scope == "temporary",
                    UserBelief.last_confirmed.isnot(None),
                    UserBelief.last_confirmed < cutoff_iso,
                )
                .all()
            )
            stale_keys = [r[0] for r in rows]
        finally:
            session.close()

        for key in stale_keys:
            self.deprecate(
                key,
                reason=f"temporary belief unconfirmed for >{threshold_days}d (last_confirmed cutoff {cutoff_iso[:10]})",
            )

        if stale_keys:
            logger.info(
                "[BeliefStore] decay_temporary domain=%s deprecated=%d (cutoff=%s)",
                domain, len(stale_keys), cutoff_iso[:10],
            )
        return stale_keys

    def flag_stale_chronic_beliefs(
        self,
        domain: str,
        *,
        now_utc: datetime,
        threshold_days: int = 180,
    ) -> List[str]:
        """
        Flag active+chronic beliefs unconfirmed for >threshold_days as 'contested'
        so the existing ReevaluateBeliefsStep picks them up for an LLM review
        (refresh / qualify / split / deprecate / confirm).

        Each transition writes a belief_evidence row recording the staleness
        reason. Returns the list of newly-contested belief_keys.

        NULL last_confirmed values are skipped defensively.
        """
        cutoff_iso = (now_utc - timedelta(days=threshold_days)).isoformat()
        now = _now_iso()

        session = get_session()
        try:
            rows = (
                session.query(UserBelief.id, UserBelief.belief_key)
                .filter(
                    UserBelief.domain == domain,
                    UserBelief.status == "active",
                    UserBelief.scope == "chronic",
                    UserBelief.last_confirmed.isnot(None),
                    UserBelief.last_confirmed < cutoff_iso,
                )
                .all()
            )
            stale = [(rid, rkey) for rid, rkey in rows]
        finally:
            session.close()

        if not stale:
            return []

        # Flip status active -> contested in one short session.
        flipped: List[Tuple[str, str]] = []
        session = get_session()
        try:
            for belief_id, belief_key in stale:
                rowcount = session.query(UserBelief).filter(
                    UserBelief.id == belief_id,
                    UserBelief.status == "active",
                ).update(
                    {"status": "contested", "updated_at": now},
                    synchronize_session=False,
                )
                if rowcount:
                    flipped.append((belief_id, belief_key))
            session.commit()
        except Exception:
            session.rollback()
            logger.debug("[BeliefStore] flag_stale_chronic failed domain=%s", domain, exc_info=True)
            raise
        finally:
            session.close()

        # Audit row per flipped belief — separate short sessions via _insert_evidence.
        for belief_id, belief_key in flipped:
            self._insert_evidence(
                belief_id,
                EvidenceInput(
                    source_type="decay_review",
                    source_date=datetime.now(timezone.utc).date().isoformat(),
                    signal_type="qualifies",
                    summary=(
                        f"Chronic belief flagged for review: unconfirmed for "
                        f">{threshold_days}d (last_confirmed cutoff {cutoff_iso[:10]})."
                    )[:800],
                    weight=1.0,
                ),
                now,
            )

        flipped_keys = [k for _, k in flipped]
        if flipped_keys:
            logger.info(
                "[BeliefStore] flag_stale_chronic domain=%s contested=%d (cutoff=%s)",
                domain, len(flipped_keys), cutoff_iso[:10],
            )
        return flipped_keys

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _insert_evidence(self, belief_id: str, ev: EvidenceInput, now: str) -> None:
        """Insert a belief_evidence row with valence + half_life_snapshot derived.

        - valence: caller can supply explicitly; falls back to mapping from
          signal_type via belief_engine.decay.valence_from_signal_type.
        - half_life_days_snapshot: looked up from the owning belief's `kind`,
          so historical half-life changes don't retroactively reshape decay.
        """
        from sqlalchemy import text as _sa_text
        from belief_engine.decay import valence_from_signal_type, half_life_for_kind

        resolved_valence = ev.valence or valence_from_signal_type(ev.signal_type)

        # Pull owning belief's kind for the half-life snapshot.
        kind: Optional[str] = None
        session_q = get_session()
        try:
            row = session_q.execute(
                _sa_text("SELECT kind FROM user_beliefs WHERE id = :id"),
                {"id": belief_id},
            ).fetchone()
            kind = row[0] if row else None
        finally:
            session_q.close()
        half_life_snapshot = half_life_for_kind(kind)

        ev_id = str(uuid.uuid4())
        session = get_session()
        try:
            session.add(
                BeliefEvidence(
                    id=ev_id,
                    belief_id=belief_id,
                    source_type=ev.source_type,
                    source_date=ev.source_date,
                    source_ref=ev.source_ref,
                    signal_type=ev.signal_type,
                    summary=ev.summary,
                    raw_text=ev.raw_text,
                    weight=ev.weight,
                    valence=resolved_valence,
                    half_life_days_snapshot=half_life_snapshot,
                    extracted_by=ev.extracted_by,
                    created_at=now,
                )
            )
            session.commit()
        except Exception:
            session.rollback()
            logger.debug("[BeliefStore] insert_evidence failed belief_id=%s", belief_id, exc_info=True)
            raise
        finally:
            session.close()
