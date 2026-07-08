"""Pod retention — the nightly sweep that keeps the store bounded.

Policy lives on each kind's registry entry (``configs/pod_kinds.json``):

    "retention": {"mode": "keep_forever"}                      # default when omitted
    "retention": {"mode": "keep_days", "days": 90}
    "retention": {"mode": "keep_days", "days": 365, "keep_latest": 8}

``keep_days`` removes pods of that kind older than the cutoff;
``keep_latest`` additionally protects the newest M of the kind regardless
of age (the planner lanes read "the latest plan" — it must survive even
if the lane was off for a season).

Hard guards, independent of policy:
- pods with ``pod_projection`` rows (secret/identity pods) are NEVER swept;
- unknown/missing retention entries mean keep_forever (fail-safe);
- a malformed retention entry fails LOUD (a typo must not silently become
  a delete policy).

Deletes are hard deletes: pods are derived artifacts — chat stays in
unified_log, emails in the event repository, plans supersede daily. Runs
through the single application writer (db_manager), one short transaction
per kind.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _parse_retention(kind: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize one kind's retention declaration. Missing → keep_forever.
    Malformed → ValueError (fail loud; a typo is not a delete policy)."""
    raw = entry.get("retention")
    if raw is None:
        return {"mode": "keep_forever"}
    if not isinstance(raw, dict):
        raise ValueError(f"pod_kinds.json: {kind!r}.retention must be a mapping, got {type(raw).__name__}")
    mode = str(raw.get("mode") or "").strip()
    if mode == "keep_forever":
        return {"mode": "keep_forever"}
    if mode == "keep_days":
        days = raw.get("days")
        if not isinstance(days, int) or days <= 0:
            raise ValueError(f"pod_kinds.json: {kind!r}.retention.days must be a positive integer")
        keep_latest = raw.get("keep_latest", 0)
        if not isinstance(keep_latest, int) or keep_latest < 0:
            raise ValueError(f"pod_kinds.json: {kind!r}.retention.keep_latest must be a non-negative integer")
        return {"mode": "keep_days", "days": days, "keep_latest": keep_latest}
    raise ValueError(f"pod_kinds.json: {kind!r}.retention.mode {mode!r} is not a known mode")


def run_pod_retention_sweep(*, dry_run: bool = False, now_utc: datetime | None = None) -> Dict[str, Any]:
    """One retention pass over every registered kind. Returns a summary
    ``{swept: {kind: n}, protected_by_projection: n, dry_run: bool}``."""
    from sqlalchemy import select

    from app.assistant.pod_store.models import PodProjection, PodRow
    from app.assistant.pod_store.pod_kind_registry import get_kind, known_kinds

    now_utc = now_utc or datetime.now(timezone.utc)
    swept: Dict[str, int] = {}
    protected = 0

    for kind in known_kinds():
        entry = get_kind(kind) or {}
        policy = _parse_retention(kind, entry)
        if policy["mode"] != "keep_days":
            continue
        cutoff = now_utc - timedelta(days=policy["days"])
        keep_latest = policy["keep_latest"]

        from app.models.db_manager import get_db_manager
        with get_db_manager().transaction(op=f"pod_retention.{kind}") as session:
            # Newest keep_latest of the kind survive regardless of age.
            protected_ids: set = set()
            if keep_latest:
                newest = session.execute(
                    select(PodRow.pod_id)
                    .where(PodRow.kind == kind)
                    .order_by(PodRow.created_at.desc())
                    .limit(keep_latest)
                ).scalars().all()
                protected_ids = set(newest)

            candidates = session.execute(
                select(PodRow.pod_id)
                .where(PodRow.kind == kind)
                .where(PodRow.created_at < cutoff)
            ).scalars().all()
            doomed = [pid for pid in candidates if pid not in protected_ids]
            if not doomed:
                continue

            # Secret/identity pods carry projection rows — never swept.
            with_projections = set(session.execute(
                select(PodProjection.pod_id).where(PodProjection.pod_id.in_(doomed))
            ).scalars().all())
            if with_projections:
                protected += len(with_projections)
                doomed = [pid for pid in doomed if pid not in with_projections]
            if not doomed:
                continue

            if dry_run:
                swept[kind] = len(doomed)
                continue
            n = (
                session.query(PodRow)
                .filter(PodRow.pod_id.in_(doomed))
                .delete(synchronize_session=False)
            )
            swept[kind] = int(n)

    total = sum(swept.values())
    logger.info(
        "[pod_retention] %s %d pod(s) across %d kind(s): %s%s",
        "would sweep" if dry_run else "swept",
        total, len(swept), swept,
        f" (protected_by_projection={protected})" if protected else "",
    )
    return {"swept": swept, "total": total, "protected_by_projection": protected, "dry_run": dry_run}
