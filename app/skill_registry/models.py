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


class AutoInjectTrigger(BaseModel):
    """Triggers that cause a skill to be auto-injected into an agent's prompt.

    The skill carries its own trigger conditions in frontmatter metadata
    under ``auto_inject_when``. Any agent with ``accept_auto_skills: true``
    in its config receives matching skills based on the current task /
    incoming message / room context / etc. — no per-agent keyword lists.

    Per the agentskills.io spec, ``metadata`` is free-form — putting this
    here is spec-compliant. Other clients (Claude Code, Codex CLI) just
    ignore the field.

    v3 supports ``task_keywords`` only. Other triggers (``room_surface``,
    ``pod_kinds``, ``url_patterns``, etc.) ride in as use cases appear.
    """

    task_keywords: List[str] = Field(
        default_factory=list,
        description=(
            "Lowercase substrings; if any appears in the agent's task or "
            "incoming_message (case-insensitive), the skill auto-injects."
        ),
    )
    requires_scope_acting_as: Optional[str] = Field(
        default=None,
        description=(
            "AND-conjunction gate. When set, keyword match is only sufficient "
            "if scope.acting_as equals this principal name. Use for persona-"
            "specific skills (emi-bluesky-voice, emi-values, etc.) that must "
            "not leak into other principals' planner contexts even when the "
            "keyword would otherwise match. Leave None for principal-agnostic "
            "skills like the technical bluesky API recipe. "
            "SUGAR for requires_scope={'acting_as': <value>}."
        ),
    )
    requires_scope: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Generic scope gate (AND-conjunction): the skill injects only when "
            "EVERY listed field matches the live ScopeContext. 'Scope is the key, "
            "the skill carries the lock.' Allowed fields (identity/context bucket "
            "only): acting_as, surface, room_id, room_context_id, visibility. "
            "Permission fields (tools/pods/approval/writes/...) are FORBIDDEN — "
            "skills gate relevance, never authorization. Each field is matched "
            "via its own canonicalizer (acting_as via resolve_principal, etc.). "
            "requires_scope_acting_as folds into this under the 'acting_as' key."
        ),
    )


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

    # Auto-inject triggers parsed from metadata.auto_inject_when. None means
    # the skill is static-binding only — agents must declare it explicitly
    # in their config.skills list to receive it.
    auto_inject_when: Optional[AutoInjectTrigger] = Field(default=None)

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
