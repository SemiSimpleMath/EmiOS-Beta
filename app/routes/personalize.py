"""Personalization dashboard routes — gives the user direct visibility +
control over the things that shape Emi's day-to-day behavior:

- Beliefs (today): trending-up / challenged / recently-deprecated views
  plus per-belief evidence drill-down so the user can see exactly what
  signals are reaching the engine.

Planned sibling pages (not yet built — placeholders in the menu):
- Personalize directives (resource_orchestrator_user_prefs_personal.md
  editor with live-rendered preview)
- Cron reminders (one-click toggle / time-edit for cron_reminder routines
  buried in configs/routines.json)

Reads the live SQLite tables, not the exported JSON, because the JSON
projection has no trend signal (just current state).
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List

from flask import Blueprint, jsonify, render_template, request
from sqlalchemy import text as _sql

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import utc_now
from app.models.base import get_session

logger = get_logger(__name__)

personalize_bp = Blueprint("personalize", __name__)


# Defaults tuned against the user's belief volume (~528 active beliefs,
# ~120 evidence rows/day). Can be overridden via query params.
_DEFAULT_WINDOW_DAYS = 14
_DEFAULT_MIN_NET_FOR_TRENDING = 3
_DEFAULT_LIMIT = 12


# ── Data layer ────────────────────────────────────────────────────────────────


def _real_evidence_filter() -> str:
    """source_types that represent real signals (not internal audit rows).
    Excludes 'deprecation', 'canonicalization', 'manual_seed', 'decay_review'
    so the trending counts reflect actual user-driven evidence, not the
    bookkeeping the engine writes when it merges/closes beliefs."""
    return "source_type IN ('daily_insights', 'ticket_acceptance', 'ticket_rejection', 'kg_edge')"


def _trending_up(session, days: int, min_net: int, limit: int) -> List[Dict[str, Any]]:
    cutoff = (utc_now() - timedelta(days=days)).date().isoformat()
    rows = session.execute(_sql(
        f"""
        SELECT b.id, b.belief_key, b.statement, b.domain, b.confidence,
               b.scope, b.observation_count,
               SUM(CASE WHEN e.signal_type='confirms' THEN 1 ELSE 0 END) AS confirms,
               SUM(CASE WHEN e.signal_type IN ('rejects','contradicts') THEN 1 ELSE 0 END) AS challenges,
               SUM(CASE WHEN e.signal_type='qualifies' THEN 1 ELSE 0 END) AS qualifies
        FROM user_beliefs b
        JOIN belief_evidence e ON e.belief_id = b.id
        WHERE b.status = 'active'
          AND e.source_date >= :cutoff
          AND {_real_evidence_filter()}
        GROUP BY b.id
        HAVING (confirms - challenges) >= :min_net
        ORDER BY (confirms - challenges) DESC, confirms DESC
        LIMIT :limit
        """),
        {"cutoff": cutoff, "min_net": min_net, "limit": limit},
    ).mappings().all()
    return [_belief_dict(r) for r in rows]


def _challenged(session, days: int, limit: int) -> List[Dict[str, Any]]:
    """Active beliefs where real-evidence challenges >= confirms in the
    window and there's at least one challenge. These are the beliefs
    actually losing ground in live signal — the engine doesn't always
    catch them yet (contestation path is dormant), so surfacing here is
    the cheap way to see what to investigate."""
    cutoff = (utc_now() - timedelta(days=days)).date().isoformat()
    rows = session.execute(_sql(
        f"""
        SELECT b.id, b.belief_key, b.statement, b.domain, b.confidence,
               b.scope, b.observation_count,
               SUM(CASE WHEN e.signal_type='confirms' THEN 1 ELSE 0 END) AS confirms,
               SUM(CASE WHEN e.signal_type IN ('rejects','contradicts') THEN 1 ELSE 0 END) AS challenges,
               SUM(CASE WHEN e.signal_type='qualifies' THEN 1 ELSE 0 END) AS qualifies
        FROM user_beliefs b
        JOIN belief_evidence e ON e.belief_id = b.id
        WHERE b.status = 'active'
          AND e.source_date >= :cutoff
          AND {_real_evidence_filter()}
        GROUP BY b.id
        HAVING challenges > 0 AND challenges >= confirms
        ORDER BY (challenges - confirms) DESC, challenges DESC
        LIMIT :limit
        """),
        {"cutoff": cutoff, "limit": limit},
    ).mappings().all()
    return [_belief_dict(r) for r in rows]


def _recently_deprecated(session, days: int, limit: int) -> List[Dict[str, Any]]:
    """Deprecation events in the window. The summary on the deprecation/
    canonicalization audit row carries the LLM's reasoning ('merged into
    X because...'), which is the audit trail the user wants to see."""
    cutoff = (utc_now() - timedelta(days=days)).date().isoformat()
    rows = session.execute(_sql(
        """
        SELECT b.id, b.belief_key, b.statement, b.domain, b.confidence,
               b.scope, b.observation_count,
               MAX(e.source_date) AS deprecated_on,
               (
                 SELECT ee.summary FROM belief_evidence ee
                 WHERE ee.belief_id = b.id
                   AND ee.source_type IN ('deprecation', 'canonicalization')
                 ORDER BY ee.source_date DESC LIMIT 1
               ) AS reason
        FROM user_beliefs b
        JOIN belief_evidence e ON e.belief_id = b.id
        WHERE b.status = 'deprecated'
          AND e.source_type IN ('deprecation', 'canonicalization')
          AND e.source_date >= :cutoff
        GROUP BY b.id
        ORDER BY deprecated_on DESC
        LIMIT :limit
        """),
        {"cutoff": cutoff, "limit": limit},
    ).mappings().all()
    out = []
    for r in rows:
        d = _belief_dict(r)
        d["deprecated_on"] = r["deprecated_on"]
        d["reason"] = (r["reason"] or "")
        out.append(d)
    return out


