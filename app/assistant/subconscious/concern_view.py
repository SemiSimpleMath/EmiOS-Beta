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
    """The work attached to the concern, oldest first, as every reader shows it: objective, status,
    where it stands now (read live from the work store: what is done, what is still to come and what
    each step waits for), each judgment with the owner's replies as lines, and the ending."""
    from app.assistant.subconscious.concern_feedback import _reply_line
    from app.assistant.subconscious.work_links import progress
    works = sorted((concern.get("attached_work") or {}).values(), key=lambda w: str(w.get("attached_at") or ""))
    return [{"work_id": w["work_id"], "title": w.get("title"), "objective": w.get("objective"),
             "attached_at": w.get("attached_at"), "status": w.get("status"), "progress": progress(w["work_id"]),
             "judgments": [{**j, "replies": [_reply_line(r) for r in j.get("replies") or []]}
                           for j in w.get("judgments") or []],
             "ended": w.get("ended")}
            for w in works]


def view(concern: Dict[str, Any], bucket: str) -> Dict[str, Any]:
    """One concern for the shared template every agent working on it reads (shared/work/concern_view.j2): what it
    is, what settles it, its record (the summary and the notes since, whole), its plan and the work done for it."""
    from app.assistant.subconscious.persist import history_for_prompt
    return {"concern_id": concern["concern_id"], "title": concern.get("title"), "status": bucket,
            "severity": concern.get("severity"), "done_when": concern.get("done_when"),
            "owner_words": (concern.get("owner_request") or {}).get("words"),
            "history": history_for_prompt(concern), "plan": concern.get("plan"), "attempts": attempts(concern)}


def hold_until_utc(value: str, now_utc: datetime) -> datetime:
    """A local 'YYYY-MM-DD HH:MM' as UTC; raises when unreadable or not in the future."""
    from app.assistant.utils.time_utils import get_local_timezone
    local = datetime.strptime(str(value or "").strip(), "%Y-%m-%d %H:%M").replace(tzinfo=get_local_timezone())
    when = local.astimezone(timezone.utc)
    if when <= now_utc:
        raise ValueError(f"hold_until {value!r} is not in the future")
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
