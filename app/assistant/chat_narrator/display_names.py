"""Display-name registry for sub-managers.

Manager name → user-facing worker name. Populated at boot from each
manager's ``config.yaml`` (``display_name: Quimby``). Centralized so the
chat narrator (which renders progress narrations) and the @mention
resolver (which routes user steering by name) read from the same source
of truth.

To name a new worker: add ``display_name: <Name>`` to its
``manager_config.yaml``. Re-register at boot picks it up automatically.

Long-running planner-loop managers benefit most from naming. Quick one-shot
tools should stay anonymous (they finish before the user can address them).
"""
from __future__ import annotations

import re
from typing import Dict, Optional


# Populated at boot by ``initialize_display_name_registry`` (called from
# initialize_system.py after ManagerRegistry.preload_all). Empty at import
# time. Tests can populate manually.
_FORWARD: Dict[str, str] = {}
_REVERSE: Dict[str, str] = {}

_MENTION_KEY_RE = re.compile(r"[^a-z0-9]+")


def mention_key(display_name: str) -> str:
    """The typeable @-handle for a display name: lowercase, non-alphanumeric
    runs collapsed to ``_``. ``"Waffle (graph)"`` → ``"waffle_graph"`` — the
    @mention regex only accepts ``[A-Za-z0-9_]``, so display names with spaces
    or punctuation are addressed by this key.
    """
    return _MENTION_KEY_RE.sub("_", str(display_name or "").strip().lower()).strip("_")


def mention_head(display_name: str) -> str:
    """The first word of the mention key — ``"Waffle (graph)"`` → ``"waffle"``.
    Used by BASE-name matching so a bare ``@waffle`` reaches the graph-mode
    Waffle when it's the only one active (ambiguity handling reports the
    explicit keys otherwise).
    """
    key = mention_key(display_name)
    return key.split("_", 1)[0] if key else ""


def manager_for_mention(name: str) -> Optional[str]:
    """Resolve a typed @-name to a manager: exact display match first, then
    mention-key match, then mention-head match over the registry. Returns the
    first manager whose display name the query can address, or None.
    """
    if not name:
        return None
    exact = manager_for_display_name(name)
    if exact is not None:
        return exact
    query = name.strip().lower()
    for manager_name, display in _FORWARD.items():
        if mention_key(display) == query:
            return manager_name
    for manager_name, display in _FORWARD.items():
        if mention_head(display) == query:
            return manager_name
    return None


def initialize_display_name_registry(name_to_display: Dict[str, str]) -> None:
    """Replace the registry contents. Called once at boot from manager
    configs; safe to call again if managers are reloaded.
    """
    _FORWARD.clear()
    _REVERSE.clear()
    for manager_name, display in (name_to_display or {}).items():
        if not isinstance(manager_name, str) or not manager_name.strip():
            continue
        if not isinstance(display, str) or not display.strip():
            continue
        m = manager_name.strip()
        d = display.strip()
        _FORWARD[m] = d
        _REVERSE[d.lower()] = m


def display_name_for(manager_name: str) -> str:
    """User-facing name for a manager.

    Source of truth is each manager's ``config.yaml`` ``display_name`` field,
    loaded into the registry at boot. If a manager isn't in the registry,
    return its raw ``manager_name`` so the gap is visible in chat
    ("``[web_manager] doing thing``" → "this manager needs a display_name
    in its config"). No magic humanization, no fallback to other workers'
    names.
    """
    if not manager_name:
        return ""
    return _FORWARD.get(manager_name, manager_name)


def manager_for_display_name(display_name: str) -> Optional[str]:
    """Reverse: ``"Quimby"`` → ``"web_manager"``. Case-insensitive.

    Returns None if the name isn't in the registry. Used by the @mention
    resolver to find which manager a user is addressing.
    """
    if not display_name:
        return None
    return _REVERSE.get(display_name.strip().lower())


def all_named_managers() -> Dict[str, str]:
    """Snapshot of the full registry — for diagnostic endpoints / "who can
    I address right now?" UIs."""
    return dict(_FORWARD)
