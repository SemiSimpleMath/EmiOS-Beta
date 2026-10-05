"""KG failure ledger: a failed call stops the run; an item that keeps failing is set aside for review (2026-10-05).

Owner, 2026-10-05: "If any call fails in KG, we should treat that as a stop processing." Every KG queue (windows,
proposals, findings, cards, nodes to rate or tag) stops its run at the first failed LLM, embedding, Chroma or DB call
and leaves the rest pending for the next run. A stop alone would let one item that always fails (a content refusal,
an over-long prompt) block its whole queue every night, so each failure is counted per item: after SET_ASIDE_AFTER
failed runs the item is set aside, its queue skips it, and a `processing_failure` finding, born escalated, puts it in
front of the owner. A set-aside item is never dropped; `release` returns it to its queue.

Usage in a queue loop:

    skip = failure_ledger.set_aside_ids("kg_window")
    for item in pending:                      # excluding `skip`
        try:
            process(item)
        except Exception as exc:
            failure_ledger.record_failure("kg_window", item.id, exc, label=...)
            raise                             # the run stops; the rest stays pending
        failure_ledger.clear("kg_window", item.id)
"""
from __future__ import annotations

from typing import Optional, Set

from sqlalchemy import Column, DateTime, Integer, String, Text

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import AwareUtcDateTime, utc_now
from app.models.base import Base
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)

SET_ASIDE_AFTER = 3


class KGItemFailure(Base):
    __tablename__ = "kg_item_failure"

    item_kind = Column(String(64), primary_key=True)
    item_id = Column(String, primary_key=True)
    label = Column(Text, nullable=True)
    failures = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)
    first_failed_at = Column(AwareUtcDateTime, nullable=False, default=utc_now)
    last_failed_at = Column(AwareUtcDateTime, nullable=False, default=utc_now)
    set_aside_at = Column(AwareUtcDateTime, nullable=True)
    finding_id = Column(String, nullable=True)


def _ensure(session) -> None:
    Base.metadata.create_all(session.connection(), tables=[KGItemFailure.__table__], checkfirst=True)


def set_aside_ids(kind: str) -> Set[str]:
    """Ids of this kind's set-aside items: their queue skips them until released."""
    with get_db_manager().transaction(op="kg_failure_ledger.read") as s:
        _ensure(s)
        return {r[0] for r in s.query(KGItemFailure.item_id)
                .filter(KGItemFailure.item_kind == kind, KGItemFailure.set_aside_at.isnot(None)).all()}


def record_failure(kind: str, item_id: str, error: BaseException, *, label: Optional[str] = None) -> bool:
    """Count one failed run for this item. Returns True when this failure set it aside (and raised its finding)."""
    item_id = str(item_id)
    message = f"{type(error).__name__}: {error}"
    with get_db_manager().transaction(op="kg_failure_ledger.record") as s:
        _ensure(s)
        row = s.get(KGItemFailure, (kind, item_id))
        if row is None:
            row = KGItemFailure(item_kind=kind, item_id=item_id, failures=0)
            s.add(row)
        row.failures = (row.failures or 0) + 1
        row.last_error = message
        row.last_failed_at = utc_now()
        if label:
            row.label = label
        newly = row.set_aside_at is None and row.failures >= SET_ASIDE_AFTER
        if newly:
            row.set_aside_at = utc_now()
        failures = row.failures
    logger.error("[kg_failure_ledger] %s %s failed (%d run(s)): %s", kind, item_id, failures, message)
    if newly:
        from app.assistant.kg_maintenance.store import upsert_finding
        finding_id, _ = upsert_finding(
            finding_type="processing_failure",
            primary_node_id=item_id,
            suggested_action="review",
            reason=(f"The {kind} item {label or item_id!r} failed in {failures} runs and is set aside; its queue "
                    f"skips it until it is released. Last error: {message}"),
            priority="medium",
            agent_name="kg_failure_ledger",
            evidence={"item_kind": kind, "item_id": item_id, "label": label, "failures": failures,
                      "last_error": message},
            initial_status="escalated",
        )
        with get_db_manager().transaction(op="kg_failure_ledger.finding") as s:
            s.get(KGItemFailure, (kind, item_id)).finding_id = finding_id
        logger.error("[kg_failure_ledger] %s %s set aside after %d failed runs (finding %s)",
                     kind, item_id, failures, finding_id)
    return newly


def clear(kind: str, item_id: str) -> None:
    """The item succeeded: forget its failures."""
    with get_db_manager().transaction(op="kg_failure_ledger.clear") as s:
        _ensure(s)
        s.query(KGItemFailure).filter(KGItemFailure.item_kind == kind,
                                      KGItemFailure.item_id == str(item_id)).delete()


def release(kind: str, item_id: str) -> None:
    """Return a set-aside item to its queue (the owner reviewed it)."""
    clear(kind, item_id)
