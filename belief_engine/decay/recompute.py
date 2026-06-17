"""Snapshot job — recompute and persist evidence-weighted confidence for
every active belief.

Run path:
  1. Pull every active belief from user_beliefs (optionally filter by domain).
  2. For each belief, fetch its belief_evidence rows.
  3. Aggregate via compute_belief_weights(); decide band.
  4. Write back: current_support_weight, current_contradiction_weight,
     current_net_weight, current_confidence_band, last_contradicted_at.
  5. If band == 'faded' → deprecate belief (terminal).
  6. If band == 'contested' or 'deprecated_by_contradiction' → mark status
     contested so ReevaluateBeliefsStep picks it up.

Pure-Python aggregation. No LLM calls. Order is deterministic.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy import text

from app.models.base import get_session
from belief_engine.decay.model import (
    band_for_weights,
    compute_belief_weights,
    valence_from_signal_type,
    EVIDENCE_BASE_WEIGHT,
    DEFAULT_BASE_WEIGHT,
)

logger = logging.getLogger(__name__)


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


@dataclass
class RecomputeStats:
    scanned: int = 0
    updated: int = 0
    faded: int = 0
    contested_flipped: int = 0
    skipped_no_evidence: int = 0
    errors: int = 0


def recompute_belief_snapshots(
    domain: Optional[str] = None,
    *,
    now_utc: Optional[datetime] = None,
) -> RecomputeStats:
    """Recompute snapshot columns for every active belief (optionally one domain).

    Faded beliefs (net weight below the floor) are deprecated. Contested or
    contradiction-dominated beliefs are flipped to status='contested' so the
    LLM reevaluator picks them up next pipeline run.

    Idempotent — re-running with no new evidence yields the same snapshot.
    """
    now = now_utc or datetime.now(timezone.utc)
    stats = RecomputeStats()

    session = get_session()
    try:
        # 1. Pull active beliefs (optionally filtered to one domain).
        if domain:
            beliefs = session.execute(
                text(
                    "SELECT id, domain, belief_key, kind, status, scope "
                    "FROM user_beliefs WHERE status = 'active' AND domain = :d"
                ),
                {"d": domain},
            ).fetchall()
        else:
            beliefs = session.execute(
                text(
                    "SELECT id, domain, belief_key, kind, status, scope "
                    "FROM user_beliefs WHERE status = 'active'"
                ),
            ).fetchall()

        stats.scanned = len(beliefs)
        logger.info("[belief_decay] recomputing %d active beliefs%s",
                    stats.scanned, f" (domain={domain})" if domain else "")

        # Map each id to its evidence rows.
        belief_ids = [str(r[0]) for r in beliefs]
        if not belief_ids:
            return stats

        # Fetch all evidence for these beliefs in one go.
        # Chunk by 500 to keep IN clause sane on SQLite.
        evidence_by_belief: Dict[str, List] = {bid: [] for bid in belief_ids}
        CHUNK = 500
        for i in range(0, len(belief_ids), CHUNK):
            chunk = belief_ids[i:i + CHUNK]
            placeholders = ",".join(f":id{j}" for j in range(len(chunk)))
            params = {f"id{j}": chunk[j] for j in range(len(chunk))}
            rows = session.execute(
                text(
                    f"SELECT belief_id, source_type, signal_type, valence, weight, "
                    f"  source_date, created_at "
                    f"FROM belief_evidence WHERE belief_id IN ({placeholders})"
                ),
                params,
            ).fetchall()
            for ev_row in rows:
                evidence_by_belief.setdefault(str(ev_row[0]), []).append(ev_row)

        # 2. Per belief: aggregate and decide.
        for b in beliefs:
            try:
                bid = str(b[0])
                kind = b[3] or None
                ev_rows = evidence_by_belief.get(bid, [])
                if not ev_rows:
                    stats.skipped_no_evidence += 1
                    continue

                # Build (base_weight, valence, observed_at) tuples.
                events = []
                last_contradicted: Optional[datetime] = None
                for er in ev_rows:
                    source_type = er[1] or ""
                    signal_type = er[2] or ""
                    valence_db = er[3]  # may be NULL on legacy rows
                    weight_db = er[4]
                    source_date = er[5]
                    created_at = er[6]
                    observed_at = _parse_iso(source_date) or _parse_iso(created_at)
                    if observed_at is None:
                        continue
                    # Prefer stored valence; fall back to signal_type mapping.
                    valence = valence_db or valence_from_signal_type(signal_type)
                    # Prefer stored weight; fall back to source_type baseline.
                    if weight_db is None or weight_db == 0.0:
                        base_w = EVIDENCE_BASE_WEIGHT.get(source_type, DEFAULT_BASE_WEIGHT)
                    else:
                        base_w = float(weight_db)
                    # Ignore bookkeeping events (canonicalization, deprecation rows).
                    if base_w == 0.0:
                        continue
                    events.append((base_w, valence, observed_at))
                    if valence == "contradict":
                        if last_contradicted is None or observed_at > last_contradicted:
                            last_contradicted = observed_at

                if not events:
                    stats.skipped_no_evidence += 1
                    continue

                weights = compute_belief_weights(events, kind, now)
                band = band_for_weights(weights)

                # 3. Write snapshot columns.
                set_cols = {
                    "support":       round(weights.support_weight, 4),
                    "contradiction": round(weights.contradiction_weight, 4),
                    "net":           round(weights.net_weight, 4),
                    "band":          band,
                    "lc":            last_contradicted.isoformat() if last_contradicted else None,
                    "id":            bid,
                }
                session.execute(
                    text(
                        "UPDATE user_beliefs SET "
                        "  current_support_weight = :support, "
                        "  current_contradiction_weight = :contradiction, "
                        "  current_net_weight = :net, "
                        "  current_confidence_band = :band, "
                        "  last_contradicted_at = COALESCE(:lc, last_contradicted_at) "
                        "WHERE id = :id"
                    ),
                    set_cols,
                )

                # 4. Terminal/contested status transitions.
                if band == "faded":
                    session.execute(
                        text(
                            "UPDATE user_beliefs SET status='deprecated', "
                            "updated_at=:now WHERE id = :id AND status='active' AND COALESCE(locked,0)=0"
                        ),
                        {"now": now.isoformat(), "id": bid},
                    )
                    stats.faded += 1
                elif band in ("contested", "deprecated_by_contradiction"):
                    session.execute(
                        text(
                            "UPDATE user_beliefs SET status='contested', "
                            "updated_at=:now WHERE id = :id AND status='active' AND COALESCE(locked,0)=0"
                        ),
                        {"now": now.isoformat(), "id": bid},
                    )
                    stats.contested_flipped += 1
                stats.updated += 1
            except Exception as exc:
                stats.errors += 1
                logger.exception("[belief_decay] recompute failed for belief %s: %s", b[0], exc)

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    logger.info(
        "[belief_decay] done: scanned=%d updated=%d faded=%d contested=%d skipped=%d errors=%d",
        stats.scanned, stats.updated, stats.faded, stats.contested_flipped,
        stats.skipped_no_evidence, stats.errors,
    )
    return stats
