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
- The steward's answer is journalled on the concern (`record_intake_outcome`).

A standing concern (a recurring part of the day, from the owner's beliefs; subconscious/day_pass.py) is
handed over per occurrence instead: when its brief says act_now, each of today's occurrences goes over once,
with its time, as soon as the day pass places it (owner, 2026-10-02: concerns are handed over when they
are created, unless they are for another day), and the work made from it waits for that time. The item id
is `concern_handoff:<concern id>:<day>:<time>`; an occurrence more than an hour past is not handed over.

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
    """The brief, the things that should be done, and the work on the concern so far, as the planner reads
    them: the same template the agents doing the work read."""
    from app.assistant.dayflow_orchestrator.work_context import render_view
    from app.assistant.subconscious import concern_brief
    view = {"concern_id": concern["concern_id"], "title": concern.get("title"), "status": bucket,
            "done_when": concern.get("done_when"), "owner_words": (concern.get("owner_request") or {}).get("words"),
            "brief": concern["brief"], "plan": concern.get("plan"), "attempts": concern_brief.attempts(concern),
            "brief_current": True}
    return render_view("concern_brief", concerns=[view]).strip()


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


def occurrence_item_id(concern_id: str, day: str, time: str) -> str:
    return f"concern_handoff:{concern_id}:{day}:{'-'.join(time.lower().split())}"


def occurrence_meta(concern: Dict[str, Any], bucket: str, day: str, occurrence: Dict[str, Any],
                    now_utc: datetime) -> Dict[str, Any]:
    """The intake item for one occurrence of a standing concern: the brief's task at its time today."""
    readiness = concern["brief"]["readiness"]
    when = occurrence["time"]
    return {**_meta(concern, bucket, now_utc, occurrence_item_id(concern["concern_id"], day, when),
                    summary=f"{'All day' if when == 'all day' else 'At ' + when} today ({day}): {readiness['task']}",
                    why=readiness["why"]),
            "occurrence_day": day, "occurrence_time": when}


def _occurrence_passed(day: str, when: str, now_utc: datetime) -> bool:
    """True when a clock-time occurrence today was more than an hour ago."""
    from app.assistant.subconscious.day_pass import _clock_hour_minute
    from app.assistant.utils.time_utils import get_local_timezone
    if when == "all day":
        return False
    hour, minute = _clock_hour_minute(when)
    at = datetime.fromisoformat(day).replace(hour=hour, minute=minute, tzinfo=get_local_timezone())
    return (now_utc - at.astimezone(timezone.utc)).total_seconds() > 3600


def _standing_handoffs(concern: Dict[str, Any], bucket: str, items: List[Dict[str, Any]],
                       now_utc: datetime) -> List[Dict[str, Any]]:
    from app.assistant.pipelines.context import boundary_date_local_str
    from app.assistant.utils.time_utils import get_local_timezone
    day = boundary_date_local_str(now_utc.astimezone(get_local_timezone()))
    handed = {i.get("item_id") for i in items}
    return [occurrence_meta(concern, bucket, day, occ, now_utc)
            for occ in (concern.get("occurrences") or {}).get(day) or []
            if occurrence_item_id(concern["concern_id"], day, occ["time"]) not in handed
            and not _occurrence_passed(day, occ["time"], now_utc)]


def run_handoffs(*, register_connect=None, now_utc: Optional[datetime] = None,
                 existing_items: Callable[[str], List[Dict[str, Any]]] = None,
                 write_item: Callable[[Dict[str, Any]], None] = None,
                 poke: Callable[[], None] = None) -> Dict[str, Any]:
    """Hand every open matter concern whose current plan says act_now to dayflow, once per plan version,
    and each of today's occurrences of every standing concern whose brief says act_now. The keyword
    arguments after `now_utc` are test seams."""
    from app.assistant.subconscious import concern_brief
    from app.assistant.subconscious.concern_store import load_register
    now = now_utc or datetime.now(timezone.utc)
    existing_items = existing_items or _existing_items
    write_item = write_item or _write_item
    register = load_register(connect=register_connect)
    handed, skipped = [], []
    for bucket in ("active", "addressing"):
        for concern in register.get(bucket) or []:
            brief = concern.get("brief") or {}
            if brief.get("basis") != concern_brief.basis(concern, bucket):
                continue                                   # a newer brief is on its way
            cid = concern["concern_id"]
            if concern.get("kind") == "standing":
                if (brief.get("readiness") or {}).get("decision") != "act_now":
                    continue
                for meta in _standing_handoffs(concern, bucket, existing_items(cid), now):
                    write_item(meta)
                    handed.append(cid)
                continue
            plan = concern.get("plan") or {}
            if plan.get("decision") != "act_now" or plan.get("basis") != brief["basis"]:
                continue                                   # no plan from this brief yet, or nothing to do
            items = existing_items(cid)
            if any(i.get("item_id") == item_id(cid, plan["basis"]) for i in items):
                continue                                   # this plan was handed over already
            if any(i.get("state") == "artifact" and i.get("evaluator_pending") for i in items):
                skipped.append((cid, "an earlier handoff is still in the planner's inbox"))
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
