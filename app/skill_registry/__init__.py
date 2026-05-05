"""Skill registry — agentskills.io standard SKILL.md loading + indexing.

Public surface:

  from app.skill_registry import SkillRegistry, Skill, SkillHeader

The registry is registered in DI as ``DI.skill_registry``. See
``docs/architecture/21_SKILLS.md`` for the design.
"""
from app.skill_registry.models import Skill, SkillHeader, ValidationResult
from app.skill_registry.skill_registry import SkillRegistry

__all__ = ["Skill", "SkillHeader", "ValidationResult", "SkillRegistry"]
