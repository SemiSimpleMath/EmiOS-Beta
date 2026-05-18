"""Display helpers — converting importance numbers into visual signals.

These are pure formatting helpers; they don't mutate state and don't
read from the DB. Moved here from `kg/timeline_builder.py` so display
bands have a canonical home shared by anything that wants to render
an importance band marker (timelines, lists, future card-summary UIs).

The band thresholds are intentionally separate from the consumer-gate
thresholds in `consumers.py`. A node can be "major" for display
purposes (≥ 7) without necessarily clearing a stricter consumer
threshold like `TIMELINE_CONFIRMATION_FLOOR = 6.0`. Bands are about
visual prioritization; gates are about action.
"""
from __future__ import annotations

from typing import Optional


IMPORTANCE_BAND_MAJOR: float = 7.0   # ★ marker — top tier
IMPORTANCE_BAND_MID: float = 4.0     # · marker — mid tier
# Below MID renders blank (still included in output, no prefix).


def importance_marker(imp: Optional[float]) -> str:
    """Visual weight prefix for a list/timeline entry.

    Returns a two-character string so callers can prepend it directly
    to a label without extra spacing logic:
        "★ " for major
        "· " for mid
        "  " for low or unrated

    Falls back to two-space blank when importance is None (rater hasn't
    run yet) — common during transition periods. The blank still
    occupies the column so column alignment is preserved across mixed
    rated/unrated lines.

    Moved from `kg/timeline_builder.py:_importance_marker`. The original
    location now imports this. The numeric thresholds also moved here
    (IMPORTANCE_BAND_MAJOR / IMPORTANCE_BAND_MID); timeline_builder
    re-exports them too for back-compat.
    """
    if imp is None:
        return "  "
    if imp >= IMPORTANCE_BAND_MAJOR:
        return "★ "
    if imp >= IMPORTANCE_BAND_MID:
        return "· "
    return "  "
