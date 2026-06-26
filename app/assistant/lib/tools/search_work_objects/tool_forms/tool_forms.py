# Do NOT add `from __future__ import annotations` — PEP 563 string-ifies nested model references and
# OpenAI's structured-output parse API can't resolve them at schema-generation time.

from pydantic import BaseModel


class search_work_objects_args(BaseModel):
    query: str
    limit: int = 10


class search_work_objects_arguments(BaseModel):
    tool_name: str
    arguments: search_work_objects_args
