"""Shared scope-gate primitive — "scope is the key, the thing carries the lock."

A skill OR a resource may declare a `requires_scope` lock: a dict of identity
fields (acting_as / surface / room_id / room_context_id / visibility). The lock
passes iff EVERY field matches the live scope (canonicalized per-field). An empty
or absent lock passes trivially — **no lock = free** (the thing is unrestricted).

Extracted from SkillInjector so skills and resources gate IDENTICALLY. Identity
fields only; permission fields (authority, tools, pods, …) are never gateable here.
"""
from __future__ import annotations

from typing import Any, Optional

GATE_FIELDS = ("acting_as", "surface", "room_id", "room_context_id", "visibility")


def scope_identity(*, scope: Any = None, scope_acting_as: Optional[str] = None) -> dict:
    """Extract the gateable identity fields from the live scope (object or dict).
    Falls back to `scope_acting_as` for the acting_as field when no scope is
    supplied (back-compat for callers that only have the principal)."""
    live: dict = {}
    if scope is not None:
        for fld in GATE_FIELDS:
            if isinstance(scope, dict):
                v = scope.get(fld)
            else:
                v = getattr(scope, fld, None)
            if v is not None and str(v).strip():
                live[fld] = str(v).strip()
    if "acting_as" not in live and scope_acting_as is not None and str(scope_acting_as).strip():
        live["acting_as"] = str(scope_acting_as).strip()
    return live


def canon_field(field: str, value: str) -> str:
    """Canonicalize a scope field value for gate comparison (per-field resolver).
    `acting_as` goes through `resolve_principal`, so a lock gating on
    acting_as=self matches any install's configured assistant name; the other
    fields are lowercased-exact for now."""
    v = str(value or "").strip().lower()
    if field == "acting_as":
        from app.assistant.utils.identity_names import resolve_principal
        return resolve_principal(v)
    return v


def scope_gate_passes(requires_scope: Optional[dict], live: dict) -> bool:
    """True iff EVERY required field matches the live scope (canonicalized).
    Empty/None requires_scope → always passes (no lock = free)."""
    if not requires_scope:
        return True
    for field, required in requires_scope.items():
        live_val = live.get(field)
        if live_val is None:
            return False  # gated field absent from live scope → no match
        if canon_field(field, required) != canon_field(field, live_val):
            return False
    return True


def gate_passes(requires_scope: Optional[dict], *, scope: Any = None,
                scope_acting_as: Optional[str] = None) -> bool:
    """Convenience: build the live identity from `scope` and check the lock.
    No lock → True."""
    if not requires_scope:
        return True
    live = scope_identity(scope=scope, scope_acting_as=scope_acting_as)
    return scope_gate_passes(requires_scope, live)
