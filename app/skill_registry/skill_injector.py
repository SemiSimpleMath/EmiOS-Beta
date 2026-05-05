"""SkillInjector — auto-attach skills based on per-skill trigger metadata.

Centralized replacement for the legacy ``task_keyword_resources`` per-agent
keyword lists. Each skill carries its own trigger conditions in
``metadata.auto_inject_when`` (parsed by the SkillRegistry into
``Skill.auto_inject_when``); the SkillInjector evaluates those conditions
against the current agent context and returns the names of skills that
should be added to the prompt.

v1 trigger: ``task_keywords`` (substring match against task +
incoming_message). Other triggers ride in as use cases appear:
``room_surface``, ``pod_kinds``, ``url_patterns``, etc.

Wired through ``context_injector.resolve_skills`` so static-bound and
auto-injected skills land in the same ``context['skills']`` dict —
templates render the same way regardless of how the skill arrived.
"""
from __future__ import annotations

from typing import List, Optional

from app.assistant.utils.logging_config import get_logger
from app.skill_registry.skill_registry import SkillRegistry

logger = get_logger(__name__)


class SkillInjector:
    """Match skill auto_inject_when triggers against an agent's current context."""

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    def matching_skill_names(
        self,
        *,
        task: str = "",
        incoming_message: str = "",
    ) -> List[str]:
        """Return names of skills whose triggers match the given context.

        ``task`` and ``incoming_message`` are checked together (concatenated,
        lowercased) against each skill's ``auto_inject_when.task_keywords``.
        Substring match — the legacy ``task_keyword_resources`` semantic.

        Skills with no ``auto_inject_when`` block are static-bound only and
        are skipped here.
        """
        search_text = (str(task or "") + " " + str(incoming_message or "")).lower()
        if not search_text.strip():
            return []

        matches: List[str] = []
        for header in self._registry.headers():
            trigger = header.auto_inject_when
            if trigger is None:
                continue
            if not trigger.task_keywords:
                continue
            for kw in trigger.task_keywords:
                if kw and kw in search_text:
                    matches.append(header.name)
                    logger.debug(
                        "[skill_injector] matched skill=%s on keyword=%r",
                        header.name, kw,
                    )
                    break  # one keyword hit is enough; don't double-count
        return matches
