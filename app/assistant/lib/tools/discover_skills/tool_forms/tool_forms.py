"""Pydantic args for discover_skills.

NOTE: do NOT add `from __future__ import annotations` — PEP 563 stringifies
nested Pydantic refs and breaks OpenAI's `parse` schema codegen.
"""
from typing import Optional

from pydantic import BaseModel, Field


class discover_skills_args(BaseModel):
    query: Optional[str] = Field(
        default="",
        description=(
            "Intent or keywords to match against skill names and descriptions. "
            "Omit to browse all registered skills. Examples: 'image', 'github api', "
            "'extract text from pdf', 'send slack message'."
        ),
    )
    limit: Optional[int] = Field(
        default=5,
        description="Max skills to return. Default 5, max 50.",
    )


class discover_skills_arguments(BaseModel):
    tool_name: str
    arguments: discover_skills_args


discover_skills_arguments.model_rebuild()
