"""Concern feedback — the work attached to a concern reports to it, and to the brain.

WorkStore commits a receipt in the same transaction as each change a concern must hear about
(work_objects/concern_outbox.py): the work was attached (it cites the concern), the finalizer judged
one of its tasks, or the work ended, including automatic rollup. Dayflow delivers after creation,
each judgment and each closure; evaluator prep retries receipts left by a crash or register failure.

Delivery writes the work's record on the concern (persist.attach_work / record_judgment /
apply_work_outcome) and, for a judgment or an ending, a brain inbox event routed to the concerns by
id (owner, 2026-09-30: when work objects are worked on and the finalizer runs, the concerns are
updated). The brain reads it with the concern's record and decides whether the concern is settled.
Each receipt is idempotent in the register and in the inbox. Delivery failures never roll back
work; a receipt remains until every linked concern and the inbox have it.
"""
from __future__ import annotations

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _last_user_reply(wo) -> dict:
    """Newest explicitly attributed reply, ordered by response/evidence time.

    Tool prose is never user text. Historical `created_by=reply` records remain
    readable as unclassified user words; ordinary `ask`/tool authors alone do not
    establish a reply (their records can also contain timeouts and errors).
    """
    from copy import deepcopy
    from datetime import datetime, timezone
    from app.assistant.utils.time_utils import parse_iso_utc
    replies = []
    for node in (getattr(wo, "nodes", {}) or {}).values():
        if getattr(node, "type", "") != "evidence":
            continue
        reply = (getattr(node, "payload", {}) or {}).get("user_reply")
        if isinstance(reply, dict) and (getattr(node, "created_by", "") not in {"create_dayflow_ticket", "ask"}
                                        or not reply.get("ticket_id")):
            continue
        if not isinstance(reply, dict):
            if getattr(node, "created_by", "") != "reply" or not (getattr(node, "content", "") or "").strip():
                continue
            reply = {"user_text": node.content, "provenance": "legacy_reply"}
        details = reply.get("response_details") or {}
        history = details.get("response_history") or []
        latest = history[-1] if history else details
        when = (parse_iso_utc(str(latest.get("responded_at") or reply.get("responded_at") or ""))
                or parse_iso_utc(str(getattr(node, "created_at", "") or ""))
                or datetime.min.replace(tzinfo=timezone.utc))
        replies.append((when, str(getattr(node, "id", "")), reply))
    return deepcopy(max(replies, key=lambda row: row[:2])[2]) if replies else {}


def _reply_line(reply: dict) -> str:
    """One recorded owner reply as the brain reads it: the question, the choice, the words."""
    details = reply.get("response_details") or {}
    history = details.get("response_history") or []
    latest = history[-1] if history else details
    said = str(reply.get("user_text") or latest.get("typed_text") or "").strip()
    choice = str(latest.get("label") or reply.get("action") or "").strip()
    parts = [f'The owner was asked: "{reply.get("question")}"'] if reply.get("question") else []
    if choice:
        parts.append(f"chose: {choice}")
    if said:
        parts.append(f'said: "{said}"')
    return "; ".join(parts) or "The owner replied (no words recorded)."


def _event_text(receipt) -> str:
    """The brain event for a judgment or an ending: what happened, in the finalizer's and the
    owner's own words."""
    payload, kind = receipt['payload'], receipt['outcome']
    if kind == 'judged':
        task, fin = payload['task'], payload['task']['finalizer']
        lines = [f'Work "{payload["work"]["title"]}" ({receipt["work_id"]}): the finalizer judged the task '
                 f'"{task["title"]}": {fin.get("verdict")}, next step {fin.get("next_step") or "none"}.',
                 f"What happened: {fin.get('outcome')}"]
        lines += [_reply_line(r) for r in task.get('replies') or []]
        return "\n".join(lines)
    context = payload['context']
    return (f'Work "{context["title"]}" ({receipt["work_id"]}) ended: {kind}. '
            f'Reason: {(context.get("terminal") or {}).get("reason") or "(none recorded)"}.')


def report_earlier_endings(reopened) -> int:
    """The move to attached work (persist.rederive_attached_work) put concerns whose work had
    already ended back to `active`. Each gets its ending as a brain event, as a live ending would,
    so the brain decides whether the concern is settled. Idempotent per concern and work."""
    from app.assistant.subconscious import brain_inbox, brain_wake
    added = 0
    for r in reopened:
        w = r["work"]
        added += brain_inbox.add_work_event(
            work_id=w["work_id"], receipt_id=f"rederive-{r['concern_id']}", occurred_at=w["ended"]["at"],
            text=(f'Work "{w.get("title")}" ({w["work_id"]}) ended: {w["ended"]["outcome"]}. '
                  f'Reason: {w["ended"].get("reason") or "(none recorded)"}.'),
            concern_ids=[r["concern_id"]])
    if added:
        brain_wake.poke()
    return added


def _deliver_receipt(store, receipt):
    from types import SimpleNamespace
    from app.assistant.subconscious import brain_inbox, brain_wake, persist
    payload, kind, work_id = receipt['payload'], receipt['outcome'], receipt['work_id']
    refs = payload['concern_refs']
    if kind == 'attached':
        results = [persist.attach_work(ref, work_id=work_id, work=payload['work'], receipt_id=receipt['id'])
                   for ref in refs]
    elif kind == 'judged':
        results = [persist.record_judgment(ref, work_id=work_id, task=payload['task'], receipt_id=receipt['id'])
                   for ref in refs]
    else:
        snapshot = SimpleNamespace(nodes={n['id']: SimpleNamespace(**n) for n in payload['reply_nodes']})
        response = _last_user_reply(snapshot)
        results = [persist.apply_work_outcome(
            ref, work_id=work_id, outcome=kind, user_response=response,
            receipt_id=receipt['id'], work_context=payload['context']) for ref in refs]
    if 'unresolved' in results:
        raise ValueError('one or more concern references could not be resolved')
    if kind != 'attached':
        brain_inbox.add_work_event(
            work_id=work_id, receipt_id=receipt['id'], occurred_at=receipt['created_at'],
            text=_event_text(receipt), concern_ids=[persist.concern_id_for(ref) for ref in refs])
        brain_wake.poke()
    store.acknowledge_concern_feedback(receipt['id'])


def recover_pending_concern_feedback(store, *, work_id=None):
    """Deliver committed receipts in order, isolating failures so unrelated work can deliver. A work
    object's receipts stop at its first failure, so a judgment never lands before its attachment."""
    delivered, stuck = 0, set()
    for receipt in store.pending_concern_feedback(work_id):
        if receipt['work_id'] in stuck:
            continue
        try:
            _deliver_receipt(store, receipt)
            delivered += 1
        except Exception:
            stuck.add(receipt['work_id'])
            logger.exception('[concern_feedback] receipt %s (%s) for %s could not finish',
                             receipt['id'], receipt['outcome'], receipt['work_id'])
    return delivered


def propagate_work_outcome(store, work_id: str, outcome: str) -> None:
    """Post-commit delivery of every receipt the work has pending: after creation, after each
    finalizer judgment, after closure. `outcome` names the change, for the log."""
    try:
        recover_pending_concern_feedback(store, work_id=work_id)
    except Exception:
        logger.exception('[concern_feedback] delivery failed for %s (%s)', work_id, outcome)
