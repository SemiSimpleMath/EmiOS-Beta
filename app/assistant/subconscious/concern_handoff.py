"""Handing a ready concern to dayflow (2026-09-30, step 6).

Before this, concerns reached the planner (strategic_planner_wo, "the steward") as lines in its
prompt, and a work object was linked to its concern only if the steward remembered to cite
`concern:<id>` in `based_on`. It often did not: 101 of 1,494 work objects cite one, and the
2026-09-20 flea-medication work, made from the owner's email while the flea concern was open, did
not, so its completion never reached the concern.

Now the brief writer decides readiness (subconscious/concern_brief.py) and a concern ready to act
on is handed over as a dayflow intake item, by code:

- The item is written straight into the steward's inbox (state `artifact`, `evaluator_pending`),
  past intake triage, which would otherwise judge again what the brain already judged with the
  active work in view. Its record carries the concern id, the brief (rendered for the planner)
  and the task with its reason.
- The steward must answer every inbox item: turn it into work by citing it, or review it as no
  action or deferred, with a reason (dayflow_orchestrator/intake_review.py). An unanswered item
  stays in the inbox.
- Work made from the item gets the concern in `constraints.concern_refs` from the item's record
  (dayflow_orchestrator/work_intake.py), not from the steward's citation, so its outcome reaches
  the concern and its agents read the brief.
- The steward's answer is journalled on the concern (`record_intake_outcome`).

One handoff per version of the brief: the item id is `concern_handoff:<concern id>:<brief basis>`.
Not `concern:…`: the steward cites the item id in `based_on`, where `concern:` means a concern
reference, and the first handoffs (2026-09-30) put the item id into `concern_refs` that way. A
concern is not handed over while an earlier handoff item is still in the inbox, or while work
citing it is active.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

SOURCE_TYPE = "concern"


def item_id(concern_id: str, brief_basis: str) -> str:
    return f"concern_handoff:{concern_id}:{brief_basis[:12]}"


def _existing_items(concern_id: str) -> List[Dict[str, Any]]:
    """Every handoff item ever written for this concern, as dayflow item metadata."""
    import json
    from app.models.db_manager import get_db_manager
    from app.assistant.dayflow_orchestrator.dayflow_item_writer import DAYFLOW_ITEM_SOURCE, DAYFLOW_ROOM_ID
    with get_db_manager().read_session() as session:
        raw = session.connection().connection.driver_connection
        rows = raw.execute("SELECT metadata_json FROM unified_log_2026 WHERE source=? AND room_id=? AND id LIKE ?",
                           (DAYFLOW_ITEM_SOURCE, DAYFLOW_ROOM_ID, f"concern_handoff:{concern_id}:%")).fetchall()
    return [json.loads(r[0]) if isinstance(r[0], str) else r[0] for r in rows]


def _active_linked_work(concern_id: str) -> List[str]:
    from app.assistant.subconscious import work_links
    return [r["id"] for r in work_links._rows()
            if r["status"] == "active" and work_links._cites(r["constraints"].get("concern_refs") or [], concern_id)]


def _write_item(meta: Dict[str, Any]) -> None:
    from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_items_batch
    write_dayflow_items_batch([meta], caller="brain_handoff")


def _poke_dayflow() -> None:
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import Message
    msg = Message(sender="brain_handoff", receiver=None, data_type="agent_msg", content="concern_handoff")
    msg.event_topic = "concern_handoff"
    DI.event_hub.publish(msg)


def _brief_text(concern: Dict[str, Any], bucket: str) -> str:
    """The brief as the planner reads it: the same template the agents doing the work read."""
    from app.assistant.dayflow_orchestrator.work_context import render_view
    view = {"concern_id": concern["concern_id"], "title": concern.get("title"), "status": bucket,
            "done_when": concern.get("done_when"), "owner_words": (concern.get("owner_request") or {}).get("words"),
            "brief": concern["brief"], "brief_current": True}
    return render_view("concern_brief", concerns=[view]).strip()


def handoff_meta(concern: Dict[str, Any], bucket: str, now_utc: datetime) -> Dict[str, Any]:
    """The intake item for one ready concern."""
    readiness = concern["brief"]["readiness"]
    return {
        "item_id": item_id(concern["concern_id"], concern["brief"]["basis"]),
        "source_type": SOURCE_TYPE, "event_type": "brain_handoff",
        "created_at": now_utc.isoformat(),
        "summary": readiness["task"],
        "why_now": readiness["why"],
        "concern_id": concern["concern_id"], "concern_title": concern.get("title"),
        "brief_text": _brief_text(concern, bucket),
        "state": "artifact", "state_reason": "brain_handoff", "evaluator_pending": True,
        "importance": "high", "actionability": "actionable",
        "last_reviewed_at": now_utc.isoformat(), "cooldown_until": None, "linked_item_ids": [],
    }


def run_handoffs(*, register_connect=None, now_utc: Optional[datetime] = None,
                 existing_items: Callable[[str], List[Dict[str, Any]]] = None,
                 active_work: Callable[[str], List[str]] = None,
                 write_item: Callable[[Dict[str, Any]], None] = None,
                 poke: Callable[[], None] = None) -> Dict[str, Any]:
    """Hand every open concern whose current brief says act_now to dayflow, once per brief version.
    The keyword arguments after `now_utc` are test seams."""
    from app.assistant.subconscious import concern_brief
    from app.assistant.subconscious.concern_store import load_register
    now = now_utc or datetime.now(timezone.utc)
    existing_items = existing_items or _existing_items
    active_work = active_work or _active_linked_work
    write_item = write_item or _write_item
    register = load_register(connect=register_connect)
    handed, skipped = [], []
    for bucket in ("active", "addressing"):
        for concern in register.get(bucket) or []:
            brief = concern.get("brief") or {}
            if (brief.get("readiness") or {}).get("decision") != "act_now":
                continue
            if brief.get("basis") != concern_brief.basis(concern, bucket):
                continue                                   # a newer brief is on its way
            cid = concern["concern_id"]
            items = existing_items(cid)
            this_version = item_id(cid, brief["basis"])
            if any(i.get("item_id") == this_version for i in items):
                continue                                   # this brief was handed over already
            if any(i.get("state") == "artifact" and i.get("evaluator_pending") for i in items):
                skipped.append((cid, "an earlier handoff is still in the planner's inbox"))
                continue
            working = active_work(cid)
            if working:
                skipped.append((cid, f"work citing it is active: {working}"))
                continue
            write_item(handoff_meta(concern, bucket, now))
            handed.append(cid)
    for cid, why in skipped:
        logger.info("[handoff] %s not handed over: %s", cid, why)
    if handed:
        logger.info("[handoff] handed %d concern(s) to dayflow: %s", len(handed), handed)
        (poke or _poke_dayflow)()
    return {"handed_over": len(handed), "handoff_skipped": len(skipped)}


def record_intake_outcome(meta: Dict[str, Any], outcome: str, detail: str, *, connect=None) -> None:
    """Journal the steward's answer to a handoff item on its concern: transferred to work, no action,
    or deferred. Called by the steward's persist node after its writes are committed."""
    from app.assistant.subconscious import persist
    persist.journal_concern(meta["concern_id"],
                            f"HANDOFF {meta['item_id']}: the planner {outcome}: {detail}", connect=connect)
