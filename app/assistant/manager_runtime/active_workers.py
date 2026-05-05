"""Active-worker lookups — thin wrapper over MAMInstanceManager.

Kept as a module so existing import paths in ChatNarrator,
RoomMentionRouter, etc. don't break, and so the canonical_manager_name
helper has a stable home (used in places that don't have an MAM record
in hand yet — e.g. log lines that strip the UUID suffix).

All real work lives in ``DI.mam_instance_manager``. This module just
forwards.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.assistant.ServiceLocator.service_locator import DI


# Manager invocations from manager_interface get a UUID suffix
# ``_<8 hex chars>`` appended to the canonical type name (e.g.
# ``web_manager_55d613a4``). Strip it for lookup against type-keyed
# registries (display-name registry, manager_registry).
_INVOCATION_SUFFIX_RE = re.compile(r"_[0-9a-f]{8}$")


def canonical_manager_name(raw: str) -> str:
    """Strip the ``_xxxxxxxx`` invocation suffix to get the manager TYPE."""
    return _INVOCATION_SUFFIX_RE.sub("", str(raw or ""))


def list_active_workers(*, room_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return active manager records as JSON-shaped dicts.

    Each entry: ``{invocation_id, manager_name, display_name,
    base_display_name, room_id, request_id, started_at_utc, running_for_s}``.
    Sorted oldest first.

    ``room_id`` filter scopes to a single room — used by the @mention
    router so callers only see invocations they can actually address.
    """
    mam = getattr(DI, "mam_instance_manager", None)
    if mam is None:
        return []
    payload = mam.get_status_payload()
    rows = payload.get("active_invocations") or []
    if room_id is not None:
        rows = [r for r in rows if r.get("room_id") == room_id]
    return rows


def find_active_invocation_by_display_name(
    name: str, *, room_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve an exact display_name (e.g. ``Webby_2``) to an active
    invocation, optionally scoped to a room.

    For bare-name lookup with ambiguity reporting (multiple Webbys), use
    ``find_active_invocations_by_base_display_name``.
    """
    mam = getattr(DI, "mam_instance_manager", None)
    if mam is None or not name:
        return None
    record = mam.find_by_display_name(name, room_id=room_id)
    if record is None:
        return None
    return {
        "invocation_id": record.invocation_id,
        "manager_name": record.manager_name,
        "display_name": record.display_name,
        "base_display_name": record.base_display_name,
        "room_id": record.room_id,
        "request_id": record.request_id,
        "started_at_utc": record.started_at_utc.isoformat(),
    }


def find_active_invocations_by_base_display_name(
    base_display_name: str, *, room_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """All active invocations sharing a base display name (``Webby``,
    ``Webby_2``, ``Webby_3`` all match base ``Webby``). Used by the
    @mention router to detect ambiguity when the user types bare ``@webby``.
    """
    mam = getattr(DI, "mam_instance_manager", None)
    if mam is None or not base_display_name:
        return []
    records = mam.find_by_base_display_name(base_display_name, room_id=room_id)
    return [
        {
            "invocation_id": r.invocation_id,
            "manager_name": r.manager_name,
            "display_name": r.display_name,
            "base_display_name": r.base_display_name,
            "room_id": r.room_id,
        }
        for r in records
    ]


def list_active_display_names(*, room_id: Optional[str] = None) -> List[str]:
    """Display names of currently-active workers, sorted, deduplicated.

    Useful for "who can I address right now?" UIs and for the @mention
    parser's "no active <name>" hint message.
    """
    seen: list[str] = []
    for w in list_active_workers(room_id=room_id):
        dn = w.get("display_name")
        if dn and dn not in seen:
            seen.append(dn)
    return seen
