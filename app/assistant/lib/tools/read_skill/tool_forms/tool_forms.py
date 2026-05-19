"""Pydantic args for read_skill.

NOTE: do NOT add `from __future__ import annotations` — PEP 563 stringifies
nested Pydantic refs and breaks OpenAI's `parse` schema codegen.
"""
from typing import List

from pydantic import BaseModel, Field


class read_skill_args(BaseModel):
    names: List[str] = Field(
        ...,
        description=(
            "Skill names to load (e.g. ['image-resize', 'pod-courier']). "
            "Get candidate names from discover_skills. Cap of 5 per call."
        ),
    )


class read_skill_arguments(BaseModel):
    tool_name: str
    arguments: read_skill_args


read_skill_arguments.model_rebuild()
