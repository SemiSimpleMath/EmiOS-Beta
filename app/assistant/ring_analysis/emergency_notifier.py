"""EmergencyNotifier — the missing consumer for bedroom_emergency_detected.

The sleep-monitoring post-handler (camera_post_handlers.bedroom_emergency_alarm)
detects a distress/fall-type frame (importance >= 10), writes an EMERGENCY
sidecar, and publishes ``bedroom_emergency_detected`` "for any downstream
subscriber". Until now NOTHING subscribed (EventHub audit E1) — the alert
existed only as a JPEG + sidecar on disk and reached nobody. This is the
consumer: it surfaces the emergency as a durable, high-priority owner
ticket (the same propose_notice_ticket channel the delivery layer uses for
undeliverable reminders), so the alert lands in the UI popup / pending
poll the moment a client is live.

Registered once at boot (initialize_system), same pattern as the other
event-subscriber services (EmiEventRelay, ChatNarrator).
"""
from __future__ import annotations

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)

_EVENT_TOPIC = "bedroom_emergency_detected"


class EmergencyNotifier:
    def __init__(self) -> None:
        DI.event_hub.register_event(_EVENT_TOPIC, self._on_emergency)
        logger.info("✅ EmergencyNotifier initialized (topic=%s).", _EVENT_TOPIC)

    def _on_emergency(self, message: Message) -> None:
        data = message.data if isinstance(getattr(message, "data", None), dict) else {}
        description = str(data.get("description") or message.content or "").strip() or "(no description)"
        camera_name = str(data.get("camera_name") or data.get("camera_id") or "the bedroom camera").strip()
        when = str(data.get("detected_at_local") or data.get("captured_at_utc") or "").strip()

        reason = str(data.get("importance_reason") or "").strip()
        body = f"Emergency detected on {camera_name}: {description}"
        if reason:
            body += f" ({reason})"
        if when:
            body += f" — {when}"

        # Best-effort by contract (propose_notice_ticket never raises); a
        # notification failure must not sink the emergency handler's other
        # side effects (log + sidecar already happened upstream).
        from app.assistant.ticket_manager.ticket_service import propose_notice_ticket
        ticket_id = propose_notice_ticket(
            title="⚠️ Bedroom emergency detected",
            message=body,
            suggestion_type="bedroom_emergency",
            trigger_reason="sleep_analyzer flagged importance>=10",
            ticket_type="dayflow_notify",
            valid_hours=168,  # a week — an emergency notice must not silently expire
            trigger_context={
                "camera_id": data.get("camera_id"),
                "frame": data.get("frame"),
                "sidecar": data.get("sidecar"),
            },
        )
        if ticket_id:
            logger.critical(
                "[emergency_notifier] surfaced emergency ticket %s for camera=%s",
                ticket_id, camera_name,
            )
        else:
            logger.critical(
                "[emergency_notifier] emergency detected on %s but ticket channel "
                "unavailable — alert only in logs + sidecar: %s",
                camera_name, body,
            )
