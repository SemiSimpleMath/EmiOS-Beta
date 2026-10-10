"""A concern as its readers see it: its record's fingerprint, the work attached to it, and the view dayflow's agents
read of the concerns a work object serves.

Until 2026-10-09 this was the concern brief (`subconscious::brief`), a page rewritten by a model every time a
concern's record changed, and read by dayflow's agents, the planner, the stories and the ticket page. Every reader now
reads the record itself: its summary and the notes written since, whole (persist.history_for_prompt; the summary is
written only when the notes grow long, subconscious/concern_summary.py). docs/design/concerns_as_state_2026-10-09.md,
rule 1.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

# What is written from the record, so not part of it: a change to these is not a change to the concern.
_OWN_KEYS = ("summary", "summary_error", "plan", "plan_error")


def basis(concern: Dict[str, Any], bucket: str) -> str:
    """Fingerprint of the concern's record, excluding what is written from it (its summary, its plan)."""
    record = {k: v for k, v in concern.items() if k not in _OWN_KEYS}
    return hashlib.sha256(json.dumps([bucket, record], sort_keys=True, ensure_ascii=False,
                                     default=str).encode("utf-8")).hexdigest()


def attempts(concern: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The work attached to the concern, oldest first, read live from the work store every time (rule 3b; until
    2026-10-09 a copy kept on the concern, which went stale): objective, status, where it stands now (what is done,
    what is still to come and what each step waits for), each task's judgment in the finalizer's words with the
    owner's replies as lines, and the ending."""
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    from app.assistant.subconscious.concern_feedback import _reply_line
    from work_objects.concern_outbox import _replies
    from work_objects.progress import work_progress
    store = get_dayflow_work_store()
    out = []
    for ref in sorted((concern.get("attached_work") or {}).values(), key=lambda w: str(w.get("attached_at") or "")):
        wo = store.load(ref["work_id"])
        goal = wo.nodes.get(wo.goal_node_id)
        terminal = (goal.payload.get("terminal") or {}) if goal else {}
        ended = wo.status in ("done", "abandoned")
        out.append({
            "work_id": wo.id, "title": wo.title, "objective": (wo.constraints or {}).get("objective") or wo.title,
            "attached_at": ref.get("attached_at"), "status": wo.status if ended else "active",
            "progress": work_progress(wo),
            "judgments": [{"node_id": n.id, "title": n.title, "outcome": n.payload["finalizer"].get("outcome"),
                           "at": n.payload["finalizer"].get("at"),
                           "replies": [_reply_line(r) for r in _replies(wo, n.id)]}
                          for n in sorted(wo.nodes.values(), key=lambda n: str(n.created_at))
                          if wo.is_work_unit(n) and (n.payload or {}).get("finalizer")],
            "ended": {"outcome": wo.status, "at": terminal.get("at") or str(wo.updated_at),
                      "reason": terminal.get("reason") or ""} if ended else None})
    return out


# A concern's source as a reader names it (rule 6).
_SOURCE_WORDS = {"user": "the owner", "email": "an email", "calendar": "the calendar", "camera": "a camera",
                 "presence": "presence at the computer", "work": "a report on the assistant's work",
                 "noticer": "the noticer's reading of the long view", "brain": "the brain's inference",
                 "belief": "a belief about the household"}


def source_text(concern: Dict[str, Any]) -> str:
    """Where the concern came from, as every reader shows it: who or what, when (local), and its own words, copied
    exactly. Empty for a concern raised before sources were kept (2026-10-09)."""
    from app.assistant.utils.time_utils import local_time_text
    src = concern.get("source")
    if not src:
        return ""
    return f'{_SOURCE_WORDS[src["kind"]]}, {local_time_text(src["at"])}: "{src["content"]}"'


def expires_text(concern: Dict[str, Any]) -> str:
    """When the concern stops mattering, local; empty when it has no expiry yet."""
    from app.assistant.utils.time_utils import local_time_text
    return local_time_text(concern["expires_at"]) if concern.get("expires_at") else ""


def view(concern: Dict[str, Any], bucket: str) -> Dict[str, Any]:
    """One concern for the shared template every agent working on it reads (shared/work/concern_view.j2): what it
    is, where it came from, what settles it, when it expires, its record (the summary and the notes since, whole), its
    plan and the work done for it."""
    from app.assistant.subconscious.persist import history_for_prompt
    return {"concern_id": concern["concern_id"], "title": concern.get("title"), "status": bucket,
            "severity": concern.get("severity"), "done_when": concern.get("done_when"),
            "source": source_text(concern), "expires": expires_text(concern),
            "history": history_for_prompt(concern), "plan": concern.get("plan"), "attempts": attempts(concern)}


def future_local_utc(value: str, now_utc: datetime) -> datetime:
    """A local 'YYYY-MM-DD HH:MM' as UTC, for a plan's hold or a concern's expiry; raises when unreadable or not in the
    future."""
    from app.assistant.utils.time_utils import get_local_timezone
    local = datetime.strptime(str(value or "").strip(), "%Y-%m-%d %H:%M").replace(tzinfo=get_local_timezone())
    when = local.astimezone(timezone.utc)
    if when <= now_utc:
        raise ValueError(f"{value!r} is not in the future")
    return when


def concerns_for_refs(refs: List[str]) -> List[Dict[str, Any]]:
    """The concerns a work object cites (full ids or `concern:<8+ chars>`), each as `view` shows it, for the agents
    working on it. This runs on every render of the work object, so a ref that matches no concern, or more than one,
    is logged as an error and shown to the agent as unresolved rather than stopping the work from rendering."""
    from app.assistant.subconscious.concern_store import load_register
    wanted = [str(r).strip() for r in refs if str(r).strip()]
    if not wanted:
        return []
    register = load_register()
    every = [(b, c) for b in ("active", "addressing", "resolved", "dormant") for c in register.get(b) or []]
    out = []
    for ref in wanted:
        key = ref[len("concern:"):] if ref.startswith("concern:") else ref
        matches = [(b, c) for b, c in every
                   if c.get("concern_id") == key or (len(key) >= 8 and str(c.get("concern_id")).startswith(key))]
        if len(matches) != 1:
            logger.error("[concern_view] work cites concern ref %r, which matches %d concerns", ref, len(matches))
            out.append({"ref": ref, "unresolved": f"matches {len(matches)} concerns in the register"})
            continue
        bucket, c = matches[0]
        out.append(view(c, bucket))
    return out
