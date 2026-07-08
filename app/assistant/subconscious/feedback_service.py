"""Feedback service — user-authored comments on subconscious suggestions.

Unified surface across /meals, /subconscious, and any future per-domain
page. User writes a comment attached to a target pod (intention.*,
plan.weekly_schedule, etc.); the comment becomes a `feedback.comment`
pod the downstream feedback_extractor processes hourly to update beliefs.

This module is the data layer. Routes call into it; an ingestion step
(separate, lands in a follow-up commit) reads unprocessed feedback
pods to emit belief_updates.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.assistant.pod_store.contracts import Pod
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


# Pod kinds the /subconscious dashboard surfaces for comment.
# Adding a future domain (intention.finance, etc.) just means appending here.
DASHBOARD_INTENTION_KINDS = [
    "intention.meal",
    "intention.wellness",
    "intention.romantic",
    "intention.shopping",
]


def mint_feedback_comment(
    *,
    target_pod_id: str,
    text: str,
    target_scope: Optional[str] = None,
    actor: str = "user",
) -> Optional[str]:
    """Mint a feedback.comment pod attached to a target pod.

    `target_scope` is an optional sub-target within the target pod —
    e.g., for a comment on a specific day of the plan.weekly_meals pod,
    target_scope="2026-05-20". When the comment applies to the whole pod,
    leave it None.

    Returns the new pod_id, or None on failure (logged).
    """
    text_stripped = (text or "").strip()
    if not text_stripped:
        return None
    target = (target_pod_id or "").strip()
    if not target:
        return None
    try:
        now_utc_iso = datetime.now(timezone.utc).isoformat()
        pod_id = f"datapod:feedback.comment:{uuid.uuid4().hex[:24]}"
        snippet = text_stripped[:140]
        body_parts = [
            f"# Feedback comment — {now_utc_iso}",
            "",
            f"**Target pod:** {target}",
        ]
        if target_scope:
            body_parts.append(f"**Target scope:** {target_scope}")
        body_parts += ["", "**Comment:**", text_stripped]
        pod = Pod(
            pod_id=pod_id,
            kind="feedback.comment",
            tags=["feedback", "user_comment", "unprocessed"],
            one_liner=f"Comment on {target}: {snippet}",
            body="\n".join(body_parts),
            source_refs=[],
            for_agents=["feedback_extractor"],
            scope_id=None,
            created_by=actor,
            metadata={
                "submitted_at_utc": now_utc_iso,
                "target_pod_id": target,
                "target_scope": target_scope,
                "text": text_stripped,
                "processed_at_utc": None,
                "extracted_belief_ids": [],
            },
        )
        PodStore().put(pod)
        return pod_id
    except Exception as e:
        logger.warning("[feedback_service] mint feedback.comment failed: %s", e)
        return None


def fetch_comments_for(target_pod_id: str, *, limit: int = 20) -> List[Dict[str, Any]]:
    """Return recent comments attached to a target pod, newest first.

    Returns plain dicts (text, target_scope, submitted_at_utc, pod_id,
    processed_at_utc) so callers can render without import-graph baggage.
    """
    if not target_pod_id:
        return []
    try:
        store = PodStore()
        # Coarse query; we filter precisely below. since=120d gives 4 months
        # of recent comments to scan — plenty for the dashboard.
        candidates = store.query(kind="feedback.comment", since="120d", limit=200)
    except Exception as e:
        logger.warning("[feedback_service] fetch comments failed: %s", e)
        return []

    out: List[Dict[str, Any]] = []
    for pod in candidates:
        meta = pod.metadata or {}
        if str(meta.get("target_pod_id") or "") != target_pod_id:
            continue
        out.append({
            "pod_id": pod.pod_id,
            "text": meta.get("text") or "",
            "target_scope": meta.get("target_scope"),
            "submitted_at_utc": meta.get("submitted_at_utc") or "",
            "processed_at_utc": meta.get("processed_at_utc"),
        })
        if len(out) >= limit:
            break
    out.sort(key=lambda c: c["submitted_at_utc"], reverse=True)
    return out


def list_intentions_for_dashboard(*, days_ahead: int = 14) -> List[Dict[str, Any]]:
    """Every intention.* pod with a date in [today, today+days_ahead].

    Returns dicts (pod_id, kind, domain, date, summary, actors, source,
    confidence, novelty, comments_count) grouped is the caller's job;
    here we return a flat sorted list by date+domain.
    """
    from datetime import date, timedelta
    today = date.today()
    horizon = today + timedelta(days=days_ahead)

    try:
        store = PodStore()
        pods: List[Any] = []
        for kind in DASHBOARD_INTENTION_KINDS:
            pods.extend(store.query(kind=kind, since="14d", limit=200))
    except Exception as e:
        logger.warning("[feedback_service] dashboard intention fetch failed: %s", e)
        return []

    rows: List[Dict[str, Any]] = []
    for pod in pods:
        meta = pod.metadata or {}
        date_raw = (meta.get("date") or "").strip()
        try:
            d = datetime.strptime(date_raw, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if d < today or d > horizon:
            continue
        domain = pod.kind.split(".", 1)[1] if "." in pod.kind else pod.kind
        rows.append({
            "pod_id": pod.pod_id,
            "kind": pod.kind,
            "domain": domain,
            "subkind": meta.get("kind") or domain,
            "date": d.isoformat(),
            "summary": meta.get("summary") or pod.one_liner or "(no summary)",
            "actors": list(meta.get("actors") or []),
            "source": meta.get("source") or "",
            "confidence": meta.get("confidence") or "",
            "novelty": meta.get("novelty") or "",
            "proposed_start_local": meta.get("proposed_start_local"),
            "duration_minutes": meta.get("duration_minutes"),
            "intensity": meta.get("intensity"),
            "cost_estimate_usd": meta.get("cost_estimate_usd"),
        })

    rows.sort(key=lambda r: (r["date"], r["domain"], r["pod_id"]))

    # Annotate with comment counts (cheap second pass — N small).
    for row in rows:
        row["comments"] = fetch_comments_for(row["pod_id"], limit=5)
        row["comments_count"] = len(row["comments"])
    return rows


def list_recent_unprocessed_comments(*, limit: int = 50) -> List[Dict[str, Any]]:
    """The feedback_extractor's queue — feedback.comment pods that haven't
    been ingested into beliefs yet (processed_at_utc is None).

    Queried by the 'unprocessed' TAG (minted on every comment, flipped to
    'processed' on ingestion), not by a time window: a window silently
    orphaned any comment that stayed unprocessed longer than it (extractor
    disabled by on_error, extended downtime). The metadata check below stays
    authoritative for pods whose tag flip failed."""
    try:
        store = PodStore()
        candidates = store.query(kind="feedback.comment", tags=["unprocessed"], limit=200)
    except Exception as e:
        logger.warning("[feedback_service] unprocessed fetch failed: %s", e)
        return []
    out: List[Dict[str, Any]] = []
    for pod in candidates:
        meta = pod.metadata or {}
        if meta.get("processed_at_utc"):
            continue
        out.append({
            "pod_id": pod.pod_id,
            "target_pod_id": meta.get("target_pod_id"),
            "target_scope": meta.get("target_scope"),
            "text": meta.get("text") or "",
            "submitted_at_utc": meta.get("submitted_at_utc") or "",
        })
        if len(out) >= limit:
            break
    out.sort(key=lambda c: c["submitted_at_utc"])
    return out
