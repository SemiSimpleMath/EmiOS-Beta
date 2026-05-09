"""Routine handler that dispatches camera-snapshot events.

Wraps the existing camera_dispatcher logic so it can be invoked as a
declarative event-triggered routine instead of a hand-registered
event_hub subscriber. The corresponding routine entry in
configs/routines.json:

    {
      "id": "camera_dispatch",
      "trigger": { "type": "event", "topic": "ring_snapshot_captured" },
      "runner": "function",
      "spec": { "function_name": "camera_dispatch" }
    }

When the routine system fires this on a `ring_snapshot_captured` event,
it passes the original Message through as `event_message`. We hand
that straight to the existing dispatcher, which knows how to look up
the camera by id, run the analyzer, write per-camera storage, evaluate
pod policy, and run post-handlers.
"""
from __future__ import annotations

from typing import Any, Optional

from app.assistant.routine_handlers import routine_handler
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


@routine_handler(name="camera_dispatch")
def camera_dispatch(
    *,
    target_date: Optional[str] = None,
    routine: Any = None,
    event_message: Any = None,
) -> None:
    """Delegate to camera_dispatcher._on_camera_event for the triggering event."""
    if event_message is None:
        logger.warning(
            "[camera_dispatch] fired with no event_message; expected from "
            "trigger.type=event. Ignoring."
        )
        return
    from app.assistant.ring_analysis.camera_dispatcher import _on_camera_event
    _on_camera_event(event_message)
