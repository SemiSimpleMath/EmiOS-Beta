"""The reading page of a ticket: the whole message and everything it rides on (2026-09-30).

Owner, 2026-09-30: a long notification or question should show only its first lines in the popup,
with a link to a page where it can be read in normal type with all its context: the concern, the
emails that came with it, and whatever else it connects to. The popup keeps the answer buttons;
this page is for reading.

Everything here is assembled by code from links that already exist, no model call:

- a dayflow ticket names its work node (`trigger_context.work_node`, `<work id>::<node id>`): the
  work's goal, the steward's reason, success criteria and the node's directive; its source intake
  (emails, pods); the concerns it serves (`constraints.concern_refs`);
- a noticer question names its question (`trigger_context.question_id`) and through it a concern;
- each concern: its brief, "done when", the owner's words, earlier asks and answers (its work
  outcomes);
- the sources the work, the brief and the concern cite, resolved to their text: an email in full
  with the rest of its Gmail thread, a chat message, a pod;
- the people, places and things named (knowledge graph, subconscious/kg_links.py) and past work
  about the concern (subconscious/work_links.py).
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_REF = re.compile(r"(datapod:[A-Za-z0-9_]+:[A-Za-z0-9_\-]+|message:[A-Za-z0-9_\-:.]+)")


def _ticket(ticket_id: str):
    from app.assistant.ticket_manager.ticket_manager import get_ticket_manager
    ticket = get_ticket_manager().get_ticket_by_id(ticket_id)
    if ticket is None:
        raise KeyError(f"no ticket {ticket_id}")
    return ticket


def _context(ticket) -> Dict[str, Any]:
    ctx = ticket.trigger_context or {}
    return json.loads(ctx) if isinstance(ctx, str) else dict(ctx)


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _work(work_node: str) -> Optional[Dict[str, Any]]:
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    work_id, _, node_id = work_node.partition("::")
    wo = get_dayflow_work_store().load(work_id)
    node = wo.nodes.get(node_id)
    constraints = wo.constraints or {}
    return {"work_id": wo.id, "title": wo.title, "status": getattr(wo, "status", None),
            "objective": constraints.get("objective"), "why": constraints.get("rationale"),
            "success_criteria": constraints.get("success_criteria"),
            "task": (node.content or node.title) if node is not None else None,
            "concern_refs": list(constraints.get("concern_refs") or []),
            "sources": list(constraints.get("source_intake") or [])}


def _question(question_id: str) -> Optional[Dict[str, Any]]:
    from app.assistant.database.pending_question import PendingQuestion
    from app.models.base import get_session
    session = get_session()
    try:
        row = session.query(PendingQuestion).filter_by(id=question_id).first()
        if row is None:
            return None
        return {"question_id": row.id, "text": row.question_text, "status": row.status,
                "related_concern_id": row.related_concern_id, "answer": row.answer_text}
    finally:
        session.close()


def _concern(ref: str) -> Optional[Dict[str, Any]]:
    """A concern by full id or `concern:<8+ chars>`, with the parts a reader needs."""
    from app.assistant.subconscious.concern_store import load_register
    key = ref[len("concern:"):] if ref.startswith("concern:") else ref
    register = load_register()
    for bucket in ("active", "addressing", "resolved", "dormant"):
        for c in register.get(bucket) or []:
            cid = str(c.get("concern_id"))
            if cid == key or (len(key) >= 8 and cid.startswith(key)):
                return {"concern_id": cid, "status": bucket, "title": c.get("title"), "subject": c.get("subject"),
                        "done_when": c.get("done_when"), "owner_words": (c.get("owner_request") or {}).get("words"),
                        "brief": c.get("brief"),
                        "earlier": [{"work_id": w.get("work_id"), "outcome": w.get("outcome"),
                                     "at": w.get("recorded_at"), "response": w.get("user_response")}
                                    for w in (c.get("work_outcomes") or {}).values()],
                        "evidence": [str(e.get("ref") or "") for e in c.get("evidence") or []],
                        "evidence_kinds": {str(e.get("ref") or ""): e.get("kind") for e in c.get("evidence") or []}}
    return None


def _source(ref: str, kind: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """A cited source as its text: an email with its thread, a chat message, a pod."""
    from app.assistant.pod_store.pod_store import PodStore
    if ref.startswith("datapod:email:"):
        from app.assistant.pod_store import email_pods
        record = email_pods.email_by_pod_id(ref)
        thread = []
        if record.get("thread_id"):
            thread = sorted((r for r in email_pods.thread_emails(record.get("account_id"), record["thread_id"])
                             if r["pod_id"] != ref), key=email_pods.email_time)
        return {"kind": "email", "ref": ref, "sender": email_pods.email_sender_line(record),
                "subject": record.get("subject"), "at": email_pods.email_time(record), "body": record.get("body"),
                "thread": [{"sender": email_pods.email_sender_line(r), "subject": r.get("subject"),
                            "at": email_pods.email_time(r), "body": r.get("body")} for r in thread]}
    if ref.startswith("datapod:"):
        pod = PodStore().get(ref)
        if pod is None:
            return None
        return {"kind": pod.kind, "ref": ref, "title": pod.one_liner, "body": pod.body, "at": _iso(pod.created_at)}
    message_id = ref[len("message:"):] if ref.startswith("message:") else (ref if kind == "chat_msg" else None)
    if message_id:
        from app.assistant.database.db_handler import UnifiedLog2026
        from app.models.base import get_session
        session = get_session()
        try:
            row = session.query(UnifiedLog2026).filter(UnifiedLog2026.id == message_id).first()
            if row is None:
                return None
            return {"kind": "chat", "ref": f"message:{message_id}", "room": row.room_id, "at": _iso(row.timestamp),
                    "speaker": "assistant" if row.role == "assistant" else (row.speaker_name or "user"),
                    "body": row.message}
        finally:
            session.close()
    return None


def ticket_reading(ticket_id: str) -> Dict[str, Any]:
    """Everything the reading page shows for one ticket."""
    from app.assistant.subconscious import kg_links, work_links
    ticket = _ticket(ticket_id)
    ctx = _context(ticket)
    work = _work(ctx["work_node"]) if ctx.get("work_node") else None
    question = _question(ctx["question_id"]) if ctx.get("question_id") else None

    refs = list((work or {}).get("concern_refs") or [])
    if question and question.get("related_concern_id"):
        refs.append(question["related_concern_id"])
    concerns = [c for c in (_concern(r) for r in dict.fromkeys(refs)) if c]

    cited: Dict[str, Optional[str]] = {}
    for s in (work or {}).get("sources") or []:
        if s.get("pod_id"):
            cited.setdefault(s["pod_id"], None)
    for c in concerns:
        for fact in (c.get("brief") or {}).get("known") or []:
            for ref in _REF.findall(str(fact.get("source") or "")):
                cited.setdefault(ref, None)
        for ref in c["evidence"]:
            cited.setdefault(ref, c["evidence_kinds"].get(ref))
    sources = []
    for ref, kind in cited.items():
        try:
            found = _source(ref, kind)
        except KeyError:
            found = None
        if found:
            sources.append(found)
        else:
            logger.warning("[reading] %s: cited source %s is not stored", ticket_id, ref)

    texts = [ticket.title or "", ticket.message or ""] + [c.get("title") or "" for c in concerns] + \
        [(c.get("brief") or {}).get("what") or "" for c in concerns]
    current = (work or {}).get("work_id")
    related: Dict[str, Dict[str, Any]] = {}
    for c in concerns:
        for w in work_links.linked_work([c["concern_id"]], []):
            if w["work_id"] != current:
                related.setdefault(w["work_id"], w)
    for c in concerns:
        c.pop("evidence", None)
        c.pop("evidence_kinds", None)
    return {
        "ticket": {"ticket_id": ticket.ticket_id, "type": ticket.ticket_type, "title": ticket.title,
                   "message": ticket.message, "state": getattr(ticket.state, "value", ticket.state), "created_at": _iso(ticket.created_at),
                   "responded_at": _iso(ticket.responded_at), "user_action": ticket.user_action,
                   "user_text": ticket.user_text,
                   "choices": [c.get("label") for c in ctx.get("response_choices") or []]},
        "work": work, "question": question, "concerns": concerns, "sources": sources,
        "entities": kg_links.find_entities(texts), "related_work": list(related.values()),
    }
