"""
context_memo.py — Compact internal memo produced after context_activation.

After the reasoning_agent stage completes, this module:
    1. Distils the full reasoning output into a short memo (context summary +
       suggested follow-up questions).
    2. Writes the memo to the global blackboard as a non-chat background message
       (sub_data_type=["context_activation"]) so ContextInjector can surface it.
    3. Exposes helpers used by ContextInjector to read and age out the memo, and
       by ConversationStarterStage to consume it when the room goes quiet.

Memo lifecycle
--------------
    created   — written by distil_and_write_memo() after pipeline completes
    injected  — ContextInjector prepends it to Emi's context on subsequent turns
    addressed — marked when Emi uses it (via mark_memo_addressed()) or when
                ConversationStarterStage fires the first proactive question
    expired   — TTL elapsed; ContextInjector silently drops it
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_MEMO_SUB_TYPE = "context_activation"
_DEFAULT_TTL_HOURS = 4


# ---------------------------------------------------------------------------
# Distillation
# ---------------------------------------------------------------------------

def distil_memo(
    *,
    reasoning_output: str,
    user_message: str,
    primary_user: str,
) -> str:
    """
    Use the memo_writer agent to turn reasoning output into a compact internal memo.

    Returns a plain-text memo with situation summary + suggested questions.
    Raises on agent failure — no silent fallback.
    """
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import Message

    agent = DI.agent_factory.create_agent("context_engine::memo_writer")
    agent.blackboard.update_state_value("primary_user", primary_user)
    agent.blackboard.update_state_value("user_message", user_message)
    agent.blackboard.update_state_value("reasoning_output", reasoning_output[:4000])

    msg = Message(agent_input={
        "primary_user": primary_user,
        "user_message": user_message,
        "reasoning_output": reasoning_output[:4000],
    })
    result = agent.action_handler(msg)
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        summary = str(data.get("situation_summary") or "").strip()
        questions = str(data.get("suggested_questions") or "").strip()
        return f"## Situation Summary\n{summary}\n\n## Suggested Questions\n{questions}"

    raise RuntimeError("memo_writer agent returned no structured data.")


# ---------------------------------------------------------------------------
# Blackboard write / read
# ---------------------------------------------------------------------------

def write_memo_to_blackboard(
    *,
    memo_text: str,
    brief_id: str,
    user_message: str,
    seeds: list[str],
) -> None:
    """
    Write the compact memo to the global blackboard as a background (non-chat) message.

    The message is NOT shown in the user-facing chat feed (is_chat=False).
    ContextInjector reads it and prepends it to Emi's context block.
    """
    from datetime import datetime, timezone
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import Message

    msg = Message(
        data_type="user_msg",
        sender="context_engine",
        content=memo_text,
        is_chat=False,
        role="assistant",
        sub_data_type=[_MEMO_SUB_TYPE],
        metadata={
            "source": "context_engine",
            "brief_id": brief_id,
            "user_message": user_message,
            "seeds": seeds,
            "addressed": False,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        },
    )
    DI.global_blackboard.add_msg(msg)
    logger.debug(
        "context_memo: wrote memo to blackboard brief_id=%s seeds=%r",
        brief_id,
        seeds,
    )


def get_active_memo(
    *,
    ttl_hours: float = _DEFAULT_TTL_HOURS,
) -> Optional[object]:
    """
    Return the most recent unaddressed context_activation memo from the blackboard,
    or None if none exists or all are expired / already addressed.
    """
    from app.assistant.ServiceLocator.service_locator import DI

    messages = DI.global_blackboard.get_messages()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)

    candidate = None
    candidate_ts: Optional[datetime] = None

    for msg in messages or []:
        sub_types = set(getattr(msg, "sub_data_type", []) or [])
        if _MEMO_SUB_TYPE not in sub_types:
            continue
        if bool(getattr(msg, "is_chat", True)):
            continue

        meta = getattr(msg, "metadata", None) or {}
        if bool(meta.get("addressed", False)):
            continue

        created_raw = meta.get("created_at_utc")
        if not isinstance(created_raw, str):
            continue
        try:
            created = datetime.fromisoformat(created_raw)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except Exception as e:
            logger.debug("context_memo: could not parse created_at_utc %r: %s", created_raw, e)
            continue

        if created < cutoff:
            continue

        if candidate_ts is None or created > candidate_ts:
            candidate = msg
            candidate_ts = created

    return candidate


def mark_memo_addressed(memo_msg: object) -> None:
    """
    Mark a memo as addressed so it is no longer injected or used for proactive questions.

    Mutates the message's metadata dict in-place (the blackboard holds object references).
    """
    meta = getattr(memo_msg, "metadata", None)
    if not isinstance(meta, dict):
        logger.debug("context_memo: mark_memo_addressed called on msg with no metadata dict")
        return
    meta["addressed"] = True
    meta["addressed_at_utc"] = datetime.now(timezone.utc).isoformat()
    logger.debug(
        "context_memo: marked memo addressed brief_id=%s",
        meta.get("brief_id", "?"),
    )
