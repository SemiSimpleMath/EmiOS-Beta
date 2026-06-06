"""Load a room's config + prose blocks from a single ROOM.md per room.

Each room directory contains exactly one file:

    app/assistant/rooms/<room_id>/ROOM.md

    ---
    policy:      <fields previously in policy.json>
    permissions: <fields previously in permissions.json>
    access:      <fields previously in access.json>
    ---

    # Identity            <- prose injected as room_identity
    # Room context        <- prose appended to room_identity
    # Conversation        <- prose injected as room_conversation
    # Safety              <- prose injected as room_safety
    # Room facts          <- prose injected as room_facts (optional)
    # Participant facts   <- prose injected as room_participant_facts (optional)

The loader returns the same dict shape consumers already expect — no
caller changes when the storage shape flipped from many-files to one.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import frontmatter

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


_SAFE_ROOM_ID_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")
_ROOM_FILE_NAME = "ROOM.md"

# Mapping of room_id prefix (before "::") to the shared config directory
# under app/assistant/rooms/. Keep in sync with the prefix routing used
# by the rest of the room runtime.
_ROOM_CONFIG_PREFIX_MAP: Dict[str, str] = {
    "task_spec": "task_create",
    "doc_editor": "doc_editor",
}

# H1 header (case-insensitive after lowercasing) -> blackboard key.
# A room's body sections are routed to these keys in declaration order;
# multiple sections targeting the same key concatenate.
_HEADER_TO_BB_KEY: List[Tuple[str, str]] = [
    ("identity",          "room_identity"),
    ("room context",      "room_identity"),       # appended
    ("conversation",      "room_conversation"),
    ("safety",            "room_safety"),
    ("room facts",        "room_facts"),
    ("participant facts", "room_participant_facts"),
]


def resolve_room_config_dir(room_id: str) -> Path:
    """Return the filesystem directory holding the room's ROOM.md.

    Same logic ``load_room_context_for_manager`` uses internally,
    exposed so other services (orchestrators, schedulers) can locate
    the room's config without re-implementing the prefix / segment-
    safety rules.
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
    """Read and return the room's policy block from ROOM.md frontmatter.

    Used by system-internal callers (orchestrators, schedulers) that
    need the room's authority/delivery/retention policy when they
    build a ScopeContext outside the room_session_manager path.
    """
    base_dir = resolve_room_config_dir(room_id)
    fm = _read_frontmatter(base_dir / _ROOM_FILE_NAME)
    policy = fm.get("policy")
    if not isinstance(policy, dict):
        raise FileNotFoundError(
            f"Room '{room_id}' ROOM.md has no 'policy' block in frontmatter."
        )
    return policy


def load_room_access(room_id: str) -> Dict[str, Any]:
    """Read and return the room's access block from ROOM.md frontmatter.

    Used by ingestion / scheduling callers that need the access
    declarations (chat_ingestion_entitled_rooms, ingestion_pod_kinds,
    rag_scopes, pinned/blocked entities) without loading the full
    prompt-context payload.
    """
    base_dir = resolve_room_config_dir(room_id)
    fm = _read_frontmatter(base_dir / _ROOM_FILE_NAME)
    access = fm.get("access")
    if not isinstance(access, dict):
        raise FileNotFoundError(
            f"Room '{room_id}' ROOM.md has no 'access' block in frontmatter."
        )
    return access


