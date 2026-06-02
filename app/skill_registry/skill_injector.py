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
        scope_acting_as: Optional[str] = None,
        scope: object = None,
    ) -> List[str]:
        """Return names of skills whose triggers match the given context.

        A skill matches iff (AND-conjunction):
          1. ``auto_inject_when.task_keywords`` — any substring appears in
             ``task + incoming_message`` (case-insensitive); AND
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
                if kw and kw in search_text:
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

    # ── requires_scope gate ────────────────────────────────────────────
    _GATE_FIELDS = ("acting_as", "surface", "room_id", "room_context_id", "visibility")

    @staticmethod
    def _scope_identity(*, scope: object, scope_acting_as: Optional[str]) -> dict:
        """Extract the gateable identity fields from the live scope (object or
        dict). Falls back to scope_acting_as for the acting_as field when no
        scope is supplied (back-compat for callers that only have the principal)."""
        live: dict = {}
        if scope is not None:
            for fld in SkillInjector._GATE_FIELDS:
                if isinstance(scope, dict):
                    v = scope.get(fld)
                else:
                    v = getattr(scope, fld, None)
                if v is not None and str(v).strip():
                    live[fld] = str(v).strip()
        if "acting_as" not in live and scope_acting_as is not None and str(scope_acting_as).strip():
            live["acting_as"] = str(scope_acting_as).strip()
        return live

    @staticmethod
    def _canon_field(field: str, value: str) -> str:
        """Canonicalize a scope field value for gate comparison (per-field
        resolver). Both the skill's required value and the live value go through
        this, so a skill gating on acting_as=self matches any install's name."""
        v = str(value or "").strip().lower()
        if field == "acting_as":
            from app.assistant.utils.identity_names import resolve_principal
            return resolve_principal(v)
        # surface/room_id/room_context_id/visibility: lowercased exact for now.
        # (surface normalizer can be added here when surface aliases appear.)
        return v

    def _scope_gate_passes(self, requires_scope: dict, live: dict) -> bool:
        """True iff EVERY required field matches the live scope (canonicalized).
        Empty requires_scope → always passes (no gate)."""
        if not requires_scope:
            return True
        for field, required in requires_scope.items():
            live_val = live.get(field)
            if live_val is None:
                return False  # gated field absent from live scope → no match
            if self._canon_field(field, required) != self._canon_field(field, live_val):
                return False
        return True
