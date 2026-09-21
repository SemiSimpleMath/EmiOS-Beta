"""Source-independent evaluator dispositions; no source content is deleted."""
from datetime import datetime, timezone

from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.dayflow_orchestrator.work_intake import source_records


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


def prepare_reviews(items, work_specs, reviews, *, now_utc=None):
    """Validate complete coverage before any work mutation; resolve only presented IDs."""
    now = now_utc or datetime.now(timezone.utc)
    aliases, ids = {}, set()
    for item in items:
        meta = get_meta(item)
        item_id = str(meta.get("item_id") or item.get("id") or "")
        ids.add(item_id)
        for alias in (item_id, str(meta.get("short_id") or "")):
            if alias:
                if alias in aliases and aliases[alias] != item_id:
                    raise ValueError(f"Ambiguous intake id: {alias}")
                aliases[alias] = item_id
    transferred = set()
    for spec in work_specs:
        sources = source_records(items, spec.get("based_on") or [])
        if sources and not str(spec.get("objective") or "").strip():
            raise ValueError("Intake transfer requires a nonempty work objective")
        transferred.update(source["item_id"] for source in sources)
    seen, prepared = set(), []
    for raw in reviews:
        item_id = aliases.get(str(raw.get("item_id") or "").strip())
        if item_id is None or item_id in seen or item_id in transferred:
            raise ValueError("Unknown, duplicate, or conflicting intake review")
        outcome = raw.get("outcome")
        reason = str(raw.get("reason") or "").strip()
        reconsider = str(raw.get("reconsider_at") or "").strip()
        if outcome not in {"no_action", "defer"} or not reason:
            raise ValueError("Intake review requires a valid outcome and reason")
        if outcome == "defer":
            when = datetime.fromisoformat(reconsider.replace("Z", "+00:00"))
            if when.tzinfo is None or when <= now:
                raise ValueError("Intake deferral requires a future timestamp with timezone")
            reconsider = when.astimezone(timezone.utc).isoformat()
        elif reconsider:
            raise ValueError("No-action review cannot have a reconsideration time")
        prepared.append({"item_id": item_id, "outcome": outcome, "reason": reason,
                         "reconsider_at": reconsider})
        seen.add(item_id)
    missing = ids - transferred - seen
    if missing:
        raise ValueError(f"Intake lacks an explicit disposition: {sorted(missing)}")
    return prepared