def load_room_context_for_manager(room_id: str) -> Dict[str, Any]:
    """Load room config + prose for an agent's prompt context.

    Reads ROOM.md, splits frontmatter into policy / permissions /
    access blocks, splits body by H1 headers into the named blackboard
    keys, and returns the flat dict consumers expect.

    Returns an empty dict when ``room_id`` is missing, non-string, or
    blank — callers in inbound paths sometimes invoke this before
    validating the message envelope.

    Raises loudly on a missing or malformed ROOM.md, missing required
    sections, or missing identity content (every room must declare
    who it is).
    """
    if not isinstance(room_id, str) or not room_id.strip():
        return {}

    base_dir = resolve_room_config_dir(room_id)
    room_id = room_id.strip()
    parts = base_dir.relative_to(Path(__file__).resolve().parent).parts

    md_path = base_dir / _ROOM_FILE_NAME
    if not md_path.exists():
        raise FileNotFoundError(
            f"Room '{room_id}' missing required file: {md_path}"
        )

    fm = _read_frontmatter(md_path)
    body = _read_body(md_path)

    policy = fm.get("policy") or {}
    permissions = fm.get("permissions") or {}
    access = fm.get("access") or {}
    if not isinstance(policy, dict):
        raise ValueError(f"Room '{room_id}' ROOM.md frontmatter 'policy' must be a mapping.")
    if not isinstance(permissions, dict):
        raise ValueError(f"Room '{room_id}' ROOM.md frontmatter 'permissions' must be a mapping.")
    if not isinstance(access, dict):
        raise ValueError(f"Room '{room_id}' ROOM.md frontmatter 'access' must be a mapping.")

    blackboard = _split_body_into_blackboard(body)
    if not blackboard.get("room_identity"):
        raise FileNotFoundError(
            f"Room '{room_id}' ROOM.md is missing identity content. "
            f"Add a '# Identity' section (or '# Room context')."
        )

    policy_id = policy.get("policy_id")
    if not isinstance(policy_id, str) or not policy_id.strip():
        policy_id = f"room_policy::{room_id}"

    room_surface = policy.get("surface")
    room_visibility = policy.get("default_visibility")
    room_manager_name = policy.get("manager_name")
    if not isinstance(room_manager_name, str) or not room_manager_name.strip():
        room_manager_name = "room_manager"

    # Prefer the display_name from policy.participant_identity (e.g. "Katy"),
    # falling back to a cleaned-up version of the room_id path.
    participant_identity = policy.get("participant_identity")
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
        "room_context_id": policy.get("default_context_id"),
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
        "room_allow_images":              bool(permissions.get("allow_images", False)),
        "room_allowed_global_resources":  access.get("allowed_global_resources", []),
        "room_allowed_entity_cards":      access.get("allowed_entity_cards", []),
        "room_pinned_entities":           access.get("pinned_entities", []),
        "room_blocked_entities":          access.get("blocked_entities", []),
        "room_rag_scopes":                access.get("rag_scopes", []),
        "room_shared_chat_room_ids":      access.get("shared_chat_room_ids", []),
    }


# ── helpers ─────────────────────────────────────────────────────────


def _read_frontmatter(path: Path) -> Dict[str, Any]:
    """Return the ROOM.md frontmatter dict (empty if no frontmatter)."""
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Missing required room file: {path}")
    raw = path.read_text(encoding="utf-8")
    post = frontmatter.loads(raw)
    md = post.metadata
    return md if isinstance(md, dict) else {}


def _read_body(path: Path) -> str:
    """Return the markdown body (everything after the frontmatter)."""
    raw = path.read_text(encoding="utf-8")
    post = frontmatter.loads(raw)
    return str(post.content or "")


def _split_body_into_blackboard(body: str) -> Dict[str, str]:
    """Walk H1 headers in body. Map each section to a blackboard key.

    Multiple sections with the same target key are joined with a blank
    line in between (so '# Identity' and '# Room context' both go into
    room_identity, just like the legacy loader behavior).

    Each section's H1 line is preserved in the returned content — the
    legacy JSON-wrapped resources ALSO carried their '# Your identity'
    style heading inside the content blob, and those headings are part
    of the agent prompt's visible structure. Stripping them would
    change rendered prompts.
    """
    sections = _split_h1_sections(body)
    out: Dict[str, str] = {}
    header_lookup = {h: k for h, k in _HEADER_TO_BB_KEY}
    last_key: str | None = None
    for header_text, content in sections:
        key = header_lookup.get(header_text.strip().lower())
        if key is None:
            # Unknown header. Do NOT silently drop authored prose — that hid
            # whole personality/engagement-policy sections from room prompts.
            # Fold it into the most-recent recognized section (so e.g. a
            # '# Your personality/backstory' under '# Identity' lands in
            # room_identity, '# Engagement Policy' after '# Conversation' lands
            # in room_conversation). An unknown section before ANY recognized
            # one has no anchor — warn and skip rather than guess.
            if last_key is None:
                logger.warning(
                    "ROOM.md: section %r appears before any recognized section and "
                    "was dropped. Place it under # Identity / # Conversation / # Safety / "
                    "# Participant facts (or # Room context / # Room facts).",
                    header_text.strip(),
                )
                continue
            key = last_key
        else:
            last_key = key
        snippet = content.strip()
        if not snippet and not header_text.strip():
            continue
        section_text = f"# {header_text.strip()}\n{snippet}".rstrip()
        if key in out:
            out[key] = out[key] + "\n\n" + section_text
        else:
            out[key] = section_text
    return out


def _split_h1_sections(body: str) -> List[Tuple[str, str]]:
    """Return [(header_text, body), ...] split on top-level H1 lines.

    A line starting with exactly one "# " (followed by a space) marks
    a new section. Content before the first H1 is dropped.
    """
    sections: List[Tuple[str, str]] = []
    current_header: str | None = None
    current_buf: List[str] = []
    for line in body.split("\n"):
        if line.startswith("# ") and not line.startswith("## "):
            if current_header is not None:
                sections.append((current_header, "\n".join(current_buf).strip()))
            current_header = line[2:].strip()
            current_buf = []
        else:
            if current_header is not None:
                current_buf.append(line)
    if current_header is not None:
        sections.append((current_header, "\n".join(current_buf).strip()))
    return sections
