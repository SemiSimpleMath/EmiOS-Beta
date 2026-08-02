"""Concern back-propagation — work-object outcomes flow into the concerns register.

The forward edge: the evaluator cites ``concern:<prefix>`` in based_on when it mints
work from a subconscious concern; work_persist stores the refs on the work object
(``constraints.concern_refs``). This module is the backward edge: every dayflow
closure path (steward complete/abandon, finalizer, repair) calls
``propagate_work_outcome`` after a terminal ``set_work_status``. The outcome — with
the user's own recorded words when they settled it — is written onto the concern
through the register's single writer, then the noticer is re-run (cooldown-guarded)
so the subconscious reacts within minutes instead of at tomorrow's tick.

Post-commit side effect: the closure itself has already committed, so this NEVER
raises into a closure path — failures log ERROR and the tick continues.
"""
from __future__ import annotations

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _last_user_reply(wo) -> str:
    """The user's final word recorded on the graph: newest reply-evidence content.
    Nodes rebuild in event order, so the last matching node is the newest — the
    same convention the evaluator's RECENTLY DROPPED render uses."""
    replies = [
        n for n in (getattr(wo, "nodes", {}) or {}).values()
        if getattr(n, "type", "") == "evidence"
        and getattr(n, "created_by", "") == "reply"
        and (getattr(n, "content", "") or "").strip()
    ]
    return (replies[-1].content or "").strip() if replies else ""


def propagate_work_outcome(store, work_id: str, outcome: str) -> None:
    """Work object reached done/abandoned: if it carries concern provenance, write
    the outcome into the concerns register and trigger a noticer rerun."""
    try:
        wo = store.load(work_id)
        refs = [str(r).strip() for r in ((wo.constraints or {}).get("concern_refs") or [])
                if str(r).strip()]
        if not refs:
            return
        user_words = _last_user_reply(wo)

        from app.assistant.subconscious.persist import apply_work_outcome
        results = [
            apply_work_outcome(ref, work_id=work_id, outcome=outcome, user_words=user_words)
            for ref in refs
        ]
        logger.info("[concern_feedback] %s (%s) -> register: %s",
                    work_id, outcome, ", ".join(results))

        from app.assistant.subconscious.answer_capture import trigger_noticer
        trigger_noticer(reason=f"work_outcome:{work_id}:{outcome}")
    except Exception as e:
        logger.error("[concern_feedback] propagation failed for %s (%s): %s",
                     work_id, outcome, e)
        logger.debug("[concern_feedback] propagation exception", exc_info=True)
