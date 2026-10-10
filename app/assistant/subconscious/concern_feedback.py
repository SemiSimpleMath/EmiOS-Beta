"""Work feedback — whatever is done reports to the concerns that hold it, and to the brain.

Since 2026-10-09 only work connected to a concern reports to the brain (owner: "if the work is not connected to a
concern it should not come back to the brain. brain gets the info via tickets anyways, also the context calendar is
updated"). Until then every work object's judgments and ending were brain events, the gate routed the reports of
routine calendar work to the nearest concern by theme, and the brain filed them there (a dog-walk follow-up under a
birthday): 43 of the 63 events the gate routed on 2026-10-09 were work reports.

WorkStore commits a receipt in the same transaction as each change (work_objects/concern_outbox.py):
the work was attached (it cites a concern), the finalizer judged one of its tasks, or the work ended,
including automatic rollup. Every work object's judgments and ending are receipts, whether it cites a
concern or not. Dayflow delivers after creation, each judgment and each closure; evaluator prep retries
receipts left by a crash or register failure.

Delivery of an attachment gives each concern the work cites a reference to it (persist.attach_work); readers
render the work live from the store (concern_view.attempts). A judgment or an ending becomes a brain inbox event
that the gate routes like any chat message or email, naming the concerns the work was made for. Until 2026-10-09
each judgment and ending was also copied onto the concerns holding the work, which moved them between active and
addressing (and a decline button parked one dormant), and the brain attached work to concerns by judgment
(docs/design/concerns_as_state_2026-10-09.md, rules 3 and 3b). Each receipt is idempotent in the register and in
the inbox. Delivery failures never roll back work; a receipt remains until the concerns and the inbox have it.
"""
from __future__ import annotations

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _reply_line(reply: dict) -> str:
    """One recorded owner reply as the brain reads it: the question, the choice, the words, and when (local)."""
    from app.assistant.utils.time_utils import local_time_text
    details = reply.get("response_details") or {}
    history = details.get("response_history") or []
    latest = history[-1] if history else details
    said = str(reply.get("user_text") or latest.get("typed_text") or "").strip()
    choice = str(latest.get("label") or reply.get("action") or "").strip()
    when = latest.get("responded_at") or reply.get("responded_at")
    parts = [f'The owner was asked: "{reply.get("question")}"'] if reply.get("question") else []
    if when:
        parts.append(f"replied {local_time_text(when)}")
    if choice:
        parts.append(f"chose: {choice}")
    if said:
        parts.append(f'said: "{said}"')
    return "; ".join(parts) or "The owner replied (no words recorded)."


def _finding(pod_id: str) -> dict:
    """A research finding as the brain reads it: whole. The brain has no tool to open a pod; the report
    carries the finding (owner, 2026-10-05: findings reach the brain through the work that made them)."""
    from app.assistant.pod_store.pod_store import PodStore
    pod = PodStore().get(pod_id)
    if pod is None:
        return {'pod_id': pod_id, 'title': '(no longer stored)', 'body': ''}
    return {'pod_id': pod_id, 'title': pod.one_liner, 'body': pod.body or ''}


def _report_text(report: dict) -> str:
    """A work report as the brain inbox holds it, worded by shared/work/work_report.j2 (the template every
    reader of a work report shares)."""
    from app.assistant.dayflow_orchestrator.work_context import render_view
    return render_view("work_report", report=report).strip()


def _event_text(receipt, concern_titles=()) -> str:
    """The brain event for a judgment or an ending: what happened, in the finalizer's and the owner's own
    words, what the work was made for, and where the work stands at that moment (its progress: what is
    done and what is still to come), so a reader sees whether the work goes on."""
    payload, kind = receipt['payload'], receipt['outcome']
    work = payload.get('work') or {'title': (payload.get('context') or {}).get('title')}
    report = {'kind': kind, 'work_id': receipt['work_id'], 'title': work['title'], 'at': receipt['created_at'],
              'objective': work.get('objective') or work['title'], 'concern_titles': list(concern_titles),
              'progress': payload.get('progress')}
    if kind == 'judged':
        task, fin = payload['task'], payload['task']['finalizer']
        report.update(task_title=task['title'], outcome=fin.get('outcome'), replies=[_reply_line(r) for r in task.get('replies') or []],
                      findings=[_finding(p) for p in task.get('finding_refs') or []])
    else:
        report['reason'] = (payload['context'].get('terminal') or {}).get('reason')
    return _report_text(report)


def reports_to_brain(work_id: str, *, store=None) -> bool:
    """Whether a work object's judgments and ending reach the brain: only work made for a concern (it cites one).
    Owner, 2026-10-09: "if the work is not connected to a concern it should not come back to the brain. brain gets
    the info via tickets anyways"."""
    if store is None:
        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        store = get_dayflow_work_store()
    return bool((store.load(work_id).constraints or {}).get("concern_refs"))


def _deliver_receipt(store, receipt):
    from app.assistant.subconscious import brain_inbox, brain_wake, persist
    payload, kind, work_id = receipt['payload'], receipt['outcome'], receipt['work_id']
    refs = list(payload['concern_refs'])
    if kind == 'attached':
        results = [persist.attach_work(ref, work_id=work_id, receipt_id=receipt['id']) for ref in refs]
        if 'unresolved' in results:
            raise ValueError('one or more concern references could not be resolved')
    # Only work connected to a concern reports to the brain (reports_to_brain); other work's outcome reaches its belief
    # and the day's context, and what the owner said in its tickets reaches the brain as a ticket reply.
    if kind != 'attached' and refs:
        brain_inbox.add_work_event(
            work_id=work_id, receipt_id=receipt['id'], occurred_at=receipt['created_at'],
            text=_event_text(receipt, [t for t in (persist.concern_title(ref) for ref in refs) if t]))
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
