# Do NOT add `from __future__ import annotations` — PEP 563 string-ifies nested model references and
# OpenAI's structured-output parse API can't resolve them at schema-generation time.

from pydantic import BaseModel


class run_work_node_args(BaseModel):
    work_id: str
    node_id: str


class run_work_node_arguments(BaseModel):
    tool_name: str
    arguments: run_work_node_args
