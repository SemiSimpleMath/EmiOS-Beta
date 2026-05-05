"""Pydantic models for the skill registry.

Mirror the agentskills.io SKILL.md frontmatter spec (see
``docs/architecture/21_SKILLS.md``). ``Skill`` is the full record (metadata
+ body); ``SkillHeader`` is metadata-only for tier-1 progressive disclosure
(name + description always in context for triggering, body loaded only on
activation).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SkillHeader(BaseModel):
    """Metadata-only view of a skill — the always-in-context tier.

    Per agentskills.io progressive disclosure: agents have all skill
    headers in context (~100 tokens each) for triggering, and only load
    full bodies when a skill activates.
    """

    name: str = Field(..., description="Unique identifier (lowercase a-z 0-9 -, 1-64 chars).")
    description: str = Field(..., description="What the skill does and when to use it (1-1024 chars).")
    license: Optional[str] = Field(default=None)
    compatibility: Optional[str] = Field(default=None)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    allowed_tools: Optional[str] = Field(default=None, description="Experimental — space-separated tool list.")

    # Where on disk this skill lives. Not part of the spec; helpful for
    # logging and for the body-loader to find supporting files.
    skill_dir: Optional[str] = Field(default=None)


class Skill(SkillHeader):
    """Full skill record: header fields plus the markdown body.

    The body is the content after the YAML frontmatter, intended for
    direct injection into agent prompts (or further processing — e.g.
    progressive loading of references/scripts/assets).
    """

    body: str = Field(default="", description="Markdown body of SKILL.md (post-frontmatter).")


class ValidationResult(BaseModel):
    """Outcome of validating a skill against the agentskills.io rules.

    ``ok`` is True iff there are no ``errors``. Warnings are advisory and
    do not block loading.
    """

    ok: bool = Field(...)
    skill_dir: str = Field(default="")
    errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
