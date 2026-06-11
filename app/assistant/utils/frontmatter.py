from __future__ import annotations

from typing import Any

import yaml

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown document into (YAML frontmatter dict, body text).

    Frontmatter is delimited by ``---`` markers at the top of the document.
    Documents without frontmatter return ``({}, text)`` unchanged.
    """
    raw = text.lstrip("﻿")
    if not raw.startswith("---"):
        return {}, text
    parts = raw.split("\n", 1)
    if len(parts) < 2:
        return {}, text
    rest = parts[1]
    if "\n---" not in rest:
        return {}, text
    fm_text, body = rest.split("\n---", 1)
    try:
        frontmatter = yaml.safe_load(fm_text) or {}
    except Exception as e:
        logger.warning("Failed to parse frontmatter: %s", e)
        frontmatter = {}
    # Strip leading newline if present.
    if body.startswith("\n"):
        body = body[1:]
    return frontmatter, body
