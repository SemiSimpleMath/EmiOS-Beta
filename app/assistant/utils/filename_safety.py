"""Project-wide filename sanitization for entity labels used as paths.

KG entity labels can contain characters Windows reserves in filenames
(``/ \\ : * ? " < > |``). Any code that writes a per-entity sidecar (wiki
prose, tag cache, bullet index, section_outputs, etc.) MUST sanitize via
this helper so:

  1. The path stays valid on Windows.
  2. Every call site agrees on the same stem — otherwise the wiki writes
     ``<vault>/Deli_Sushi Section.md`` while the tag-cache reader looks
     for ``<vault>/tags/Deli/Sushi Section.json`` and crashes.

Apostrophes (ASCII + curly), spaces, dots, and hyphens are preserved so
natural labels like "Katy" or "St. Mary's" round-trip cleanly into
Obsidian's ``[[wiki-link]]`` resolution.
"""
from __future__ import annotations

import re

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9 ._'’\-]")


def safe_filename(label: str) -> str:
    """Convert an entity label to a filesystem-safe stem (no extension).

    Examples::

        safe_filename("Katy")               -> "Katy"
        safe_filename("Deli/Sushi Section") -> "Deli_Sushi Section"
        safe_filename("Foo:Bar")            -> "Foo_Bar"
        safe_filename("St. Mary's")         -> "St. Mary's"
    """
    stem = _SAFE_FILENAME_RE.sub("_", (label or "").strip())
    return stem.strip(". ")
