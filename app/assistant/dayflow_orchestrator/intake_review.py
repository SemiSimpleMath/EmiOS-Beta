"""Reading the steward's stored intake reviews (`evaluator_review` on a dayflow item).

The steward (strategic_planner_wo, retired 2026-10-10) reviewed every intake item it did not turn into work as no
action, or deferred to a time, with a reason. The reviews stay on the items: an item reviewed as no action is not a
fresh event that can wake waiting work (state_mover_prep_node).
"""
from datetime import datetime, timezone


def review_due(meta, now_utc=None):
    """Malformed stored deferrals remain visible instead of silently disappearing."""
    review = meta.get("evaluator_review") or {}
    if not isinstance(review, dict) or review.get("outcome") != "defer":
        return True
    try:
        when = datetime.fromisoformat(review["reconsider_at"].replace("Z", "+00:00"))
        if when.tzinfo is None:
            return True
        return when <= (now_utc or datetime.now(timezone.utc))
    except (KeyError, TypeError, ValueError, AttributeError):
        return True


def wake_intake_eligible(meta, now_utc=None):
    """Retired/held intake is not a fresh event; transferred sources can still wake work."""
    review = meta.get("evaluator_review") or {}
    return ((not isinstance(review, dict) or review.get("outcome") != "no_action")
            and review_due(meta, now_utc))

