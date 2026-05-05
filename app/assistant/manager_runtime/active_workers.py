"""Active-worker resolver: walk the manager_invoker's invocation registry
and answer "who's running right now, addressable by name?"

Used by the @mention path in master_room: when the user types
``@quimby check the savory page``, the parser asks this module
"is Quimby running?" and gets back an invocation_id (or None).

Today the registry is a flat dict (manager_invoker._active_invocations).
There's no parent-child chain tracked, so a "single chain" is implied —
if multiple invocations of the same manager are running concurrently,
this resolver returns the most-recently-started one. Disambiguation by
context can be added later if/when forks become common.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.assistant.chat_narrator.display_names import (
    display_name_for,
    manager_for_display_name,
)
from app.assistant.ServiceLocator.service_locator import DI


def list_active_workers() -> List[Dict[str, Any]]:
    """Return active invocations annotated with their display names.

    Each entry: ``{invocation_id, manager_name, display_name, request_id,
    started_at_utc, running_for_s}``. Sorted by start time (oldest first)
    so callers can deterministically pick "most recent" with [-1].
    """
    invoker = getattr(DI, "manager_invoker", None)
    if invoker is None:
        return []
    try:
        status = invoker.get_invocation_status()
    except Exception:
        return []
    rows = status.get("active_invocations") or []
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        manager_name = str(row.get("manager_name") or "").strip()
        if not manager_name:
            continue
        out.append({
            "invocation_id": row.get("invocation_id"),
            "manager_name": manager_name,
            "display_name": display_name_for(manager_name),
            "request_id": row.get("request_id"),
            "started_at_utc": row.get("started_at_utc"),
            "running_for_s": row.get("running_for_s"),
        })
    return out


def find_active_invocation_by_display_name(name: str) -> Optional[Dict[str, Any]]:
    """Return the active invocation matching the display name, or None.

    "Quimby" / "quimby" / "QUIMBY" all work (case-insensitive match).
    If multiple invocations of the same manager are running, returns the
    most recently started one — single-chain assumption.

    Returns the same dict shape as list_active_workers entries.
    """
    if not name:
        return None
    target_manager = manager_for_display_name(name)
    if not target_manager:
        return None
    matches = [
        w for w in list_active_workers()
        if w["manager_name"] == target_manager
    ]
    if not matches:
        return None
    # list_active_workers sorts oldest first; pick most recent.
    return matches[-1]


def list_active_display_names() -> List[str]:
    """Return display names of currently-active workers, sorted, deduplicated.

    Useful for "who can I address right now?" UIs and for the @mention
    parser's "no active <name>" fallback message.
    """
    seen = []
    for w in list_active_workers():
        dn = w.get("display_name")
        if dn and dn not in seen:
            seen.append(dn)
    return seen
