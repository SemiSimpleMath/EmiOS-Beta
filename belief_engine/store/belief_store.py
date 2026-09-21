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

from app.assistant.utils.logging_config import get_logger
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.models.base import get_session
from belief_engine.chroma.belief_chroma import get_belief_chroma
from belief_engine.db.models import BeliefEvidence, UserBelief

logger = get_logger(__name__)


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
    # Owner lock (1 = manual correction via /beliefs; engine must not modify/deprecate).
    locked: int = 0


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
            locked=getattr(row, "locked", 0) or 0,
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
        from belief_engine.matching.context import lineage
        from belief_engine.matching.history import connection
        from belief_engine.db.paths import belief_db_path
        from dataclasses import fields
        with connection(belief_db_path()) as conn:
            _, evidence = lineage(conn, belief_id)
        names = [f.name for f in fields(EvidenceRecord)]
        return [EvidenceRecord(**{k: e.get(k) for k in names}) for e in evidence]

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
        self, req: BeliefUpsertRequest, evidence: Optional[List[EvidenceInput]] = None,
    ) -> BeliefRecord:
        """Commit claim and evidence together; observation metadata comes only from sources.

        Rewriting or rereading a claim is not a new observation. Source dates are not
        replaced by processing time. Chroma is updated only after the SQLite commit.
        """
        from sqlalchemy import text
        from belief_engine.config import list_all_domain_ids
        from belief_engine.decay import classify_kind_heuristic
        if req.domain not in list_all_domain_ids():
            raise ValueError(f"Unknown belief domain: {req.domain!r}")
        now = _now_iso()
        session = get_session()
        try:
            session.execute(text('BEGIN IMMEDIATE'))
            row = session.query(UserBelief).filter_by(belief_key=req.belief_key).one_or_none()
            if row is None:
                row = UserBelief(id=str(uuid.uuid4()), domain=req.domain, belief_key=req.belief_key,
                    statement=req.statement, confidence=req.confidence, scope=req.scope, status=req.status,
                    conditions=json.dumps(req.conditions) if req.conditions is not None else None,
                    kind=req.kind or classify_kind_heuristic(belief_key=req.belief_key,domain=req.domain,scope=req.scope),
                    observation_count=0, first_observed=None, last_confirmed=None,
                    created_at=now, updated_at=now)
                session.add(row)
                session.flush()
            elif not row.locked:
                row.statement, row.confidence, row.scope, row.status = req.statement, req.confidence, req.scope, req.status
                if req.conditions is not None:
                    row.conditions = json.dumps(req.conditions)
                if req.kind is not None:
                    row.kind = req.kind
                row.updated_at = now
                session.flush()
            belief_id = row.id
            for ev in evidence or []:
                self._append_evidence(session, belief_id, ev, now)
            self._refresh_observation_metadata(session, belief_id)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        belief = self.get_by_key(req.belief_key)
        if belief is None:
            raise RuntimeError(f'Committed belief missing: {req.belief_key}')
        self._chroma.upsert(belief_id=belief.id, statement=belief.statement, domain=belief.domain)
        try:
            from belief_engine.identity import ensure_short_id
            ensure_short_id(belief.id)
        except Exception:
            logger.warning('Short-id assignment failed for %s',belief.belief_key,exc_info=True)
        return belief

    def add_evidence_to_existing(
        self, belief_key: str, evidence: List[EvidenceInput]
    ) -> Optional[BeliefRecord]:
        """Attach evidence to an EXISTING, non-deprecated belief — never create one.

        For contradicting signals: a 'contradicts' must only weaken a belief that
        already exists, never mint a new affirmative belief. Minting one turns
        "kids don't like zucchini" into a phantom "kids will eat zucchini" carrying
        only negative evidence (see scratch/MEAL-PLANNING-AUDIT.md). Returns the
        belief if found and active (evidence attached); None if absent or deprecated
        so the caller can skip rather than fabricate.
        """
        from sqlalchemy import text
        session = get_session()
        try:
            session.execute(text('BEGIN IMMEDIATE'))
            row = session.query(UserBelief).filter_by(belief_key=belief_key).one_or_none()
            if row is None or row.status == 'deprecated':
                session.rollback()
                return None
            for ev in evidence or []:
                self._append_evidence(session,row.id,ev,_now_iso())
            self._refresh_observation_metadata(session,row.id)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        return self.get_by_key(belief_key)

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
                summary=(f"Deprecated: {reason}" if reason else "Deprecated (no reason given)"),
                weight=1.0,
            ),
            now,
        )
        logger.info("[BeliefStore] deprecated key=%s reason=%s", belief_key, reason)

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

    @staticmethod
    def _append_evidence(session, belief_id: str, ev: EvidenceInput, now: str) -> bool:
        """Insert an exact source record once within the caller's write transaction."""
        from sqlalchemy import text
        from belief_engine.decay import valence_from_signal_type, half_life_for_kind
        parameters = dict(bid=belief_id, source_type=ev.source_type, source_date=ev.source_date,
                          source_ref=ev.source_ref, signal_type=ev.signal_type, summary=ev.summary, raw_text=ev.raw_text)
        existing = session.execute(text(
            'SELECT id FROM belief_evidence WHERE belief_id=:bid AND source_type IS :source_type '
            'AND source_date IS :source_date AND source_ref IS :source_ref AND signal_type IS :signal_type '
            'AND summary IS :summary AND raw_text IS :raw_text LIMIT 1'), parameters).first()
        if existing:
            return False
        import sqlite3
        from belief_engine.matching.context import lineage
        raw = session.connection().connection.driver_connection
        previous_factory = raw.row_factory
        try:
            raw.row_factory = sqlite3.Row
            _, inherited = lineage(raw,belief_id)
        finally:
            raw.row_factory = previous_factory
        identity_fields = ('source_type','source_date','source_ref','signal_type','summary','raw_text')
        if any(all(item.get(k) == getattr(ev,k) for k in identity_fields) for item in inherited):
            return False
        kind = session.execute(text('SELECT kind FROM user_beliefs WHERE id=:id'), {'id':belief_id}).scalar()
        half_life = half_life_for_kind(kind)
        session.add(BeliefEvidence(id=str(uuid.uuid4()),belief_id=belief_id,
            source_type=ev.source_type,source_date=ev.source_date,source_ref=ev.source_ref,
            signal_type=ev.signal_type,summary=ev.summary,raw_text=ev.raw_text,weight=ev.weight,
            valence=ev.valence or valence_from_signal_type(ev.signal_type),
            half_life_days_snapshot=half_life if half_life is not None else -1,
            extracted_by=ev.extracted_by,created_at=now))
        session.flush()
        return True

    @staticmethod
    def _refresh_observation_metadata(session, belief_id: str) -> None:
        """Count source observations, including merged provenance, in this transaction."""
        import sqlite3
        from sqlalchemy import text
        from belief_engine.matching.context import observations
        raw = session.connection().connection.driver_connection
        previous_factory = raw.row_factory
        try:
            raw.row_factory = sqlite3.Row
            sources = observations(raw, belief_id)
        finally:
            raw.row_factory = previous_factory
        dated = [e['source_date'] for e in sources if e.get('source_date')]
        confirmed = [e['source_date'] for e in sources if e.get('source_date')
                     and (e.get('valence') == 'support' if e.get('valence') else e.get('signal_type') == 'confirms')]
        session.execute(text('UPDATE user_beliefs SET observation_count=:count, '
            'first_observed=COALESCE(:first,first_observed), last_confirmed=COALESCE(:last,last_confirmed) '
            'WHERE id=:id'), {'count':len(sources), 'first':min(dated) if dated else None,
                              'last':max(confirmed) if confirmed else None,'id':belief_id})

    def _insert_evidence(self, belief_id: str, ev: EvidenceInput, now: str) -> None:
        """Atomically attach source evidence and refresh observation metadata."""
        from sqlalchemy import text
        session = get_session()
        try:
            session.execute(text('BEGIN IMMEDIATE'))
            self._append_evidence(session, belief_id, ev, now)
            self._refresh_observation_metadata(session, belief_id)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
