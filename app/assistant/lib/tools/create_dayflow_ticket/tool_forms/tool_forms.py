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
    status_effect: list[str] | None = None
    speak_tts: bool = True


class create_dayflow_ticket_arguments(BaseModel):
    tool_name: str
    arguments: create_dayflow_ticket_args
