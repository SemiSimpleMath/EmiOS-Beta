"""The concern brief: what everyone working on a concern reads first (2026-09-30, step 5).

A concern's record grows as a list of evidence refs, journal lines and work outcomes. Dayflow saw
none of it: the planner got the title and notes cut at 200 characters, and the agents doing the work
saw nothing of the concern. The brief is the record made readable for someone new to the matter:
what it is, why it matters, what is known (each fact with its source), what was tried and how it
went, what the owner said, what depends on it, open questions, and a recommendation.

`subconscious::brief` writes it for every open concern whose record changed since its brief was
written, run by the brain_gate routine after the brain step. The brief is stored on the concern
(`brief`, with the `basis` fingerprint of the record it was written from); a concern whose brief
could not be written carries `brief_error` with the same fingerprint and is not retried until its
record changes again. Every cited source must appear in what the writer was shown; one correction.

Dayflow reads it through `briefs_for_refs`: every agent working on a work object that cites a
concern (architect, worker, finalizer) gets that concern's brief whole (dayflow_orchestrator/
work_context.work_data).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_AGENT = "subconscious::brief"
_OWN_KEYS = ("brief", "brief_error")
_FIELDS = ("what", "why_it_matters", "tried", "owner_wishes", "recommendation")
_OWN_RECORD = ("journal", "notes")   # a fact from the concern's own journal or notes has no other ref


def basis(concern: Dict[str, Any], bucket: str) -> str:
    """Fingerprint of the concern's record, excluding the brief itself."""
    record = {k: v for k, v in concern.items() if k not in _OWN_KEYS}
    return hashlib.sha256(json.dumps([bucket, record], sort_keys=True, ensure_ascii=False,
                                     default=str).encode("utf-8")).hexdigest()


def stale(register: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """Open concerns whose brief is missing or was written from an older record, minus those whose
    brief already failed on the current record. (bucket, concern) pairs."""
    out = []
    for bucket in ("active", "addressing"):
        for c in register.get(bucket) or []:
            b = basis(c, bucket)
            if (c.get("brief") or {}).get("basis") == b or (c.get("brief_error") or {}).get("basis") == b:
                continue
            out.append((bucket, c))
    return out


def build_payload(concern: Dict[str, Any], bucket: str, *, calendar: str, now_utc: datetime) -> Dict[str, Any]:
    from app.assistant.subconscious import brain_step, work_links
    from app.assistant.utils.time_utils import utc_to_local
    linked = work_links.linked_work([concern["concern_id"]], [])
    similar = work_links.similar_work([concern.get("title") or "", concern.get("notes") or ""],
                                      exclude=[w["work_id"] for w in linked])
    return {
        "now": utc_to_local(now_utc.isoformat()).strftime("%a %Y-%m-%d %H:%M"),
        "concern": brain_step._concern_view("C1", {**concern, "_status": bucket}),
        "linked_work": linked, "similar_work": similar,
        "active_work": work_links.active_work(), "reminders": work_links.live_reminders(now_utc),
        "calendar": calendar,
    }


def _problems(data: Any, shown: str) -> List[str]:
    if not isinstance(data, dict):
        return ["no brief"]
    out = [f"{f} is empty" for f in _FIELDS if not str(data.get(f) or "").strip()]
    for fact in data.get("known") or []:
        source = str(fact.get("source") or "").strip()
        if not source or (source not in _OWN_RECORD and source not in shown):
            out.append(f"source {source!r} (for {str(fact.get('fact'))!r}) was not shown; cite a ref exactly as shown, or the word journal or the word notes")
    return out


def _agent_call(payload: Dict[str, Any]) -> Any:
    from app.assistant.routine_handlers.subconscious import _run_subconscious_agent
    return _run_subconscious_agent(handler_label="brief", agent_name=_AGENT, context=payload,
                                   scope_id="subconscious::brief", actor_id="subconscious::brief")


def write_brief(concern: Dict[str, Any], bucket: str, *, calendar: str,
                call: Optional[Callable[[Dict[str, Any]], Any]] = None) -> Dict[str, Any]:
    """The brief for one concern; raises when still invalid after one correction."""
    now = datetime.now(timezone.utc)
    payload = build_payload(concern, bucket, calendar=calendar, now_utc=now)
    shown = json.dumps(payload, ensure_ascii=False)
    call = call or _agent_call
    data = call(payload)
    problems = _problems(data, shown)
    if problems:
        logger.warning("[brief] invalid brief for %s, one correction: %s", concern["concern_id"], problems)
        data = call({**payload, "correction": "; ".join(problems)})
        problems = _problems(data, shown)
        if problems:
            raise ValueError(f"brief still invalid after one correction: {problems}")
    return {k: data.get(k) for k in ("what", "why_it_matters", "known", "tried", "owner_wishes",
                                     "depends_on_it", "open_questions", "recommendation")}


def run_briefs(*, call=None, register_connect=None, calendar: Optional[str] = None) -> Dict[str, Any]:
    """Write the brief of every open concern whose record changed. Free when none did."""
    from app.assistant.subconscious import persist
    from app.assistant.subconscious.concern_store import load_register
    todo = stale(load_register(connect=register_connect))
    if not todo:
        return {"briefs_written": 0, "briefs_failed": 0}
    if calendar is None:
        from app.assistant.subconscious.brain_step import _calendar
        calendar = _calendar(datetime.now(timezone.utc))
    written = failed = 0
    for bucket, concern in todo:
        written_from = basis(concern, bucket)
        try:
            brief = write_brief(concern, bucket, calendar=calendar, call=call)
        except Exception as exc:
            logger.error("[brief] could not write the brief for %s; not retried until it changes: %s",
                         concern["concern_id"], exc, exc_info=True)
            persist.set_concern_brief(concern["concern_id"], written_from, error=f"{type(exc).__name__}: {exc}",
                                      connect=register_connect)
            failed += 1
            continue
        if persist.set_concern_brief(concern["concern_id"], written_from, brief=brief, connect=register_connect):
            written += 1
    logger.info("[brief] written=%d failed=%d", written, failed)
    return {"briefs_written": written, "briefs_failed": failed}


def briefs_for_refs(refs: List[str]) -> List[Dict[str, Any]]:
    """The concerns a work object cites (full ids or `concern:<8+ chars>`), with their briefs, for the
    agents working on it. This runs on every render of the work object, so a ref that matches no
    concern, or more than one, is logged as an error and shown to the agent as unresolved rather
    than stopping the work from rendering."""
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
            logger.error("[brief] work cites concern ref %r, which matches %d concerns", ref, len(matches))
            out.append({"ref": ref, "unresolved": f"matches {len(matches)} concerns in the register"})
            continue
        bucket, c = matches[0]
        out.append({"concern_id": c["concern_id"], "title": c.get("title"), "status": bucket,
                    "done_when": c.get("done_when"), "owner_words": (c.get("owner_request") or {}).get("words"),
                    "brief": c.get("brief"),
                    "brief_current": (c.get("brief") or {}).get("basis") == basis(c, bucket)})
    return out
