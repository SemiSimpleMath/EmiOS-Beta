# Do NOT add `from __future__ import annotations` — PEP 563 string-ifies
# nested model references; OpenAI's structured-output `parse` API can't
# resolve them at schema-generation time. See
# http_request/tool_forms/tool_forms.py for the full explanation.

from typing import Any, Optional, List, Dict

from pydantic import BaseModel


class create_dayflow_ticket_args(BaseModel):
    ticket_kind: str
    suggestion_type: str
    title: str
    message: str
    action_type: str = "none"
    action_params: dict[str, Any] | None = None
    trigger_context: dict[str, Any] | None = None
    trigger_reason: str | None = None
    valid_hours: int = 4
    # How long this call BLOCKS waiting for the answer. It was absent from this form
    # entirely, so an LLM-routed call could not set it and was locked to the 600s default
    # while asking for a 4-hour ticket — the tool expires the ticket when the wait ends, so
    # the stated validity was silently cut to ten minutes. Keep it consistent with
    # valid_hours; the dayflow lane drives both from ASK_WINDOW_HOURS for exactly this
    # reason. The tool logs a warning when they disagree.
    wait_timeout_seconds: int = 600
    status_effect: list[str] | None = None
    speak_tts: bool = True


class create_dayflow_ticket_arguments(BaseModel):
    tool_name: str
    arguments: create_dayflow_ticket_args
