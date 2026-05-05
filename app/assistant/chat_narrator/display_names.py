"""Display-name registry for sub-managers.

Manager name → user-facing worker name. Centralized here so the chat
narrator (which renders progress narrations) and the @mention resolver
(which routes user steering by name) read from the same source of truth.

To add a new named worker: drop an entry into ``DISPLAY_NAMES`` below.
Long-running planner-loop managers benefit most from naming. Quick one-shot
tools should stay anonymous (they finish before the user can address them).

Names should be:
- Short, human, distinct
- Chosen, not generated (the affection users develop for them is the point)
- Stable — once a manager has a name, don't rename it casually
"""
from __future__ import annotations

from typing import Dict, Optional


DISPLAY_NAMES: Dict[str, str] = {
    "web_manager": "Quimby",
    "emi_team_manager": "Em",
    "personal_admin_manager": "Phyllis",
    "devices_manager": "Watt",
    "kg_team_manager": "Mnemo",
}


# Reverse lookup, computed once at import. ``@quimby`` (any case) → "web_manager".
_DISPLAY_TO_MANAGER: Dict[str, str] = {
    name.lower(): manager for manager, name in DISPLAY_NAMES.items()
}


def display_name_for(manager_name: str) -> str:
    """Return the user-facing display name for a manager.

    Falls back to a humanized form of the manager_name so unnamed managers
    show up visibly (signals "this worker still needs a name").
    """
    if not manager_name:
        return "Em"
    name = DISPLAY_NAMES.get(manager_name)
    if name:
        return name
    return manager_name.replace("_manager", "").replace("_", " ").title() or "Em"


def manager_for_display_name(display_name: str) -> Optional[str]:
    """Reverse: ``"Quimby"`` → ``"web_manager"``. Case-insensitive.

    Returns None if the name isn't in the registry. Used by the @mention
    resolver to find which manager a user is addressing.
    """
    if not display_name:
        return None
    return _DISPLAY_TO_MANAGER.get(display_name.strip().lower())


def all_named_managers() -> Dict[str, str]:
    """Return a copy of the full registry. Useful for UIs that want to show
    "the team" or for diagnostic endpoints listing who can be addressed."""
    return dict(DISPLAY_NAMES)
