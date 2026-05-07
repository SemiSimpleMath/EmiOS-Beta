"""
Step: goal_outcome_detect

Watches recent chat for explicit "I achieved X" / "I gave up on X"
statements about active Goal nodes, and closes those Goals as terminal
(achieved or abandoned). Distinct from goal_dormancy_sweep:

  goal_dormancy_sweep — active → dormant (reversible, no end_date)
  goal_outcome_detect — active|dormant → achieved|abandoned (terminal,
                        end_date set, recorded in user's life narrative)

Conservative by design: when the chat evidence is ambiguous, the agent
returns `no_signal` and the Goal stays in its current state. The
dormancy sweep handles silent abandonment; this step handles only the
explicit signals.

Pipeline:
  1. Pick the K most-recently-touched active Goal nodes (most likely
     to have fresh chat evidence around them).
  2. For each, harvest recent chat windows that mention the Goal's
     label/keywords (within a bounded recency window).
  3. Hand the batch to goal_outcome_detector — it returns verdicts.
  4. For verdicts with outcome != no_signal AND confidence ≥ threshold,
     set goal_status + end_date on the node, write kg_revision_log row.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text as sql_text
from sqlalchemy.orm.attributes import flag_modified

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.models.base import get_session

logger = get_logger(__name__)


GOALS_PER_RUN = 10
RECENCY_WINDOW_DAYS = 60
EVIDENCE_PER_GOAL = 8
MIN_CONFIDENCE = 0.7


def run(ctx: PipelineContext, *, limit: Optional[int] = None) -> dict:
    """Returns counts: {goals_examined, achieved, abandoned, no_signal,
    skipped_low_confidence, skipped_no_evidence}."""
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node

    limit = int(limit or GOALS_PER_RUN)

    session = get_session()
    try:
        candidates = (
            session.query(
                Node.id, Node.label, Node.original_sentence,
                Node.created_at, Node.attributes,
            )
            .filter(Node.node_type == "Goal")
            .filter(Node.end_date.is_(None))  # not already terminal
            .all()
        )
    finally:
        session.close()

    # Filter to active or dormant (skip terminals defensively).
    active_goals: List[Dict[str, Any]] = []
    for nid, label, sent, created_at, attrs in candidates:
        a = attrs if isinstance(attrs, dict) else {}
        status = (a.get("goal_status") or "active").lower()
        if status in ("achieved", "abandoned"):
            continue
        active_goals.append({
            "id": str(nid),
            "label": label or "",
            "sentence": sent or "",
            "created_at": created_at,
            "last_pursued_at": _parse_ts(a.get("last_pursued_at")),
            "last_observed": _parse_ts(a.get("last_observed")),
        })

    # Pick the K most-recently-touched (descending by max(last_pursued, last_observed)).
    def _recency(g):
        return max(
            g["last_pursued_at"] or g["created_at"] or datetime.min,
            g["last_observed"] or g["created_at"] or datetime.min,
        )
    active_goals.sort(key=_recency, reverse=True)
    batch = active_goals[:limit]

    if not batch:
        logger.info("[goal_outcome_detect] no active Goal nodes; nothing to do")
        return _empty_result()

    # Gather recent chat evidence per Goal.
    cutoff = datetime.utcnow() - timedelta(days=RECENCY_WINDOW_DAYS)
    enriched = [
        {**g, "evidence": _fetch_recent_evidence(g["label"], g["sentence"], cutoff)}
        for g in batch
    ]

    # Skip Goals with zero recent evidence — agent has nothing to read.
    has_evidence = [e for e in enriched if e["evidence"]]
    skipped_no_evidence = len(enriched) - len(has_evidence)
    if not has_evidence:
        logger.info(
            "[goal_outcome_detect] examined=%d, no recent evidence on any",
            len(batch),
        )
        return {
            "goals_examined": len(batch),
            "achieved": 0, "abandoned": 0, "no_signal": 0,
            "skipped_low_confidence": 0,
            "skipped_no_evidence": skipped_no_evidence,
        }

    # Call the agent in one batch.
    agent = DI.agent_factory.create_agent("goal_outcome_detector")
    if agent is None:
        raise RuntimeError("goal_outcome_detector agent not registered")

    payload = _serialize_for_agent(has_evidence)
    try:
        result = agent.action_handler(Message(agent_input={"candidate_outcomes": payload}))
        data = getattr(result, "data", None)
    except Exception as e:
        logger.error("[goal_outcome_detect] agent call failed: %s", e, exc_info=True)
        return {
            "goals_examined": len(has_evidence),
            "achieved": 0, "abandoned": 0, "no_signal": 0,
            "skipped_low_confidence": 0,
            "skipped_no_evidence": skipped_no_evidence,
        }

    if not isinstance(data, dict):
        logger.warning("[goal_outcome_detect] non-dict from agent: %s", type(data).__name__)
        return {
            "goals_examined": len(has_evidence),
            "achieved": 0, "abandoned": 0, "no_signal": 0,
            "skipped_low_confidence": 0,
            "skipped_no_evidence": skipped_no_evidence,
        }

    verdicts = list(data.get("verdicts") or [])
    by_id = {g["id"]: g for g in has_evidence}

    achieved_count = 0
    abandoned_count = 0
    no_signal_count = 0
    skipped_low_conf = 0
    to_apply: List[Dict[str, Any]] = []

    for v in verdicts:
        if not isinstance(v, dict):
            continue
        gid = v.get("goal_node_id")
        if gid not in by_id:
            continue
        outcome = (v.get("outcome") or "no_signal").lower()
        if outcome == "no_signal":
            no_signal_count += 1
            continue
        try:
            conf = float(v.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < MIN_CONFIDENCE:
            skipped_low_conf += 1
            continue
        if outcome not in ("achieved", "abandoned"):
            continue
        to_apply.append({
            "goal_node_id": gid,
            "outcome": outcome,
            "confidence": conf,
            "evidence_quote": (v.get("evidence_quote") or "")[:600],
            "reasoning": (v.get("reasoning") or "")[:600],
            "label": by_id[gid]["label"],
        })
        if outcome == "achieved":
            achieved_count += 1
        else:
            abandoned_count += 1

    # Apply terminal closures.
    if to_apply:
        _apply_outcomes(to_apply)

    logger.info(
        "[goal_outcome_detect] examined=%d achieved=%d abandoned=%d "
        "no_signal=%d low_conf=%d no_evidence=%d",
        len(has_evidence), achieved_count, abandoned_count, no_signal_count,
        skipped_low_conf, skipped_no_evidence,
    )
    return {
        "goals_examined": len(has_evidence),
        "achieved": achieved_count,
        "abandoned": abandoned_count,
        "no_signal": no_signal_count,
        "skipped_low_confidence": skipped_low_conf,
        "skipped_no_evidence": skipped_no_evidence,
    }


def _empty_result() -> dict:
    return {
        "goals_examined": 0, "achieved": 0, "abandoned": 0,
        "no_signal": 0, "skipped_low_confidence": 0, "skipped_no_evidence": 0,
    }


def _fetch_recent_evidence(
    label: str, sentence: str, cutoff: datetime
) -> List[Dict[str, Any]]:
    """Pull the most-recent chat messages that mention the Goal's label
    or its key nouns. Bounded to RECENCY_WINDOW_DAYS so old chats don't
    re-fire. Filters to user-authored messages (role='user') so the
    agent isn't misled by Emi paraphrasing the Goal back."""
    if not label:
        return []
    keywords = [label.strip()]
    # Cheap noun extraction — pull capitalized words from the sentence
    # too; they're often the proper-noun anchors the user references.
    if sentence:
        for tok in sentence.split():
            t = tok.strip(",.;:!?\"'()[]")
            if len(t) >= 3 and t[0].isupper() and t.lower() != label.strip().lower():
                keywords.append(t)
    keywords = list(dict.fromkeys(keywords))[:5]

    session = get_session()
    try:
        like_clauses = " OR ".join([f"LOWER(message) LIKE :kw{i}" for i in range(len(keywords))])
        params = {"cutoff": cutoff}
        for i, kw in enumerate(keywords):
            params[f"kw{i}"] = f"%{kw.lower()}%"
        rows = session.execute(
            sql_text(f"""
                SELECT timestamp, role, COALESCE(speaker_name, role) AS speaker, message
                FROM unified_log_2026
                WHERE timestamp >= :cutoff
                  AND role = 'user'
                  AND ({like_clauses})
                ORDER BY timestamp DESC
                LIMIT {EVIDENCE_PER_GOAL}
            """),
            params,
        ).fetchall()
        return [
            {
                "timestamp": str(r[0]),
                "speaker": r[2] or r[1],
                "message": (r[3] or "")[:300],
            }
            for r in rows
        ]
    except Exception as e:
        logger.debug("[goal_outcome_detect] evidence query failed: %s", e)
        return []
    finally:
        session.close()


