"""dayflow_orchestrator.action_ledger — record what a work object DID to the outside world.

Nodes say what is planned and what state it is in. Until 2026-09-12 nothing said what had
actually been DONE, so no planning pass could see its own past tense. A stuck flea-medication
goal emailed one recipient eleven times in fifty-two minutes while every guard in the system
counted timeouts, prunes and minutes. None counted sends, because there was nothing to count.

`record_outbound` is called BY THE TOOL that performs the side effect, at the moment it
happens. Not by an agent afterwards: an agent that forgets to log is precisely the blindness
this closes.

The work context is recovered from the session thread's NAME
(`work_session::<work_id>::<node_id>`, see work_session.session_id_for), so a tool needs no new
argument and no plumbing through six layers. A send that is not running under a work session —
a chat-initiated email, say — has no work object to charge and records nothing.

The ledger is keyed on WORK ID, not node id. Every node-keyed counter in this system resets
when the planner mints a replacement node, which is exactly how the loop escaped the
three-timeout ask ceiling.
"""
from __future__ import annotations

import threading
from typing import Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_PREFIX = "work_session::"


def current_work_context() -> tuple[Optional[str], Optional[str]]:
    """(work_id, node_id) for the running work session, or (None, None) off one."""
    name = threading.current_thread().name or ""
    if not name.startswith(_PREFIX):
        return None, None
    parts = name[len(_PREFIX):].split("::")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None, None
    return parts[0], parts[1]


def record_outbound(channel: str, target: str, summary: str, *,
                    outcome: str = "sent", actor: str = "", payload: Optional[dict] = None,
                    work_id: Optional[str] = None, node_id: Optional[str] = None) -> bool:
    """Append one outward-facing act to the current work object's ledger.

    Returns True when a row was written. Pass work_id/node_id explicitly for callers that
    know their context but do not run on a session thread (the ticket path).

    A ledger failure is logged at ERROR and swallowed ON PURPOSE, and this is the one place
    that deviates from fail-loud: the side effect has ALREADY reached the outside world by
    the time we are called. Raising here would make the tool report failure for an email
    that was genuinely sent, which puts a false negative into the graph — the same class of
    blindness this module exists to remove. A missing row is bad; a row that says an email
    did not go out when it did is worse.
    """
    if work_id is None or node_id is None:
        ctx_work, ctx_node = current_work_context()
        work_id = work_id or ctx_work
        node_id = node_id or ctx_node
    if not work_id:
        return False
    try:
        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        get_dayflow_work_store().apply(
            "record_action",
            {"work_id": work_id, "node_id": node_id, "channel": channel,
             "target": target, "summary": summary, "outcome": outcome,
             "payload": payload or {}},
            actor=actor or "tool",
        )
        logger.info("[action_ledger] %s -> %s (%s) on %s", channel, target, outcome, work_id)
        return True
    except Exception as e:
        logger.error(
            "[action_ledger] FAILED to record %s to %s on %s: %s — the act itself already "
            "happened; the ledger is now missing a row and a later pass may repeat it.",
            channel, target, work_id, e, exc_info=True,
        )
        return False


def recent_outbound(work_id: str, *, channel: Optional[str] = None,
                    target: Optional[str] = None, within_minutes: int = 1440) -> list:
    """Acts this work object performed recently, newest first.

    The read side of the ledger: a tool or guard can ask "have I already done this, to this
    person, lately" before doing it again.
    """
    from datetime import timedelta

    from work_objects.model import utcnow
    try:
        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        wo = get_dayflow_work_store().load(work_id)
    except Exception as e:
        logger.error("[action_ledger] could not read ledger for %s: %s", work_id, e)
        return []
    cutoff = utcnow() - timedelta(minutes=within_minutes)
    out = []
    for a in wo.actions:
        ts = a.ts
        if isinstance(ts, str):
            continue
        if ts.tzinfo is None or ts < cutoff:
            continue
        if channel and a.channel != channel:
            continue
        if target and (a.target or "").lower() != target.lower():
            continue
        out.append(a)
    out.sort(key=lambda a: a.ts, reverse=True)
    return out
