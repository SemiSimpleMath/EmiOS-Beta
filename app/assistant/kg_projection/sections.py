"""Section taxonomy for KG-projected artifacts.

The taxonomy defines the biographical/contextual "slots" that bullets can be
tagged into (family, education, work, contact, hobbies, ...). Shared across
wiki and entity-card generators so they're both tagging against the same
vocabulary — the tagger's cache is reused across consumers.

Authoritative file: ``resources/user/resource_wiki_sections.json`` (name kept
as-is for now to avoid a file rename; content is not wiki-specific).

Each consumer picks which sections it cares about. Wiki ignores
card-only sections; cards ignore wiki-only sections. No metadata distinguishes
them in the file — consumer-side selection only.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ScopeContext

logger = get_logger(__name__)

SECTIONS_RESOURCE = "resource_wiki_sections"


@dataclass
class SectionSpec:
    """A single taxonomy entry."""
    key: str           # slug used as tag + as dict key in grouped bullets
    title: str         # human-readable title for UIs / section headers
    description: str   # one-liner shown to the tagger to help it classify


def load_taxonomy(
    *,
    scope_context: Optional[ScopeContext] = None,
    resource_id: str = SECTIONS_RESOURCE,
) -> List[SectionSpec]:
    """Load the section taxonomy.

    If ``scope_context`` is provided, tries the ResourceManager first (so
    resource overlays + auth apply). Falls back to reading the raw JSON file
    from the repo's ``resources/user/`` directory. Callers that don't have a
    scope (e.g., one-off scripts) can omit it and get the file fallback.
    """
    raw_sections: List[dict] = []

    if scope_context is not None:
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            rm = DI.resource_manager
        except Exception:
            rm = None
        if rm is not None:
            try:
                value = rm.get_resource(
                    scope_context=scope_context,
                    resource_id=resource_id,
                    required=False,
                )
                if isinstance(value, dict) and isinstance(value.get("sections"), list):
                    raw_sections = value["sections"]
            except Exception as e:
                logger.debug("Could not load %s via resource_manager: %s", resource_id, e)

    if not raw_sections:
        from app.assistant.utils.path_utils import get_resources_dir
        candidate = get_resources_dir() / "user" / f"{resource_id}.json"
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    raw_sections = data.get("sections") or []
            except Exception as e:
                logger.warning("Failed reading %s: %s", candidate, e)

    out: List[SectionSpec] = []
    for s in raw_sections:
        key = str(s.get("key") or "").strip()
        if not key:
            continue
        out.append(SectionSpec(
            key=key,
            title=str(s.get("title") or key),
            description=str(s.get("description") or ""),
        ))
    return out


def sections_as_prompt_list(sections: List[SectionSpec]) -> str:
    """Render the taxonomy as a bullet list for the tagger's user prompt."""
    lines = [
        f"- `{s.key}` ({s.title}): {s.description}"
        for s in sections
    ]
    return "\n".join(lines)
