"""The synthetic-fact drain — completes the wiki's learning cycle.

wiki_connection_investigator reads fresh wiki pages and files facts the
prose implies but the KG doesn't hold, as `synthetic_fact_proposal`
findings. Those used to dead-end in a human-review queue with no review
surface. This drain closes the cycle:

  1. GATE+ASK (daily, budgeted): each pending proposal goes through
     wiki::fact_question_writer — taxonomy trivia gets dismissed outright;
     worthy facts become a natural confirmation question in the pending
     question queue ("Am I right that Isa had a company in Irvine?"),
     delivered and answer-captured by the standard question loop.
  2. PROCESS ANSWERS: captured answers go through wiki::fact_answer_judge.
     confirmed/corrected → the finding becomes status='investigated' with
     a recommendation + disposition='auto_apply', so the EXISTING
     kg_finding_executor drain materializes the fact through the audited
     mutator suite after its 24h grace (full dedup + revision-log
     safeguards; no new ingestion machinery). denied → dismissed.
     unclear → parked (never re-asked; visible in execution_notes).
  3. EXPIRE: drain questions unanswered past their window close out and
     park their finding — the user saw the question once; don't nag.

Ownership: the drain processes only its OWN questions
(created_by='wiki::synthetic_fact_drain'); the noticer's mailbox is
filtered to its own. Question↔finding linkage rides related_concern_id
with a 'finding:' prefix.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import utc_now

logger = get_logger(__name__)

CREATED_BY = "wiki::synthetic_fact_drain"
_FINDING_REF_PREFIX = "finding:"

# Confirmation questions per drain run — they share the user's daily
# question budget with the noticer, so stay modest.
MAX_QUESTIONS_PER_RUN = 2
# Questions older than this with no answer close out (the question text
# carries its own "if no reply" default = leave the graph unchanged).
QUESTION_EXPIRY_HOURS = 96.0


def _run_agent(agent_name: str, agent_input: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from app.assistant.routine_handlers.subconscious import _run_subconscious_agent

    return _run_subconscious_agent(
        handler_label=agent_name,
        agent_name=agent_name,
        context=agent_input,
        scope_id=f"wiki::{agent_name.split('::')[-1]}",
        actor_id=CREATED_BY,
    )


def _pending_proposals(limit: int) -> List[Dict[str, Any]]:
    """Pending synthetic_fact_proposal findings not yet drained (no
    drain_stage marker), highest confidence first."""
    from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
    from app.models.db_manager import get_db_manager

    out: List[Dict[str, Any]] = []
    with get_db_manager().read_session() as session:
        rows = (
            session.query(KGMaintenanceFinding)
            .filter(KGMaintenanceFinding.finding_type == "synthetic_fact_proposal")
            .filter(KGMaintenanceFinding.status == "pending")
            .order_by(KGMaintenanceFinding.confidence.desc().nullslast(),
                      KGMaintenanceFinding.created_at.asc())
            .limit(limit * 5)
            .all()
        )
        for f in rows:
            report = f.investigation_report_json
            if isinstance(report, str):
                try:
                    report = json.loads(report)
                except ValueError:
                    report = {}
            if isinstance(report, dict) and report.get("drain_stage"):
                continue  # already asked / parked
            evidence = f.evidence_json if isinstance(f.evidence_json, dict) else {}
            out.append({
                "finding_id": f.id,
                "reason": str(f.reason or ""),
                "confidence": f.confidence,
                "primary_node_id": f.primary_node_id,
                "evidence": evidence,
            })
            if len(out) >= limit:
                break
    return out


def _stamp_finding(
    finding_id: str,
    *,
    status: Optional[str] = None,
    report: Optional[Dict[str, Any]] = None,
    execution_notes: Optional[str] = None,
    investigated: bool = False,
) -> bool:
    from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
    from app.models.db_manager import get_db_manager

    with get_db_manager().transaction(op="wiki.synthetic_fact_drain") as session:
        f = session.query(KGMaintenanceFinding).filter_by(id=finding_id).first()
        if f is None:
            logger.warning("[fact_drain] finding %s vanished", finding_id[:8])
            return False
        if status:
            f.status = status
        if report is not None:
            f.investigation_report_json = report
        if execution_notes:
            f.execution_notes = execution_notes[:1000]
        if investigated:
            f.investigated_at = utc_now()
    return True


def _claim_from_reason(reason: str) -> str:
    """The investigator's reason format: \"Inferred from wiki page on 'X': <claim> (conf=N)\".
    Extract the claim; degrade to the whole reason when the format shifts."""
    text = reason.strip()
    if ": " in text:
        text = text.split(": ", 1)[1]
    if text.endswith(")") and "(conf=" in text:
        text = text[: text.rfind("(conf=")].strip()
    return text or reason


def _source_page_from_reason(reason: str) -> str:
    if "wiki page on '" in reason:
        after = reason.split("wiki page on '", 1)[1]
        return after.split("'", 1)[0]
    return ""


def drain_pending_proposals(*, max_questions: int = MAX_QUESTIONS_PER_RUN) -> Dict[str, Any]:
    """Gate pending proposals; dismiss trivia, turn worthy ones into
    confirmation questions."""
    from app.assistant.pending_questions import enqueue_question

    proposals = _pending_proposals(limit=max_questions * 3)
    asked = 0
    dismissed = 0
    for p in proposals:
        if asked >= max_questions:
            break
        claim = _claim_from_reason(p["reason"])
        verdict = _run_agent("wiki::fact_question_writer", {
            "claim": claim,
            "source_page": _source_page_from_reason(p["reason"]),
            "confidence": p["confidence"],
            "subject_label": "",
            "subject_type": "",
        })
        if not isinstance(verdict, dict):
            continue

        if not verdict.get("worthy"):
            reason = str(verdict.get("skip_reason") or "below the worthiness bar")
            _stamp_finding(
                p["finding_id"],
                status="dismissed",
                execution_notes=f"fact_drain trivia gate: {reason}",
            )
            dismissed += 1
            logger.info("[fact_drain] dismissed %s: %s", p["finding_id"][:8], reason)
            continue

        question_text = str(verdict.get("question_text") or "").strip()
        if_unanswered = str(verdict.get("if_unanswered") or "").strip()
        if not question_text:
            logger.warning("[fact_drain] worthy verdict without question for %s", p["finding_id"][:8])
            continue
        if if_unanswered:
            question_text = f"{question_text} (if no reply, I'll go with: {if_unanswered})"

        qid = enqueue_question(
            question_text=question_text,
            topical_tag="wiki",
            priority="medium",
            created_by=CREATED_BY,
            expires_after_hours=QUESTION_EXPIRY_HOURS,
            related_concern_id=f"{_FINDING_REF_PREFIX}{p['finding_id']}",
            ask_mode="chat",
        )
        if not qid:
            continue
        _stamp_finding(
            p["finding_id"],
            report={"drain_stage": "question_asked", "question_id": qid, "claim": claim},
        )
        asked += 1
        logger.info("[fact_drain] asked about %s (q=%s)", p["finding_id"][:8], qid[:8])

    return {"proposals_seen": len(proposals), "questions_asked": asked, "trivia_dismissed": dismissed}


def _finding_id_from_question(q) -> Optional[str]:
    ref = str(q.related_concern_id or "")
    if ref.startswith(_FINDING_REF_PREFIX):
        return ref[len(_FINDING_REF_PREFIX):]
    return None


def process_drain_answers() -> Dict[str, Any]:
    """Judge captured answers to drain questions and move their findings."""
    from app.assistant.pending_questions import close_question, get_by_creator

    answered = get_by_creator(created_by=CREATED_BY, status="answered")
    confirmed = denied = unclear = 0
    for q in answered:
        finding_id = _finding_id_from_question(q)
        if not finding_id:
            close_question(q.id, outcome="closed", notes="no finding linkage")
            continue

        verdict = _run_agent("wiki::fact_answer_judge", {
            "claim": q.question_text,
            "question_text": q.question_text,
            "answer_text": q.answer_text or "",
        })
        if not isinstance(verdict, dict):
            continue
        v = str(verdict.get("verdict") or "unclear")

        if v in ("confirmed", "corrected"):
            claim = str(verdict.get("corrected_claim") or "").strip() if v == "corrected" else ""
            if not claim:
                # The question embedded the claim; recover the original from
                # the drain stamp when available.
                claim = _drain_claim_for_finding(finding_id) or q.question_text
            recommendation = (
                f"USER-CONFIRMED wiki inference. Materialize this fact in the KG: \"{claim}\". "
                f"The user confirmed it on {str(q.answered_at)[:10]} (their words: "
                f"\"{(q.answer_text or '')[:200]}\"). First use kg_query to check whether "
                f"equivalent structure already exists (resolve as already-present if so). "
                f"Otherwise create it with the narrowest correct structure "
                f"(kg_create_state_node / kg_create_edge), original_sentence set to the "
                f"claim, and confidence reflecting direct user confirmation."
            )
            _stamp_finding(
                finding_id,
                status="investigated",
                investigated=True,
                report={
                    "recommendation": recommendation,
                    "diagnosis": "Wiki-inferred fact confirmed by the user via the question loop.",
                    "evidence": [{"kind": "user_answer", "text": (q.answer_text or '')[:400]}],
                    "confidence": "high",
                    "disposition": "auto_apply",
                    "drain_stage": "confirmed",
                    "question_id": q.id,
                },
            )
            close_question(q.id, outcome="closed", notes=f"fact {v}; queued for execution")
            confirmed += 1
        elif v == "denied":
            _stamp_finding(
                finding_id,
                status="dismissed",
                execution_notes=f"fact_drain: user denied — {(q.answer_text or '')[:200]}",
            )
            close_question(q.id, outcome="closed", notes="fact denied by user")
            denied += 1
        else:
            _stamp_finding(
                finding_id,
                report={"drain_stage": "unclear_answer", "question_id": q.id},
                execution_notes=f"fact_drain: unclear answer — {(q.answer_text or '')[:200]}",
            )
            close_question(q.id, outcome="closed", notes="answer unclear; fact parked")
            unclear += 1

    return {"answers_processed": len(answered), "confirmed": confirmed,
            "denied": denied, "unclear": unclear}


def _drain_claim_for_finding(finding_id: str) -> Optional[str]:
    from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
    from app.models.db_manager import get_db_manager

    with get_db_manager().read_session() as session:
        f = session.query(KGMaintenanceFinding).filter_by(id=finding_id).first()
        if f is None:
            return None
        report = f.investigation_report_json
        if isinstance(report, str):
            try:
                report = json.loads(report)
            except ValueError:
                return None
        if isinstance(report, dict):
            return str(report.get("claim") or "") or None
    return None


def expire_stale_drain_questions() -> int:
    """Close drain questions that aged out unanswered; park their findings."""
    from datetime import timedelta

    from app.assistant.pending_questions import close_question, get_by_creator

    cutoff = utc_now() - timedelta(hours=QUESTION_EXPIRY_HOURS)
    expired = 0
    for q in get_by_creator(created_by=CREATED_BY, status="asked", limit=50):
        if q.asked_at is None or q.asked_at > cutoff:
            continue
        finding_id = _finding_id_from_question(q)
        if finding_id:
            _stamp_finding(
                finding_id,
                report={"drain_stage": "question_expired", "question_id": q.id},
                execution_notes="fact_drain: question expired unanswered; fact parked",
            )
        close_question(q.id, outcome="expired", notes="no answer within the window")
        expired += 1
    return expired


def run_fact_drain() -> Dict[str, Any]:
    """One full drain pass: process answers first (they free budget),
    then expire stale asks, then gate new proposals."""
    answers = process_drain_answers()
    expired = expire_stale_drain_questions()
    asked = drain_pending_proposals()
    summary = {**answers, "questions_expired": expired, **asked}
    logger.info("[fact_drain] pass complete: %s", summary)
    return summary
