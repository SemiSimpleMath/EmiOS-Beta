"""A concern's summary: its older notes folded into one account, written only when the notes grow long (2026-10-09).

Owner, 2026-10-09: "we also need something that looks at these concerns and can just summarize them. we cant have
something with 100 notes on it. we also don't want to show truncated record."; "It does not have to run on every
concern every time. only when the record gets long." (docs/design/concerns_as_state_2026-10-09.md, rule 1).

Every reader of a concern gets its summary and the notes written since, whole (persist.history_for_prompt). When the
notes no summary covers pass SUMMARY_AFTER_CHARS, `subconscious::concern_summary` reads the previous summary and those
notes and writes the new summary; the notes it folded stay in the concern's journal as history. It replaced the
brief, rewritten by a model on every change to the record (subconscious/concern_view.py). Run by the brain's wake
after the brain step. A summary that could not be written is recorded (`summary_error`) and not tried again until
another note is written.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_AGENT = "subconscious::concern_summary"
# The notes no summary covers are folded once they are longer than this (characters, about ten notes).
SUMMARY_AFTER_CHARS = 3000


def _journal_count(concern: Dict[str, Any]) -> int:
    from app.assistant.subconscious.persist import _journal_entries
    return len(_journal_entries(concern.get("reinforcement_notes") or ""))


def due(register: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """Open concerns whose notes since their summary are longer than SUMMARY_AFTER_CHARS, minus those whose summary
    already failed on the same notes. (bucket, concern)."""
    from app.assistant.subconscious.persist import unsummarized
    out = []
    for bucket in ("active", "addressing"):
        for c in register.get(bucket) or []:
            if len("\n".join(unsummarized(c))) <= SUMMARY_AFTER_CHARS:
                continue
            if (c.get("summary_error") or {}).get("lines") == _journal_count(c):
                continue
            out.append((bucket, c))
    return out


def build_payload(concern: Dict[str, Any], bucket: str) -> Dict[str, Any]:
    from app.assistant.subconscious import concern_view
    from app.assistant.subconscious.persist import unsummarized
    from app.assistant.utils.time_utils import local_time_text
    summary = concern.get("summary")
    return {"concern": {"title": concern.get("title"), "status": bucket, "subject": concern.get("subject") or "household",
                        "done_when": concern.get("done_when"),
                        "source": concern_view.source_text(concern)},
            "summary": summary["text"] if summary else None,
            "summary_through": local_time_text(summary["through"]) if summary else None,
            "notes": unsummarized(concern)}


def _agent_call(payload: Dict[str, Any]) -> Any:
    from app.assistant.routine_handlers.subconscious import _run_subconscious_agent
    return _run_subconscious_agent(handler_label="concern_summary", agent_name=_AGENT, context=payload,
                                   scope_id="subconscious::concern_summary", actor_id="subconscious::concern_summary")


def write_summary(concern: Dict[str, Any], bucket: str, *,
                  call: Optional[Callable[[Dict[str, Any]], Any]] = None) -> str:
    """The new summary of one concern; raises when the answer has no summary after one correction."""
    payload = build_payload(concern, bucket)
    call = call or _agent_call
    data = call(payload)
    if not isinstance(data, dict) or not str(data.get("summary") or "").strip():
        logger.warning("[concern_summary] no summary for %s, one correction", concern["concern_id"])
        data = call({**payload, "correction": "the answer had no summary"})
        if not isinstance(data, dict) or not str(data.get("summary") or "").strip():
            raise ValueError("concern summary still empty after one correction")
    return str(data["summary"]).strip()


def run_summaries(*, call=None, register_connect=None) -> Dict[str, Any]:
    """Summarize every open concern whose notes since its summary grew long. Free when none did."""
    from app.assistant.subconscious import persist
    from app.assistant.subconscious.brain_trace import step, trace
    from app.assistant.subconscious.concern_store import load_register
    todo = due(load_register(connect=register_connect))
    if not todo:
        return {"summaries_written": 0, "summaries_failed": 0}
    written = failed = 0
    for bucket, concern in todo:
        cid, lines = concern["concern_id"], _journal_count(concern)
        step("summary", "due", subject=cid, title=concern.get("title"), notes=lines)
        try:
            with trace(stage="summary", concern_id=cid):
                text = write_summary(concern, bucket, call=call)
        except Exception as exc:
            logger.error("[concern_summary] could not summarize %s; not tried again until a note is written: %s",
                         cid, exc, exc_info=True)
            step("summary", "failed: not tried again until a note is written", subject=cid,
                 error=f"{type(exc).__name__}: {exc}")
            persist.set_concern_summary(cid, lines, error=f"{type(exc).__name__}: {exc}", connect=register_connect)
            failed += 1
            continue
        persist.set_concern_summary(cid, lines, text=text, connect=register_connect)
        step("summary", "written", subject=cid, folded=lines)
        written += 1
    logger.info("[concern_summary] written=%d failed=%d", written, failed)
    return {"summaries_written": written, "summaries_failed": failed}
