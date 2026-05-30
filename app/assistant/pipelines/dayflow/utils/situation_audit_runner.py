"""
Situation Audit Runner

Periodic background audit of the user's active context.
Gathers a snapshot, calls the situation_auditor agent, and
notifies the user of any findings via chat.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def run_situation_audit() -> dict:
    """Run one audit cycle. Returns a summary dict."""
    from app.assistant.pipelines.dayflow.utils.situation_snapshot import build_situation_snapshot
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.scope.loader import load_scope_for_source
    from app.assistant.utils.pydantic_classes import Message, UserMessage, UserMessageData

    logger.info("[situation_audit] Starting audit cycle.")

    # 1. Build snapshot.
    try:
        snapshot = build_situation_snapshot()
    except Exception as e:
        logger.error("[situation_audit] Failed to build snapshot: %s", e)
        return {"status": "error", "error": f"snapshot_failed: {e}"}

    if not snapshot or not snapshot.strip():
        logger.info("[situation_audit] Empty snapshot, nothing to audit.")
        return {"status": "skipped", "reason": "empty_snapshot"}

    # 2. Call the auditor agent.
    try:
        scope_context = load_scope_for_source(kind="pipeline", source_id="dayflow", actor_id="situation_auditor_runner", identity_overrides={"room_id": "dayflow_orchestrator", "surface": ("ui") or "pipeline"})

        agent = DI.agent_factory.create_agent("situation_auditor")
        if not agent:
            raise RuntimeError("Failed to create situation_auditor agent")

        result = agent.action_handler(Message(
            agent_input={"task": snapshot, "information": ""},
            scope_context=scope_context,
        ))

        data = getattr(result, "data", None)
        if not isinstance(data, dict):
            logger.warning("[situation_audit] Agent returned non-dict: %s", type(data))
            return {"status": "error", "error": "agent_returned_non_dict"}

    except Exception as e:
        logger.error("[situation_audit] Agent call failed: %s", e)
        return {"status": "error", "error": f"agent_failed: {e}"}

    # 3. Process findings.
    findings = data.get("findings", [])
    overall_health = data.get("overall_health", "green")
    reasoning = data.get("reasoning", "")

    logger.info(
        "[situation_audit] Completed: health=%s, findings=%d",
        overall_health, len(findings),
    )

    # 4. Notify user if there are findings.
    if findings:
        _notify_user_of_findings(findings, overall_health)

    return {
        "status": "completed",
        "overall_health": overall_health,
        "findings_count": len(findings),
        "reasoning": reasoning[:200],
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }


def _notify_user_of_findings(findings: list, overall_health: str) -> None:
    """Send findings to the user via chat message."""
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.assistant_name import get_assistant_name
        from app.assistant.utils.pydantic_classes import UserMessage, UserMessageData

        # Build a concise message.
        icon = {"red": "🔴", "yellow": "🟡", "green": "🟢"}.get(overall_health, "⚪")
        lines = [f"{icon} **Situation Audit** — {len(findings)} finding(s):\n"]

        for f in findings[:5]:  # Cap at 5 to avoid wall of text.
            if isinstance(f, dict):
                severity = f.get("severity", "")
                summary = f.get("summary", "")
                sev_icon = {"high": "⚠️", "medium": "⚡", "low": "ℹ️"}.get(severity, "•")
                lines.append(f"{sev_icon} {summary}")
            elif hasattr(f, "summary"):
                severity = getattr(f, "severity", "")
                sev_icon = {"high": "⚠️", "medium": "⚡", "low": "ℹ️"}.get(severity, "•")
                lines.append(f"{sev_icon} {f.summary}")

        chat_text = "\n".join(lines)

        # Emit as a chat message.
        assistant_name = get_assistant_name()

        metadata = {"reply_to": {"type": "socketio", "room_id": "master_room"}}

        msg = UserMessage(
            data_type="user_msg",
            sender=assistant_name,
            receiver=None,
            content=chat_text,
            timestamp=datetime.now(timezone.utc),
            role="assistant",
            metadata=metadata,
            sub_data_type=["proactive", "situation_audit"],
            user_message_data=UserMessageData(feed=None, chat=chat_text, tts=False, tts_text=None),
        )
        msg.event_topic = "socket_emit"
        DI.event_hub.publish(msg)

        logger.info("[situation_audit] Notified user of %d findings.", len(findings))
    except Exception as e:
        logger.warning("[situation_audit] Failed to notify user: %s", e)
