"""Handing a ready concern to dayflow (2026-09-30, step 6).

Before this, concerns reached the planner (strategic_planner_wo, "the steward") as lines in its
prompt, and a work object was linked to its concern only if the steward remembered to cite
`concern:<id>` in `based_on`. It often did not: 101 of 1,494 work objects cite one, and the
2026-09-20 flea-medication work, made from the owner's email while the flea concern was open, did
not, so its completion never reached the concern.

A concern ready to act on is handed over as a dayflow intake item, by code. For a matter concern the
concern planner decides (subconscious/concern_planner.py): when its plan says act_now, the concern goes
over with the plan's bullets, the things that should be done, for the steward to interpret. Until
2026-10-02 the brief's readiness decided, and a concern was not handed over while work citing it was
active; the planner sees that work and decides act_now only when it should be added to or corrected
(the book club moved: tell the caterer, the guests), so active work no longer holds a handoff back.

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
- Until 2026-10-09 the steward's answer was journalled on the concern; only the brain writes its notes now.

Today's calendar is not handed over through concerns: each placement goes to dayflow from the calendar
(subconscious/belief_occurrences.py). Until 2026-10-03 a placed belief got a standing concern whose
occurrences were handed over here, with the concern's single rule as their task.

When the concern lane is live (configs/brain_lanes.json), nothing is handed to the steward: concern_tasks keeps
the concern's entries on the brain's list and the list-item intake node makes their work
(dayflow_orchestrator/brain_list_intake.py).

One handoff per version of the plan (one per brief version): the item id is
`concern_handoff:<concern id>:<brief basis>`. Not `concern:…`: the steward cites the item id in
`based_on`, where `concern:` means a concern reference, and the first handoffs (2026-09-30) put the item
id into `concern_refs` that way. A concern is not handed over while an earlier handoff item is still in
the inbox.
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
    """Every handoff item ever written for this concern, as dayflow item metadata. Found by the
    concern id in the item's own record, so the first two handoffs, named `concern:…` before the
    rename, are found too (an item id is an identity field and is not rewritten)."""
    import json
    from app.models.db_manager import get_db_manager
    from app.assistant.dayflow_orchestrator.dayflow_item_writer import DAYFLOW_ITEM_SOURCE, DAYFLOW_ROOM_ID
    with get_db_manager().read_session() as session:
        raw = session.connection().connection.driver_connection
        rows = raw.execute("SELECT metadata_json FROM unified_log_2026 WHERE source=? AND room_id=? "
                           "AND json_extract(metadata_json, '$.source_type') = ? "
                           "AND json_extract(metadata_json, '$.concern_id') = ?",
                           (DAYFLOW_ITEM_SOURCE, DAYFLOW_ROOM_ID, SOURCE_TYPE, concern_id)).fetchall()
    return [json.loads(r[0]) if isinstance(r[0], str) else r[0] for r in rows]


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
    """The concern's record, the things that should be done, and the work on it so far, as the planner reads
    them: the same template the agents doing the work read."""
    from app.assistant.dayflow_orchestrator.work_context import render_view
    from app.assistant.subconscious.concern_view import view
    return render_view("concern_view", concerns=[view(concern, bucket)]).strip()


def handoff_meta(concern: Dict[str, Any], bucket: str, now_utc: datetime) -> Dict[str, Any]:
    """The intake item for one matter concern whose plan says act_now: its things to do and why."""
    plan = concern["plan"]
    return _meta(concern, bucket, now_utc, item_id(concern["concern_id"], plan["basis"]),
                 summary="; ".join(plan["todo"]), why=plan["why"])


def _meta(concern: Dict[str, Any], bucket: str, now_utc: datetime, item: str, *, summary: str, why: str) -> Dict[str, Any]:
    return {
        "item_id": item,
        "source_type": SOURCE_TYPE, "event_type": "brain_handoff",
        "created_at": now_utc.isoformat(),
        "summary": summary,
        "why_now": why,
        "concern_id": concern["concern_id"], "concern_title": concern.get("title"),
        "brief_text": _brief_text(concern, bucket),
        "state": "artifact", "state_reason": "brain_handoff", "evaluator_pending": True,
        "importance": "high", "actionability": "actionable",
        "last_reviewed_at": now_utc.isoformat(), "cooldown_until": None, "linked_item_ids": [],
    }


def run_handoffs(*, register_connect=None, now_utc: Optional[datetime] = None,
                 existing_items: Callable[[str], List[Dict[str, Any]]] = None,
                 write_item: Callable[[Dict[str, Any]], None] = None,
                 poke: Callable[[], None] = None) -> Dict[str, Any]:
    """Hand every open concern whose current plan says act_now to dayflow, once per plan version. The
    keyword arguments after `now_utc` are test seams."""
    from app.assistant.dayflow_orchestrator.brain_list_intake import concern_live
    if concern_live():
        return {"handed_over": 0, "handoff_skipped": 0, "handoffs": "the brain's list"}
    from app.assistant.subconscious.concern_view import basis
    from app.assistant.subconscious.brain_trace import step
    from app.assistant.subconscious.concern_store import load_register
    now = now_utc or datetime.now(timezone.utc)
    existing_items = existing_items or _existing_items
    write = write_item or _write_item

    def write_item(meta: Dict[str, Any]) -> None:
        write(meta)
        step("handoff", "handed", subject=meta["concern_id"], item_id=meta["item_id"],
             title=meta["concern_title"], summary=meta["summary"], why=meta["why_now"])

    register = load_register(connect=register_connect)
    handed, skipped = [], []
    for bucket in ("active", "addressing"):
        for concern in register.get(bucket) or []:
            cid = concern["concern_id"]
            plan = concern.get("plan") or {}
            if plan.get("decision") != "act_now" or plan.get("basis") != basis(concern, bucket):
                continue                                   # no plan from this record yet, or nothing to do
            items = existing_items(cid)
            if any(i.get("item_id") == item_id(cid, plan["basis"]) for i in items):
                continue                                   # this plan was handed over already
            if any(i.get("state") == "artifact" and i.get("evaluator_pending") for i in items):
                skipped.append((cid, "an earlier handoff is still in the planner's inbox"))
                step("handoff", "held back", subject=cid, title=concern.get("title"),
                     reason="an earlier handoff is still in the planner's inbox")
                continue
            write_item(handoff_meta(concern, bucket, now))
            handed.append(cid)
    for cid, why in skipped:
        logger.info("[handoff] %s not handed over: %s", cid, why)
    if handed:
        logger.info("[handoff] handed %d concern(s) to dayflow: %s", len(handed), handed)
        (poke or _poke_dayflow)()
    return {"handed_over": len(handed), "handoff_skipped": len(skipped)}
