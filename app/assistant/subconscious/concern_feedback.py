"""Concern back-propagation — work-object outcomes flow into the concerns register.

WorkStore commits a pending receipt with each concern-linked terminal transition,
including automatic rollup. Dayflow delivers after finalization/explicit closure;
evaluator prep retries receipts left by a crash or register failure. Each receipt
is idempotent in the concern register. Generic stores never invoke the noticer.

Delivery failures never roll back completed work. A pending receipt remains until
all linked concerns have durably received the outcome. Noticer triggering is a
best-effort, cooldown-guarded acceleration of its normal scheduled run.
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


def _deliver_receipt(store, receipt):
    from types import SimpleNamespace
    from app.assistant.subconscious.persist import apply_work_outcome
    payload = receipt['payload']
    snapshot = SimpleNamespace(nodes={n['id']: SimpleNamespace(**n) for n in payload['reply_nodes']})
    response = _last_user_reply(snapshot)
    results = [apply_work_outcome(
        ref, work_id=receipt['work_id'], outcome=receipt['outcome'], user_response=response,
        receipt_id=receipt['id'], work_context=payload['context']) for ref in payload['concern_refs']]
    if 'unresolved' in results:
        raise ValueError('one or more concern references could not be resolved')
    store.acknowledge_concern_feedback(receipt['id'])
    return any(result != 'already_applied' for result in results)


def _trigger(work_id, outcome):
    from app.assistant.subconscious.answer_capture import trigger_noticer
    trigger_noticer(reason=f"work_outcome:{work_id}:{outcome}")


def recover_pending_concern_feedback(store, *, work_id=None):
    """Retry committed receipts, isolating failures so unrelated closures can deliver."""
    delivered = 0
    for receipt in store.pending_concern_feedback(work_id):
        try:
            changed = _deliver_receipt(store, receipt)
            delivered += 1
        except Exception:
            logger.exception('[concern_feedback] receipt %s for %s could not finish',
                             receipt['id'], receipt['work_id'])
            continue
        if changed:
            try:
                _trigger(receipt['work_id'], receipt['outcome'])
            except Exception:
                logger.exception('[concern_feedback] outcome saved; noticer wake failed for %s',
                                 receipt['work_id'])
    return delivered


def propagate_work_outcome(store, work_id: str, outcome: str) -> None:
    """Post-commit delivery; legacy explicit callers without a receipt still work."""
    try:
        if hasattr(store, 'pending_concern_feedback'):
            recover_pending_concern_feedback(store, work_id=work_id)
            return
        wo = store.load(work_id)
        refs = [str(r).strip() for r in ((wo.constraints or {}).get('concern_refs') or []) if str(r).strip()]
        if not refs:
            return
        from app.assistant.subconscious.persist import apply_work_outcome
        results = [apply_work_outcome(ref, work_id=work_id, outcome=outcome,
                                     user_response=_last_user_reply(wo)) for ref in refs]
        if 'unresolved' in results:
            raise ValueError('one or more concern references could not be resolved')
        _trigger(work_id, outcome)
    except Exception:
        logger.exception('[concern_feedback] propagation failed for %s (%s)', work_id, outcome)
