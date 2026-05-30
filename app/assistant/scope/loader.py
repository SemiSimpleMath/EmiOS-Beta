"""Unified scope loader.

The single entry point the unified-scope refactor converges every
scope-construction site onto — rooms first, then routines, pipelines, and
maintenance jobs. See ``docs/architecture/SCOPE_AUDIT.md`` and the
``project_unified_scope_design`` memory for the end-state design.

Model — one schema, no merging
------------------------------
A source's ``scope.yaml`` is a *partial* :class:`ScopeContext` carrying only
POLICY (the declarable sub-policies plus the stable ``surface`` / ``visibility``
identity-ish fields). There is no base file, no overlay, no inheritance between
scope files: each source declares its own complete scope.

Per-request IDENTITY (``scope_id``, ``owner_id``, ``actor_id``, ``room_id``,
``room_context_id``, ``reply_to``, ``acting_as``, ``policy_id``) is NOT authored
in the file — it is stamped here from the ``identity`` envelope the caller
passes at load time. A file may not spoof identity; identity keys found in the
file are dropped with a warning.

Fail-closed floor
-----------------
A source that declares no ``tools.allowed_tools`` gets an EMPTY allowed surface
(``[]``), not the permissive ``["all"]`` model default. Grants exist only where
the source explicitly writes them. (The ``["all"]`` default still serves
deliberately-permissive system/courier scopes constructed in code; it is only
the *file* path that is fail-closed.)
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, Mapping

import yaml

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ScopeContext

logger = get_logger(__name__)


# Per-request runtime identity. A scope.yaml may NOT author these — they are
# stamped from the identity envelope at load time. ``surface`` / ``visibility``
# are deliberately NOT here: they are stable source properties a file may
# declare, though identity may still override them.
_IDENTITY_FIELDS: frozenset = frozenset(
    {
        "scope_id",
        "owner_id",
        "actor_id",
        "room_id",
        "room_context_id",
        "reply_to",
        "acting_as",
        "policy_id",
    }
)

# Identity fields the caller MUST provide. ScopeContext declares these as
# required non-Optional strings with no default. ``scope_id`` is generated
# when absent, so it is not required from the caller.
_REQUIRED_IDENTITY: tuple = ("owner_id", "actor_id", "surface")


def load_scope(
    source: "str | Path | Mapping[str, Any]",
    *,
    identity: Mapping[str, Any],
) -> ScopeContext:
    """Load a scope declaration and stamp runtime identity onto it.

    Parameters
    ----------
    source
        Path to a ``scope.yaml`` file, or an already-parsed mapping (the dict
        form is used by tests and by callers that hold the declaration in
        memory).
    identity
        Per-request runtime fields to stamp. MUST include ``owner_id``,
        ``actor_id`` and ``surface``. MAY include ``scope_id`` (generated when
        omitted), ``room_id``, ``room_context_id``, ``reply_to``,
        ``acting_as``, ``policy_id``, ``visibility``.

    Returns
    -------
    ScopeContext
        Validated runtime scope.

    Raises
    ------
    FileNotFoundError
        When ``source`` is a path that does not exist.
    ValueError
        On a non-mapping file, missing required identity, or any schema
        violation (unknown keys are rejected by ``extra="forbid"``).
    """
    if not isinstance(identity, Mapping):
        raise ValueError("load_scope: identity must be a mapping.")

    declared = _read_declaration(source)
    declared = _strip_identity_fields(declared, source=source)
    declared = _apply_fail_closed_floor(declared)

    merged: Dict[str, Any] = dict(declared)
    for key, value in identity.items():
        merged[key] = value

    _require_identity(merged)
    merged.setdefault(
        "scope_id",
        f"scope::{merged.get('surface') or 'system'}::{merged.get('owner_id')}::{uuid.uuid4().hex[:8]}",
    )

    try:
        return ScopeContext.model_validate(merged)
    except Exception as e:
        src = source if isinstance(source, (str, Path)) else "<in-memory mapping>"
        raise ValueError(f"load_scope: invalid scope declaration from {src}: {e}") from e


# ── helpers ─────────────────────────────────────────────────────────────────


def _read_declaration(source: "str | Path | Mapping[str, Any]") -> Dict[str, Any]:
    if isinstance(source, Mapping):
        return dict(source)
    path = Path(source)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"load_scope: scope file not found: {path}")
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError(
            f"load_scope: {path} must contain a YAML mapping, got {type(parsed).__name__}."
        )
    return parsed


def _strip_identity_fields(declared: Dict[str, Any], *, source: Any) -> Dict[str, Any]:
    present = [k for k in declared if k in _IDENTITY_FIELDS]
    if not present:
        return declared
    src = source if isinstance(source, (str, Path)) else "<in-memory mapping>"
    logger.warning(
        "load_scope: scope declaration %s authored identity field(s) %s — "
        "identity is stamped at load time; these declared values are ignored.",
        src,
        sorted(present),
    )
    return {k: v for k, v in declared.items() if k not in _IDENTITY_FIELDS}


def _apply_fail_closed_floor(declared: Dict[str, Any]) -> Dict[str, Any]:
    """Omitting ``tools.allowed_tools`` means NO tools, not all tools.

    Only ``tools.allowed_tools`` needs this — every other sub-policy's model
    default is already restrictive (``pods`` -> ``["self"]``, ``resources`` ->
    ``[]``, ``approval.authority_level`` -> 0, ``writes.write_kg`` -> False).
    A non-dict ``tools`` value is left untouched so ``model_validate`` raises
    on it loudly.
    """
    out = dict(declared)
    raw_tools = out.get("tools", None)
    if raw_tools is None:
        out["tools"] = {"allowed_tools": []}
        return out
    if isinstance(raw_tools, dict):
        tools = dict(raw_tools)
        if "allowed_tools" not in tools:
            tools["allowed_tools"] = []
        out["tools"] = tools
    return out


def _require_identity(merged: Mapping[str, Any]) -> None:
    missing = [
        f
        for f in _REQUIRED_IDENTITY
        if not (isinstance(merged.get(f), str) and merged.get(f).strip())
    ]
    if missing:
        raise ValueError(
            f"load_scope: identity is missing required field(s): {missing}. "
            "owner_id, actor_id and surface must be provided by the caller."
        )
