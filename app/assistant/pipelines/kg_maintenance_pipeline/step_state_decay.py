"""
Step: state_decay

Auto-closes the era of State/Event nodes whose TTL estimate has elapsed
without fresh observations. The TTL is set at promotion time by the
``state_ttl_estimator`` agent and stored in ``kg_node_metadata.attributes.ttl``.

For each active (valid_to IS NULL) State/Event node with a TTL:
  - Compute expected_end = start_date + estimated_duration_days.
  - If now > expected_end AND updated_at < expected_end (no re-observation
    since the expected end), set end_date = expected_end and mark
    end_date_confidence = "auto_decay".
  - Write a kg_maintenance_finding so the user can review the closure.

Conservative defaults:
  - Skip nodes whose TTL confidence < LOW_CONFIDENCE_FLOOR — they stay open.
  - Skip nodes whose updated_at is AFTER the expected_end (they were
    re-observed; keep them alive).
  - Apply a GRACE_DAYS buffer so we don't close the instant the TTL fires —
    wait for a bit of "silent" time past the estimate.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from app.assistant.kg_maintenance.store import upsert_finding
from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import parse_iso_utc
from app.models.base import get_session

logger = get_logger(__name__)


# Nodes whose TTL estimate has confidence below this stay open — the
# estimator is uncertain, so we don't close automatically.
LOW_CONFIDENCE_FLOOR = 0.4

# Extra buffer past the estimated end before we actually close. Gives
# the system a cushion for late-arriving observations and conservative behavior.
GRACE_DAYS = 3

# Silence rule for finding emission: low-stakes closures don't deserve a
# review row. The closure still happens (node.end_date is set); we just
# don't write a finding the user has to dismiss. A closure is "noteworthy"
# (and gets a finding) only if at least one of these is true:
#   - duration_class in NOTEWORTHY_DURATION_CLASSES (medium/long/durable —
#     real-life consequence: jobs ending, projects ending, residences ending)
#   - confidence is shaky (< NOTEWORTHY_LOW_CONF_THRESHOLD) — TTL agent itself
#     is uncertain, so a human glance is cheap insurance
#   - node importance >= NOTEWORTHY_IMPORTANCE_FLOOR — important entity, even
#     short-term states deserve a heads-up
NOTEWORTHY_DURATION_CLASSES = {"medium_term", "long_term", "durable"}
NOTEWORTHY_LOW_CONF_THRESHOLD = 0.6
NOTEWORTHY_IMPORTANCE_FLOOR = 5


def run(ctx: PipelineContext) -> dict:
    """Returns {"scanned": int, "closed": int, "findings": int, "skipped_low_confidence": int}."""
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node

    # Naive UTC throughout this file — matches Node.start_date / end_date /
    # updated_at which read back as naive. _to_naive_utc normalizes the lone
    # aware column (created_at) so comparisons stay in one tz state.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    scanned = 0
    to_close: List[Tuple[str, str, str, datetime, dict]] = []
    skipped_low_conf = 0
    skipped_recent_activity = 0

    session = get_session()
    try:
        # Candidates: State/Event nodes with no end_date. We fall back to
        # created_at when start_date is NULL — many states (beliefs,
        # preferences) don't have a clear real-world onset date, but we
        # DO know when we first observed them. Using the observation
        # floor for decay is honest: "this is when we first noticed it;
        # if we haven't seen it in TTL days, auto-close."
        candidates = (
            session.query(
                Node.id, Node.label, Node.node_type, Node.start_date,
                Node.created_at, Node.updated_at, Node.attributes,
                Node.locked_by_user_at, Node.importance,
            )
            .filter(Node.node_type.in_(("State", "Event")))
            .filter(Node.end_date.is_(None))
            .all()
        )

        for row in candidates:
            nid, label, ntype, start_date, created_at, updated_at, attributes, locked_at, importance = row
            scanned += 1
            # Decay anchor: prefer real start_date; else the "first_observed"
            # floor carried from the originating proposal's evidence (stored
            # in attributes); else fall back to created_at as last resort.
            # All sources may arrive tz-aware (legacy data) or naive — coerce
            # to naive UTC to match `now` and the rest of the comparisons.
            first_observed = _extract_first_observed(attributes)
            reference_start = _to_naive_utc(start_date or first_observed or created_at)
            if reference_start is None:
                continue  # nothing to anchor decay against
            updated_at = _to_naive_utc(updated_at)

            # Locked nodes never auto-close.
            if locked_at is not None:
                continue

            ttl = _extract_ttl(attributes)
            if ttl is None:
                continue
            duration_days = ttl.get("estimated_duration_days")
            if not isinstance(duration_days, (int, float)) or duration_days <= 0:
                # "durable" or missing — leave open.
                continue

            confidence = float(ttl.get("confidence") or 0.0)
            if confidence < LOW_CONFIDENCE_FLOOR:
                skipped_low_conf += 1
                continue

            expected_end = reference_start + timedelta(days=float(duration_days))
            close_threshold = expected_end + timedelta(days=GRACE_DAYS)

            # Not yet ripe.
            if now < close_threshold:
                continue

            # Re-observed since expected end → keep alive. Use ONLY
            # ``attributes.last_observed`` here. Earlier fallback to
            # ``updated_at`` poisoned the filter — pagerank/description
            # maintenance writes bump updated_at on every node every run, so
            # 85% of nodes (those without last_observed) all looked
            # "recently observed" no matter how stale the underlying state
            # was. Result: 0 auto_decay closures despite 1,282+ candidates
            # past their TTL.
            #
            # Without last_observed populated, we treat the node as "not
            # re-observed since promotion" → close per TTL. The promoter's
            # matched-existing path is responsible for stamping
            # last_observed when the same fact gets reasserted; that's the
            # only legitimate signal of re-observation.
            last_observed = _extract_last_observed(attributes)
            if last_observed and last_observed >= expected_end:
                skipped_recent_activity += 1
                continue

            to_close.append((str(nid), label or "", ntype or "", expected_end, ttl, importance))

        # Apply closures atomically.
        for nid, label, ntype, expected_end, ttl, _importance in to_close:
            node = session.query(Node).filter(Node.id == nid).first()
            if node is None:
                continue
            node.end_date = expected_end
            node.end_date_confidence = "auto_decay"
            ref_date = node.start_date or node.created_at
            ref_label = "start_date" if node.start_date else "created_at (observation floor)"
            node.end_date_prose = (
                f"auto-closed by decay — TTL {ttl.get('duration_class')} "
                f"({ttl.get('estimated_duration_days')}d from {ref_date:%Y-%m-%d} [{ref_label}])"
            )
        session.commit()
    finally:
        session.close()

    # Write findings in a separate phase — but only for noteworthy closures.
    # Routine ephemeral/short_term closures of low-importance states are
    # already-applied facts; queueing them as review rows is pure noise.
    findings_created = 0
    findings_silenced = 0
    for nid, label, ntype, expected_end, ttl, importance in to_close:
        duration_class = ttl.get("duration_class") or ""
        ttl_confidence = float(ttl.get("confidence") or 0.5)
        node_importance = float(importance) if importance is not None else 0.0

        is_noteworthy = (
            duration_class in NOTEWORTHY_DURATION_CLASSES
            or ttl_confidence < NOTEWORTHY_LOW_CONF_THRESHOLD
            or node_importance >= NOTEWORTHY_IMPORTANCE_FLOOR
        )
        if not is_noteworthy:
            findings_silenced += 1
            continue

        _, created = upsert_finding(
            finding_type="state_auto_closed",
            primary_node_id=nid,
            suggested_action="review",
            reason=(
                f"State {label!r} ({ntype}) auto-closed at {expected_end:%Y-%m-%d} — "
                f"TTL class {duration_class} ({ttl.get('estimated_duration_days')}d). "
                f"Reason: {ttl.get('reasoning', '')}"
            ),
            confidence=ttl_confidence,
            priority="low",
            agent_name="state_decay",
            evidence={
                "label": label,
                "node_type": ntype,
                "expected_end": expected_end.isoformat(),
                "ttl": ttl,
                "node_importance": node_importance,
            },
            pipeline_run_id=ctx.run_id,
        )
        if created:
            findings_created += 1

    logger.info(
        "[state_decay] scanned=%d closed=%d findings=%d silenced=%d skipped_low_conf=%d skipped_recent=%d",
        scanned, len(to_close), findings_created, findings_silenced,
        skipped_low_conf, skipped_recent_activity,
    )
    return {
        "scanned": scanned,
        "closed": len(to_close),
        "findings": findings_created,
        "findings_silenced": findings_silenced,
        "skipped_low_confidence": skipped_low_conf,
        "skipped_recent_activity": skipped_recent_activity,
    }


def _to_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Coerce to naive UTC. The rest of this step uses naive UTC (matches
    the SQLAlchemy DateTime columns which are tz-naive in this codebase).
    Mixing aware + naive triggers TypeError on comparison."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _extract_first_observed(attributes):
    """Pull the 'first_observed' timestamp from node attributes, if present.

    Stored by the proposal writer from the originating evidence's
    observed_at timestamp. Lets decay compute an anchor date for states
    that don't have a real start_date (beliefs, preferences).
    """
    if not attributes:
        return None
    if isinstance(attributes, dict):
        val = attributes.get("first_observed")
    elif isinstance(attributes, str):
        import json
        try:
            val = json.loads(attributes).get("first_observed")
        except Exception:
            return None
    else:
        return None
    if not val:
        return None
    try:
        if isinstance(val, str):
            return _to_naive_utc(parse_iso_utc(val))
        return _to_naive_utc(val)
    except Exception:
        return None


def _extract_last_observed(attributes) -> Optional[datetime]:
    """Pull the 'last_observed' timestamp from node attributes, if present.

    Bumped by ``proposal_promoter._refresh_on_reobservation`` each time a
    new proposal matches this node. Decay uses it as the authoritative
    "has this been re-observed recently?" signal — the noisier
    ``updated_at`` column was tried as a fallback in an earlier version
    but had to be removed (every pagerank/description maintenance write
    bumped updated_at, masking real silence).
    """
    if not attributes:
        return None
    if isinstance(attributes, dict):
        val = attributes.get("last_observed")
    elif isinstance(attributes, str):
        import json
        try:
            val = json.loads(attributes).get("last_observed")
        except Exception:
            return None
    else:
        return None
    if not val:
        return None
    try:
        if isinstance(val, str):
            return _to_naive_utc(parse_iso_utc(val))
        return _to_naive_utc(val)
    except Exception:
        return None


def _extract_ttl(attributes) -> Optional[dict]:
    """Pull the TTL sub-dict from a node's attributes column.

    SQLite stores JSON as a string or dict depending on how it was written;
    normalize either way.
    """
    if not attributes:
        return None
    if isinstance(attributes, dict):
        return attributes.get("ttl")
    if isinstance(attributes, str):
        import json
        try:
            return json.loads(attributes).get("ttl")
        except Exception:
            return None
    return None
