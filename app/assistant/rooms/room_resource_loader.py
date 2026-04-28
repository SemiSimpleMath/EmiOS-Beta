import json
import re
from pathlib import Path
from typing import Any, Dict


_SAFE_ROOM_ID_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")

# Mapping of room_id prefix (before "::") to the shared config directory
# under app/assistant/rooms/. Keep in sync with the _ROOM_PREFIX_MAP defined
# inside load_room_context_for_manager (same purpose, same entries).
_ROOM_CONFIG_PREFIX_MAP: dict[str, str] = {
    "task_spec": "task_create",
    "doc_editor": "doc_editor",
}


def resolve_room_config_dir(room_id: str) -> Path:
    """Return the filesystem directory holding the room's JSON config files.

    Same logic that ``load_room_context_for_manager`` uses internally,
    exposed so other services can read a room's ``policy.json``, ``access.json``,
    etc. without re-implementing the prefix / segment-safety rules.
    """
    if not isinstance(room_id, str) or not room_id.strip():
        raise ValueError("room_id must be a non-empty string.")
    room_id = room_id.strip()
    if not _SAFE_ROOM_ID_RE.match(room_id):
        raise ValueError(f"Invalid room_id: {room_id!r}")

    config_room_id = room_id
    if "::" in room_id:
        prefix = room_id.split("::", 1)[0]
        config_room_id = _ROOM_CONFIG_PREFIX_MAP.get(prefix, prefix)

    parts = config_room_id.split("/")
    if any((not p or p in {".", ".."}) for p in parts):
        raise ValueError(f"Invalid room_id path segments: {room_id!r}")

    return Path(__file__).resolve().parent.joinpath(*parts)


def load_room_policy(room_id: str) -> Dict[str, Any]:
    """Read and return the room's ``policy.json`` dict.

    Used by system-internal callers (orchestrators, schedulers) that need the
    room's authority/delivery/retention policy when they build a ScopeContext
    outside the room_session_manager path.
    """
    base_dir = resolve_room_config_dir(room_id)
    return _read_json(base_dir / "policy.json")

# Canonical resource files and the blackboard key each maps to.
# Order determines the load sequence; later entries can extend or override earlier ones.
_RESOURCE_MAP: list[tuple[str, str]] = [
    ("resource_identity.json",        "room_identity"),
    ("resource_room_context.json",    "room_identity"),   # appended to identity block
    ("resource_conversation.json",    "room_conversation"),
    ("resource_safety.json",          "room_safety"),
    ("resource_room_facts.json",      "room_facts"),
    ("resource_participant_facts.json", "room_participant_facts"),
]


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Missing required room file: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"Expected JSON object in {path}")
        return data
    except Exception:
        raise


def _extract_content(path: Path) -> str:
    data = _read_json(path)
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"Room resource file missing 'content' string: {path}")
    return content.strip()


def load_room_context_for_manager(room_id: str) -> Dict[str, Any]:
    """
    Load room-scoped resources from `app/assistant/rooms/<room_id>/`.

    Reads the canonical resource_*.json contract files and maps them to
    the blackboard keys agents expect:
      room_identity          - resource_identity.json + resource_room_context.json (appended)
      room_conversation      - resource_conversation.json
      room_safety            - resource_safety.json
      room_facts             - resource_room_facts.json
      room_participant_facts - resource_participant_facts.json

    Structural config files (policy.json, permissions.json, access.json) are
    parsed for policy enforcement and not injected as text.

    Raises loudly on any missing required file or malformed content.
    """
    if not isinstance(room_id, str) or not room_id.strip():
        return {}

    base_dir = resolve_room_config_dir(room_id)
    room_id = room_id.strip()
    parts = base_dir.relative_to(Path(__file__).resolve().parent).parts

    policy = _read_json(base_dir / "policy.json")
    permissions = _read_json(base_dir / "permissions.json")
    access = _read_json(base_dir / "access.json")

    # Accumulate text content into blackboard keys.
    # resource_identity and resource_room_context are both appended to room_identity.
    blackboard: Dict[str, str] = {}

    for filename, bb_key in _RESOURCE_MAP:
        file_path = base_dir / filename
        if not file_path.exists():
            # Optional files are allowed to be absent; required ones will raise below.
            continue
        content = _extract_content(file_path)
        if bb_key in blackboard:
            blackboard[bb_key] = blackboard[bb_key] + "\n\n" + content
        else:
            blackboard[bb_key] = content

    # room_identity is required — every room must tell the agent who it is.
    if not blackboard.get("room_identity"):
        raise FileNotFoundError(
            f"Room '{room_id}' has no identity content. "
            f"Add resource_identity.json and/or resource_room_context.json."
        )

    policy_id = policy.get("policy_id") if isinstance(policy, dict) else None
    if not isinstance(policy_id, str) or not policy_id.strip():
        policy_id = f"room_policy::{room_id}"

    room_surface = policy.get("surface") if isinstance(policy, dict) else None
    room_visibility = policy.get("default_visibility") if isinstance(policy, dict) else None
    room_manager_name = policy.get("manager_name") if isinstance(policy, dict) else None
    if not isinstance(room_manager_name, str) or not room_manager_name.strip():
        room_manager_name = "room_manager"
    # Prefer the display_name from policy.participant_identity (e.g. "Katy"),
    # falling back to a cleaned-up version of the room_id path.
    participant_identity = policy.get("participant_identity") if isinstance(policy, dict) else None
    room_contact_name = ""
    if isinstance(participant_identity, dict):
        room_contact_name = str(participant_identity.get("display_name") or "").strip()
    if not room_contact_name:
        contact_seed = parts[-1] if parts else room_id
        room_contact_name = re.sub(r"[_\-]+", " ", contact_seed).strip().title()
    if not room_contact_name:
        room_contact_name = "User"

    return {
        "room_id": room_id,
        "room_contact_name": room_contact_name,
        "room_surface": room_surface or "unknown",
        "room_context_id": policy.get("default_context_id") if isinstance(policy, dict) else None,
        "room_visibility": room_visibility or "room_shared",
        "room_policy_id": policy_id,
        "room_manager_name": room_manager_name.strip(),
        "room_policy": policy,
        "room_permissions": permissions,
        "room_access": access,
        # Text blocks injected into agent prompts
        "room_identity":           blackboard.get("room_identity", ""),
        "room_conversation":       blackboard.get("room_conversation", ""),
        "room_safety":             blackboard.get("room_safety", ""),
        "room_facts":              blackboard.get("room_facts", ""),
        "room_participant_facts":  blackboard.get("room_participant_facts", ""),
        # Access control lists
        "room_allow_images":           bool(permissions.get("allow_images", False)) if isinstance(permissions, dict) else False,
        "room_allowed_global_resources": access.get("allowed_global_resources", []) if isinstance(access, dict) else [],
        "room_allowed_entity_cards":     access.get("allowed_entity_cards", []) if isinstance(access, dict) else [],
        "room_pinned_entities":          access.get("pinned_entities", []) if isinstance(access, dict) else [],
        "room_blocked_entities":         access.get("blocked_entities", []) if isinstance(access, dict) else [],
        "room_rag_scopes":               access.get("rag_scopes", []) if isinstance(access, dict) else [],
        "room_shared_chat_room_ids":     access.get("shared_chat_room_ids", []) if isinstance(access, dict) else [],
    }
