"""
Step: goal_dormancy_sweep

Flips long-silent Goal nodes from `active` to `dormant`. Reversible —
ANY re-observation in proposal_promoter._refresh_on_reobservation flips
them back to `active`. Achievement/abandonment closures are terminal
and outside this step's scope (those go through goal_outcome_detector).

Why this isn't state_decay:
  - state_decay sets `end_date` and treats the State/Event as ENDED.
    Wrong for Goals — silently retiring a real intent the user still
    cares about is a worse failure mode than letting an old Goal
    linger as visual noise.
  - This step ONLY mutates `attributes.goal_status` (active → dormant).
    Never sets `end_date`. Lens / cards / visualizer can filter to
    `goal_status='active'` to hide stale Goals from default views;
    user can browse `dormant` as a "what I haven't been pursuing"
    surface.

Dormancy threshold per Goal:
  - If the Goal has a TTL with `estimated_duration_days`: dormancy
    threshold = max(estimated_duration_days * 1.5, 30 days). The 1.5x
    bias gives an active Goal pursued at-cadence headroom past its
    "expected" duration before going dormant.
  - If no TTL (most Goals today, since RELATIONSHIP_LIKE_TYPES excludes
    Goal in the promoter): default 90 days. Long enough that quarterly
    pursuits don't go dormant; short enough that abandoned Goals fade
    within a season.
  - Fixed cap at 730 days (2 years) so even durable-flagged Goals
    eventually fade absent any re-observation. (Override: don't dormant
    Goals whose label/category indicates lifelong values — see
    DURABLE_GOAL_KEYWORDS below.)

Recency signal:
  - Read `attributes.last_pursued_at` (bumped by _refresh_on_reobservation).
  - Fall back to `first_observed`, then `created_at`, like state_decay.

Per-run output:
  - Counts: candidates_examined, marked_dormant, skipped_durable,
    skipped_already_dormant, skipped_terminal.
  - Per-Goal kg_revision_log row with op='goal_set_dormant'.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text as sql_text
from sqlalchemy.orm.attributes import flag_modified

from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)


# Goals matching any of these patterns in label/category are "lifelong
# values" and never go dormant by sweep alone — only via explicit
# achievement/abandonment. Keeps "stay healthy" / "be a good parent" /
# career-defining values from fading just because they're rarely
# explicitly re-mentioned.
DURABLE_GOAL_KEYWORDS = (
    "lifelong", "lifetime", "value", "principle", "purpose",
    "be a good", "stay healthy", "live well", "be present",
)

DEFAULT_DORMANCY_DAYS = 90
DORMANCY_TTL_MULTIPLIER = 1.5
DORMANCY_FLOOR_DAYS = 30
DORMANCY_CEILING_DAYS = 730


def run(ctx: PipelineContext) -> dict:
    """Returns counts: {candidates_examined, marked_dormant,
    skipped_durable, skipped_already_dormant, skipped_terminal,
    skipped_recent_activity}."""
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node

    now = datetime.utcnow()
    candidates_examined = 0
    marked_dormant = 0
    skipped_durable = 0
    skipped_already_dormant = 0
    skipped_terminal = 0
    skipped_recent_activity = 0

    to_dormant: List[Dict[str, Any]] = []

    session = get_session()
    try:
        # Goal nodes with no end_date — closed (achieved/abandoned) goals
        # should never come through this step.
        candidates = (
            session.query(
                Node.id, Node.label, Node.node_type, Node.category,
                Node.created_at, Node.attributes, Node.locked_by_user_at,
                Node.goal_status,
            )
            .filter(Node.node_type == "Goal")
            .filter(Node.end_date.is_(None))
            .all()
        )

        for row in candidates:
            nid, label, ntype, category, created_at, attributes, locked_at, col_goal_status = row
            candidates_examined += 1

            attrs = attributes if isinstance(attributes, dict) else {}
            if isinstance(attributes, str):
                try:
                    attrs = json.loads(attributes)
                except Exception:
                    attrs = {}

            # Locked goals never auto-transition.
            if locked_at is not None:
                continue

            # goal_status lives on the first-class column. Empty/null
            # string treated as 'active' (the canonical default).
            cur_status = (col_goal_status or "active").lower()
            if cur_status == "dormant":
                skipped_already_dormant += 1
                continue
            if cur_status in ("completed", "abandoned"):
                # Terminal — shouldn't fire (end_date should also be set)
                # but guard anyway.
                skipped_terminal += 1
                continue

            # Lifelong-value override.
            haystack = " ".join([
                str(label or ""), str(category or ""),
            ]).lower()
            if any(kw in haystack for kw in DURABLE_GOAL_KEYWORDS):
                skipped_durable += 1
                continue

            threshold_days = _dormancy_threshold_days(attrs)
            anchor = _resolve_recency_anchor(attrs, created_at)
            if anchor is None:
                # No recency signal at all — be conservative, skip.
                skipped_recent_activity += 1
                continue
            silence = (now - anchor).total_seconds() / 86400.0
            if silence < threshold_days:
                skipped_recent_activity += 1
                continue

            to_dormant.append({
                "node_id": str(nid),
                "label": label,
                "category": category,
                "anchor": anchor.isoformat(),
                "silence_days": round(silence, 1),
                "threshold_days": threshold_days,
                "attrs": attrs,
            })

        # Apply transitions in one short write phase. goal_status goes
        # on the first-class column; dormant_since / dormant_reason are
        # bookkeeping that lives on attributes.
        for entry in to_dormant:
            node = session.query(Node).filter(Node.id == entry["node_id"]).first()
            if node is None:
                continue
            attrs = dict(entry["attrs"])
            attrs["dormant_since"] = now.isoformat()
            attrs["dormant_reason"] = (
                f"silent for {entry['silence_days']}d (threshold "
                f"{entry['threshold_days']}d)"
            )
            node.attributes = attrs
            flag_modified(node, "attributes")
            node.goal_status = "dormant"

        session.commit()
        marked_dormant = len(to_dormant)
    finally:
        session.close()

    # Audit rows in kg_revision_log — separate session so commit on the
    # batch doesn't block other writers.
    if to_dormant:
        session = get_session()
        try:
            for entry in to_dormant:
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
                        "op": "goal_set_dormant",
                        "args": json.dumps({"node_id": entry["node_id"]}),
                        "before": json.dumps({
                            "goal_status": "active",  # column was 'active' or null
                        }),
                        "after": json.dumps({
                            "goal_status": "dormant",
                            "dormant_since": now.isoformat(),
                        }),
                        "reason": (
                            f"Silent for {entry['silence_days']}d (threshold "
                            f"{entry['threshold_days']}d). Reversible — any "
                            f"re-observation flips back to active."
                        ),
                        "agent": "goal_dormancy_sweep",
                        "now": now,
                    },
                )
            session.commit()
        finally:
            session.close()

    logger.info(
        "[goal_dormancy_sweep] candidates=%d marked_dormant=%d "
        "skipped_durable=%d skipped_already_dormant=%d skipped_terminal=%d "
        "skipped_recent=%d",
        candidates_examined, marked_dormant, skipped_durable,
        skipped_already_dormant, skipped_terminal, skipped_recent_activity,
    )
    return {
        "candidates_examined": candidates_examined,
        "marked_dormant": marked_dormant,
        "skipped_durable": skipped_durable,
        "skipped_already_dormant": skipped_already_dormant,
        "skipped_terminal": skipped_terminal,
        "skipped_recent_activity": skipped_recent_activity,
    }


def _dormancy_threshold_days(attrs: Dict[str, Any]) -> float:
    """Compute the dormancy threshold for one Goal node.

    Threshold = max(ttl.estimated_duration_days * 1.5, FLOOR), capped
    at CEILING. Defaults to DEFAULT_DORMANCY_DAYS when no TTL."""
    ttl = attrs.get("ttl") if isinstance(attrs, dict) else None
    if isinstance(ttl, dict):
        dur = ttl.get("estimated_duration_days")
        if isinstance(dur, (int, float)) and dur > 0:
            t = max(float(dur) * DORMANCY_TTL_MULTIPLIER, DORMANCY_FLOOR_DAYS)
            return min(t, DORMANCY_CEILING_DAYS)
    return DEFAULT_DORMANCY_DAYS


def _resolve_recency_anchor(
    attrs: Dict[str, Any], created_at: Optional[datetime]
) -> Optional[datetime]:
    """Pick the most-recent observation signal: last_pursued_at >
    last_observed > first_observed > created_at. All values normalized
    to naive UTC to match `now` in the caller."""
    candidates = []
    for key in ("last_pursued_at", "last_observed", "first_observed"):
        v = attrs.get(key) if isinstance(attrs, dict) else None
        if v:
            ts = _parse_ts(v)
            if ts is not None:
                candidates.append(ts)
    if created_at is not None:
        candidates.append(_to_naive_utc(created_at))
    if not candidates:
        return None
    return max(candidates)


def _parse_ts(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return _to_naive_utc(value)
    if isinstance(value, str):
        try:
            return _to_naive_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except Exception:
            return None
    return None


def _to_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt
