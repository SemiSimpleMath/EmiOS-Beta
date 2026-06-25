# Do NOT add `from __future__ import annotations` — PEP 563 string-ifies
# nested model references; OpenAI's structured-output `parse` API can't
# resolve them at schema-generation time. See
# http_request/tool_forms/tool_forms.py for the full explanation.

from pydantic import BaseModel


class create_work_notification_args(BaseModel):
    work_id: str
    node_id: str


class create_work_notification_arguments(BaseModel):
    tool_name: str
    arguments: create_work_notification_args
