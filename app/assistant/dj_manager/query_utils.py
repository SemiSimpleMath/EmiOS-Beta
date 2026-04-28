from __future__ import annotations

from typing import Tuple


def build_search_query(title: str, artist: str) -> str:
    t = (title or "").strip()
    a = (artist or "").strip()
    if not t and not a:
        return ""
    if not a:
        return t
    if not t:
        return a
    return f"{t} by {a}"


def parse_search_query(search_query: str) -> Tuple[str, str]:
    """
    Parse a "Song by Artist" or "Artist - Song" search query into (title, artist).

    This is used for:
    - play history scoring/recording (DB rows are keyed by title+artist)
    - candidate scoring logs
    """
    q = (search_query or "").strip()
    if not q:
        return "", "Unknown"

    q_lower = q.lower()

    # Preferred canonical form: "Song by Artist"
    if " by " in q_lower:
        idx = q_lower.index(" by ")
        title = q[:idx].strip()
        artist = q[idx + 4:].strip()
        return title, artist or "Unknown"

    # Alternate common form: "Artist - Song"
    if " - " in q:
        parts = q.split(" - ", 1)
        artist = (parts[0] or "").strip() or "Unknown"
        title = (parts[1] or "").strip() or q
        return title, artist

    return q, "Unknown"