def _evidence_for_belief(session, belief_id: str, limit: int) -> List[Dict[str, Any]]:
    rows = session.execute(_sql(
        """
        SELECT id, source_type, source_date, signal_type, summary, raw_text, weight
        FROM belief_evidence
        WHERE belief_id = :bid
        ORDER BY source_date DESC, id DESC
        LIMIT :limit
        """),
        {"bid": belief_id, "limit": limit},
    ).mappings().all()
    return [
        {
            "id": r["id"],
            "source_type": r["source_type"],
            "source_date": r["source_date"],
            "signal_type": r["signal_type"],
            "summary": r["summary"] or "",
            "raw_text": r["raw_text"] or "",
            "weight": r["weight"],
        }
        for r in rows
    ]


def _belief_dict(row) -> Dict[str, Any]:
    keys = row.keys()
    return {
        "id": row["id"],
        "belief_key": row["belief_key"],
        "statement": row["statement"],
        "domain": row["domain"],
        "confidence": row["confidence"],
        "scope": row["scope"],
        "observation_count": row["observation_count"],
        "confirms": int(row["confirms"]) if "confirms" in keys and row["confirms"] is not None else None,
        "challenges": int(row["challenges"]) if "challenges" in keys and row["challenges"] is not None else None,
        "qualifies": int(row["qualifies"]) if "qualifies" in keys and row["qualifies"] is not None else None,
    }


# ── Routes ────────────────────────────────────────────────────────────────────


@personalize_bp.route("/personalize")
def personalize_landing():
    """Landing page — links to the per-area dashboards."""
    return render_template("personalize.html")


@personalize_bp.route("/personalize/beliefs")
def personalize_beliefs():
    return render_template("personalize_beliefs.html")


@personalize_bp.route("/api/personalize/beliefs/trends")
def api_belief_trends():
    """Return three lists: trending_up / challenged / recently_deprecated.

    Optional query params:
      ?days=N              window in days (default 14)
      ?min_net=N           min confirms-challenges for trending_up (default 3)
      ?limit=N             per-section cap (default 12)
    """
    days = int(request.args.get("days") or _DEFAULT_WINDOW_DAYS)
    min_net = int(request.args.get("min_net") or _DEFAULT_MIN_NET_FOR_TRENDING)
    limit = int(request.args.get("limit") or _DEFAULT_LIMIT)

    s = get_session()
    try:
        trending_up = _trending_up(s, days, min_net, limit)
        challenged = _challenged(s, days, limit)
        deprecated = _recently_deprecated(s, days, limit)
    except Exception as e:
        logger.error("[personalize] belief trends failed: %s", e, exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        s.close()

    return jsonify({
        "success": True,
        "window_days": days,
        "trending_up": trending_up,
        "challenged": challenged,
        "recently_deprecated": deprecated,
    })


@personalize_bp.route("/api/personalize/beliefs/<belief_id>/evidence")
def api_belief_evidence(belief_id: str):
    """Return the recent evidence trail for one belief — what signals
    reached it. Useful for diagnosing 'why is this belief still active'
    or 'why did the engine think this was high-confidence'."""
    limit = int(request.args.get("limit") or 25)

    s = get_session()
    try:
        belief_row = s.execute(_sql(
            "SELECT id, belief_key, statement, domain, confidence, scope, "
            "       status, observation_count, first_observed, last_confirmed "
            "FROM user_beliefs WHERE id = :bid"),
            {"bid": belief_id},
        ).mappings().first()
        if belief_row is None:
            return jsonify({"success": False, "error": "belief not found"}), 404
        belief = dict(belief_row)
        evidence = _evidence_for_belief(s, belief_id, limit)
    except Exception as e:
        logger.error("[personalize] belief evidence failed: %s", e, exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        s.close()

    return jsonify({
        "success": True,
        "belief": belief,
        "evidence": evidence,
    })
