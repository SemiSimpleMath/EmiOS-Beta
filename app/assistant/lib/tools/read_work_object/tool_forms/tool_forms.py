# Do NOT add `from __future__ import annotations` — PEP 563 string-ifies nested model references and
# OpenAI's structured-output parse API can't resolve them at schema-generation time.

from pydantic import BaseModel


class read_work_object_args(BaseModel):
    work_id: str


class read_work_object_arguments(BaseModel):
    tool_name: str
    arguments: read_work_object_args
