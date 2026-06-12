"""Date-gap question drain — curated asks, and answers that LAND.

The missing-dates scanner files state_missing_dates findings; this drain
turns the best of them into user questions and routes the answers back
into the graph:

  1. PROCESS ANSWERS first: captured answers (per-turn check / sweeper /
     ticket) promote the finding to status='investigated' with
     disposition='auto_apply' — the EXISTING kg_finding_executor then
     applies the date through the audited mutator per the year-floor
     doctrine ("around 2003" → floored date + estimated confidence + the
     user's words as prose; a precise stated date → user_set). An answer
     with no usable date instructs the resolution manager to dismiss.
  2. GATE before asking: each candidate passes kg_maintenance::date_gap_gate
     — life-record dates (education, homes, relationships, big purchases)
     get a SPECIFIC crafted question ("When did you graduate from UC
     Berkeley?"); swivel-chair-grade ownership gets the finding rejected
     outright (the scanner's don't-re-raise state). The old template
     question ("when did the 'Ownership' state begin?") is gone.
  3. Question↔finding linkage rides related_concern_id ('finding:' prefix,
     same convention as the wiki fact drain); ownership of answers follows
     the drain-your-own pattern (created_by filter).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import utc_now

logger = get_logger(__name__)

CREATED_BY = "kg_date_gap_drain"
_FINDING_REF_PREFIX = "finding:"


def _run_gate(agent_input: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from app.assistant.routine_handlers.subconscious import _run_subconscious_agent

    return _run_subconscious_agent(
        handler_label="date_gap_gate",
        agent_name="kg_maintenance::date_gap_gate",
        context=agent_input,
        scope_id="kg_maintenance::date_gap_gate",
        actor_id=CREATED_BY,
    )


def _finding_id_from_question(q) -> Optional[str]:
    ref = str(q.related_concern_id or "")
    if ref.startswith(_FINDING_REF_PREFIX):
        return ref[len(_FINDING_REF_PREFIX):]
    return None


def process_date_gap_answers() -> Dict[str, Any]:
    """Promote answered date-gap questions into executor-ready findings."""
    from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
    from app.assistant.pending_questions import close_question, get_by_creator
    from app.models.db_manager import get_db_manager

    answered = get_by_creator(created_by=CREATED_BY, status="answered")
    promoted = 0
    for q in answered:
        finding_id = _finding_id_from_question(q)
        if not finding_id:
            # Legacy question from before the linkage existed — nothing to
            # route to; retire it visibly.
            close_question(q.id, outcome="closed", notes="no finding linkage (pre-curation question)")
            continue

        with get_db_manager().transaction(op="kg_date_gap_drain.answer") as session:
            f = session.query(KGMaintenanceFinding).filter_by(id=finding_id).first()
            if f is None:
                close_question(q.id, outcome="closed", notes="finding vanished")
                continue
            label = (dict(f.evidence_json or {})).get("label") or "the state"
            answer = (q.answer_text or "").strip()
            recommendation = (
                f"USER ANSWERED a date-gap question about node `{f.primary_node_id}` "
                f"({label!r}). Question asked: \"{q.question_text}\". The user's answer "
                f"({str(q.answered_at)[:10]}): \"{answer[:300]}\".\n"
                f"If the answer contains a usable date, era, or relational anchor: apply "
                f"start_date (and end_date if mentioned) via kg_update_node_field, per the "
                f"year-floor doctrine — a precise calendar date the user stated gets "
                f"confidence user_set; a loose answer ('around 2003', 'since birth') gets "
                f"the floored date with confidence estimated and the user's words in "
                f"start_date_prose. Cross-check dated siblings before anchoring. "
                f"If the answer contains NO usable anchor (declined, doesn't remember), "
                f"resolve this finding as dismissed via kg_finding_resolve instead — do "
                f"not invent a date."
            )
            f.status = "investigated"
            f.investigated_at = utc_now()
            f.investigation_report_json = {
                "recommendation": recommendation,
                "diagnosis": "Date gap answered by the user via the question loop.",
                "evidence": [{"kind": "user_answer", "text": answer[:400]}],
                "confidence": "high",
                "disposition": "auto_apply",
                "question_id": q.id,
            }
        close_question(q.id, outcome="closed", notes="answer routed to executor")
        promoted += 1
        logger.info("[date_gap_drain] answer promoted finding %s", finding_id[:8])

    return {"answers_processed": len(answered), "promoted": promoted}


def run_date_gap_drain(*, limit: int = 3, cooldown_days: int = 7) -> Dict[str, Any]:
    """One drain pass: route answers, then gate + ask new questions."""
    from sqlalchemy import case

    from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
    from app.assistant.pending_questions.store import enqueue_question
    from app.models.db_manager import get_db_manager

    answers = process_date_gap_answers()

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, cooldown_days))
    asked = 0
    gated_out = 0
    skipped_in_cooldown = 0
    question_payloads: List[Dict[str, Any]] = []

    with get_db_manager().transaction(op="kg_date_gap_drain") as session:
        rows = (
            session.query(KGMaintenanceFinding)
            .filter(KGMaintenanceFinding.finding_type == "state_missing_dates")
            .filter(KGMaintenanceFinding.status == "pending")
            .order_by(
                case(
                    {"high": 1, "medium": 2, "low": 3},
                    value=KGMaintenanceFinding.priority,
                    else_=4,
                ),
                KGMaintenanceFinding.created_at.asc(),
            )
            .limit(max(1, limit) * 5)  # buffer for cooldown + gate attrition
            .all()
        )
        for f in rows:
            if asked >= max(1, limit):
                break
            ev = dict(f.evidence_json or {})
            last_asked_raw = ev.get("last_asked_at")
            if last_asked_raw:
                try:
                    last_asked = datetime.fromisoformat(str(last_asked_raw).replace("Z", "+00:00"))
                    if last_asked.tzinfo is None:
                        last_asked = last_asked.replace(tzinfo=timezone.utc)
                    if last_asked > cutoff:
                        skipped_in_cooldown += 1
                        continue
                except ValueError:
                    pass

            verdict = _run_gate({
                "label": ev.get("label") or f.reason or "",
                "node_type": ev.get("node_type") or "",
                "category": ev.get("category") or "",
                "description": ev.get("description") or "",
                "entities": (ev.get("connected_entity_labels") or [])[:8],
                "worth": ev.get("worth"),
            })
            if not isinstance(verdict, dict):
                continue
            if not verdict.get("worthy"):
                f.status = "rejected"
                f.execution_notes = (
                    f"date_gap_gate: {str(verdict.get('skip_reason') or 'not worth a user question')[:300]}"
                )
                gated_out += 1
                continue
            question_text = str(verdict.get("question_text") or "").strip()
            if not question_text:
                logger.warning("[date_gap_drain] worthy verdict without question for %s", f.id[:8])
                continue

            ev["last_asked_at"] = datetime.now(timezone.utc).isoformat()
            ev["asked_count"] = int(ev.get("asked_count", 0)) + 1
            f.evidence_json = ev
            question_payloads.append({
                "text": f"{question_text} (a loose answer like 'around 2003' is plenty)",
                "priority": f.priority or "medium",
                "finding_id": f.id,
            })
            asked += 1

    # Deliver AFTER the write transaction (enqueue opens its own session;
    # SQLite is single-writer).
    questions_enqueued = 0
    for p in question_payloads:
        if enqueue_question(
            question_text=p["text"],
            topical_tag="kg_date_gap",
            priority=p["priority"],
            created_by=CREATED_BY,
            related_concern_id=f"{_FINDING_REF_PREFIX}{p['finding_id']}",
            ask_mode="chat",
        ):
            questions_enqueued += 1

    summary = {
        **answers,
        "questions_enqueued": questions_enqueued,
        "gated_out": gated_out,
        "skipped_in_cooldown": skipped_in_cooldown,
    }
    logger.info("[date_gap_drain] pass complete: %s", summary)
    return summary