def _serialize_for_agent(goals: List[Dict[str, Any]]) -> str:
    """Build the candidate_outcomes block the agent reads."""
    parts = [f"Candidates: {len(goals)} active Goal nodes\n"]
    for g in goals:
        parts.append(f"--- goal {g['id'][:8]} ---")
        parts.append(f"goal_node_id: {g['id']}")
        parts.append(f"goal_label: {g['label']!r}")
        if g.get("sentence"):
            parts.append(f"goal_sentence: {g['sentence']}")
        if g.get("created_at"):
            parts.append(f"created_at: {g['created_at']}")
        ev = g.get("evidence") or []
        if not ev:
            parts.append("recent_evidence: (none)")
        else:
            parts.append(f"recent_evidence ({len(ev)} messages, newest first):")
            for e in ev:
                parts.append(f"  [{e['timestamp']}] {e['speaker']}: {e['message']}")
        parts.append("")
    return "\n".join(parts)


def _apply_outcomes(applies: List[Dict[str, Any]]) -> None:
    """Set goal_status + end_date on the target Goal nodes; write
    kg_revision_log audit row per closure."""
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node

    now = datetime.now(timezone.utc)
    session = get_session()
    try:
        for entry in applies:
            n = session.query(Node).filter(Node.id == entry["goal_node_id"]).first()
            if n is None:
                continue
            attrs = dict(n.attributes or {}) if isinstance(n.attributes, dict) else {}
            before = {
                "goal_status": attrs.get("goal_status") or "active",
                "end_date": str(n.end_date) if n.end_date else None,
            }
            attrs["goal_status"] = entry["outcome"]
            attrs[f"{entry['outcome']}_at"] = now.isoformat()
            attrs[f"{entry['outcome']}_evidence"] = entry["evidence_quote"]
            n.attributes = attrs
            flag_modified(n, "attributes")
            n.end_date = now
            n.end_date_confidence = f"{entry['outcome']}_detected"
            n.end_date_prose = entry["reasoning"][:200]

            session.execute(
                sql_text(
                    "INSERT INTO kg_revision_log "
                    "(id, op, args_json, before_json, after_json, reason, "
                    " agent_id, succeeded, created_at) "
                    "VALUES (:id, :op, :args, :before, :after, :reason, "
                    "        :agent, 1, :now)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "op": f"goal_set_{entry['outcome']}",
                    "args": json.dumps({"node_id": entry["goal_node_id"]}),
                    "before": json.dumps(before),
                    "after": json.dumps({
                        "goal_status": entry["outcome"],
                        "end_date": now.isoformat(),
                    }),
                    "reason": (
                        f"User-confirmed {entry['outcome']}: "
                        f"{entry['reasoning']} (conf {entry['confidence']})"
                    )[:500],
                    "agent": "goal_outcome_detector",
                    "now": now,
                },
            )
        session.commit()
    finally:
        session.close()


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is None else value.astimezone(timezone.utc).replace(tzinfo=None)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo is None else dt.astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            return None
    return None
