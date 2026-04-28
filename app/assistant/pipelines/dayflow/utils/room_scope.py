from __future__ import annotations

from typing import Any, Dict, List


def resolve_room_scope(*, pipeline_config: Dict[str, Any], step_config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Resolve effective room scope for dayflow_ history access.

    Contract (fail-loud):
    - room_scope must exist either at step-level or pipeline-level
    - required keys: room_id, room_surface, manager_name, agent_name
    - room_surface may be "any" to disable surface filtering while still being explicit
    """
    step_cfg = step_config if isinstance(step_config, dict) else {}
    pipeline_cfg = pipeline_config if isinstance(pipeline_config, dict) else {}

    scope = step_cfg.get("room_scope")
    if scope is None:
        scope = pipeline_cfg.get("room_scope")
    if not isinstance(scope, dict):
        raise ValueError("dayflow_ room_scope must be configured as an object in pipeline or step config.")

    required = ("room_id", "room_surface", "manager_name", "agent_name")
    out: Dict[str, Any] = {}
    for key in required:
        raw = scope.get(key)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"dayflow_ room_scope.{key} must be a non-empty string.")
        out[key] = raw.strip()

    shared_ids_raw = scope.get("shared_chat_room_ids", [])
    if shared_ids_raw is None:
        shared_ids_raw = []
    if not isinstance(shared_ids_raw, list):
        raise ValueError("dayflow_ room_scope.shared_chat_room_ids must be a list when provided.")
    shared_ids: List[str] = []
    for item in shared_ids_raw:
        if not isinstance(item, str) or not item.strip():
            continue
        cleaned = item.strip()
        if cleaned == out["room_id"]:
            continue
        if cleaned not in shared_ids:
            shared_ids.append(cleaned)
    out["shared_chat_room_ids"] = shared_ids

    shared_surface = scope.get("shared_chat_room_surface", "any")
    if not isinstance(shared_surface, str) or not shared_surface.strip():
        raise ValueError("dayflow_ room_scope.shared_chat_room_surface must be a non-empty string when provided.")
    out["shared_chat_room_surface"] = shared_surface.strip()
    return out

