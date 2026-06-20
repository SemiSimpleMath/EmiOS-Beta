"""SkillInjector — auto-attach skills based on per-skill trigger metadata.

Centralized replacement for the legacy ``task_keyword_resources`` per-agent
keyword lists. Each skill carries its own trigger conditions in
``metadata.auto_inject_when`` (parsed by the SkillRegistry into
``Skill.auto_inject_when``); the SkillInjector evaluates those conditions
against the current agent context and returns the names of skills that
should be added to the prompt.

v1 trigger: ``task_keywords`` (whole-word/phrase match against task +
incoming_message — never a partial-word substring). Other triggers ride in
as use cases appear:
``room_surface``, ``pod_kinds``, ``url_patterns``, etc.

Wired through ``context_injector.resolve_skills`` so static-bound and
auto-injected skills land in the same ``context['skills']`` dict —
templates render the same way regardless of how the skill arrived.
"""
from __future__ import annotations

import re
from typing import List, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils import scope_gate
from app.skill_registry.skill_registry import SkillRegistry

logger = get_logger(__name__)


def _keyword_matches(keyword: str, text: str) -> bool:
    """Whole-word/phrase containment: ``keyword`` must appear in ``text`` bounded by
    non-word characters (or a string edge) on BOTH sides — so a short keyword like
    "pr" matches "open a pr" but NOT "dicaprio" or "prince". Skills must never trigger
    on a partial word. Both args arrive lowercased (search text by the caller, keywords
    at parse time); ``re`` memoizes the compiled pattern per keyword internally."""
    kw = (keyword or "").strip()
    if not kw:
        return False
    return re.search(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", text) is not None


class SkillInjector:
    """Match skill auto_inject_when triggers against an agent's current context."""

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    def matching_skill_names(
        self,
        *,
        task: str = "",
        incoming_message: str = "",
        scope_acting_as: Optional[str] = None,
        scope: object = None,
    ) -> List[str]:
        """Return names of skills whose triggers match the given context.

        A skill matches iff (AND-conjunction):
          1. ``auto_inject_when.task_keywords`` — any keyword appears as a
             whole word/phrase in ``task + incoming_message`` (case-insensitive,
             bounded by non-word chars — "pr" won't match "dicaprio"); AND
          2. ``auto_inject_when.requires_scope`` — EVERY listed field matches
             the live scope. "Scope is the key, the skill carries the lock."
             Allowed fields: acting_as, surface, room_id, room_context_id,
             visibility (identity/context only — permission fields are rejected
             at parse time). Each field is matched via its own canonicalizer
             (acting_as via resolve_principal).

        ``scope`` is the live ScopeContext (object or dict); the gate reads its
        identity fields. ``scope_acting_as`` is kept for back-compat / callers
        that only have the principal — it seeds the acting_as field when ``scope``
        is absent.

        Skills with no ``auto_inject_when`` block are static-bound only and
        are skipped here.
        """
        search_text = (str(task or "") + " " + str(incoming_message or "")).lower()
        if not search_text.strip():
            return []

        live = self._scope_identity(scope=scope, scope_acting_as=scope_acting_as)
        matches: List[str] = []
        for header in self._registry.headers():
            trigger = header.auto_inject_when
            if trigger is None:
                continue
            if not trigger.task_keywords:
                continue
            # Keyword check
            keyword_hit = False
            matched_kw = None
            for kw in trigger.task_keywords:
                if kw and _keyword_matches(kw, search_text):
                    keyword_hit = True
                    matched_kw = kw
                    break
            if not keyword_hit:
                continue
            # Generic requires_scope gate (AND over every required field).
            if not self._scope_gate_passes(trigger.requires_scope, live):
                logger.debug(
                    "[skill_injector] skill=%s matched keyword=%r but requires_scope gate "
                    "failed (required=%r, live=%r)",
                    header.name, matched_kw, dict(trigger.requires_scope), live,
                )
                continue
            matches.append(header.name)
            logger.debug(
                "[skill_injector] matched skill=%s on keyword=%r",
                header.name, matched_kw,
            )
        return matches

    def always_inject_skill_names(self, *, scope: object = None, scope_acting_as: Optional[str] = None) -> List[str]:
        """Return skills that are ALWAYS-ON for the given scope — i.e. they have a
        requires_scope gate that matches the live scope AND no task_keywords (so
        they're not keyword-triggered, they ride the whole task).

        This is the data-driven replacement for the hardcoded _PRINCIPAL_SKILL_PACKS:
        the scope builder calls this to populate scope.skills.always_inject, which
        propagates a skill across every downstream agent. A skill opts in by
        declaring, e.g.:
            auto_inject_when:
              requires_scope: { acting_as: self }   # and NO task_keywords
        Keyword-less + a scope gate = "always on whenever this scope is active."
        """
        live = self._scope_identity(scope=scope, scope_acting_as=scope_acting_as)
        out: List[str] = []
        for header in self._registry.headers():
            trigger = header.auto_inject_when
            if trigger is None:
                continue
            if trigger.task_keywords:
                continue  # keyword-triggered -> handled by matching_skill_names, not always-on
            if not trigger.requires_scope:
                continue  # no gate + no keywords -> not auto-injected at all
            if self._scope_gate_passes(trigger.requires_scope, live):
                out.append(header.name)
        return out

    def skill_gate_passes(self, skill, *, scope: object = None, scope_acting_as: Optional[str] = None) -> bool:
        """THE universal gate check: may this skill enter this scope?

        Every injection path — static config binding, keyword auto-inject,
        caller-supplied, scope-stamped, discoverable — MUST consult this before
        admitting a skill. A skill's requires_scope (from auto_inject_when) is the
        lock; the live scope is the key. A skill with no requires_scope passes
        trivially (generic skills are unrestricted). Canonicalization is per-field
        (acting_as via resolve_principal), so a 'self'-gated skill matches any
        install's assistant name.

        `skill` may be a Skill or SkillHeader (anything with .auto_inject_when).
        """
        trigger = getattr(skill, "auto_inject_when", None)
        req = getattr(trigger, "requires_scope", None) if trigger is not None else None
        if not req:
            return True
        live = self._scope_identity(scope=scope, scope_acting_as=scope_acting_as)
        return self._scope_gate_passes(req, live)

    # ── requires_scope gate ────────────────────────────────────────────
    _GATE_FIELDS = scope_gate.GATE_FIELDS  # back-compat alias

    @staticmethod
    def _scope_identity(*, scope: object, scope_acting_as: Optional[str]) -> dict:
        """Delegates to the shared scope-gate primitive (utils/scope_gate)."""
        return scope_gate.scope_identity(scope=scope, scope_acting_as=scope_acting_as)

    @staticmethod
    def _canon_field(field: str, value: str) -> str:
        """Delegates to the shared scope-gate primitive (utils/scope_gate)."""
        return scope_gate.canon_field(field, value)

    def _scope_gate_passes(self, requires_scope: dict, live: dict) -> bool:
        """Delegates to the shared scope-gate primitive (utils/scope_gate)."""
        return scope_gate.scope_gate_passes(requires_scope, live)
