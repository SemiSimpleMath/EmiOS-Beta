"""Shopping-intent consolidation for the meal subconscious surface.

The weekly-meal-email rendering that used to live here was removed with the
"Send to Katy" feature; `consolidate_intention_shopping` survives because the
/meals page view-model uses it to build the ad-hoc additions block.
"""
from __future__ import annotations

from typing import Iterable, List

from app.assistant.pod_store.contracts import Pod


def consolidate_intention_shopping(pods: Iterable[Pod]) -> List[str]:
    """Collect unique items across all intention.shopping pods, in
    first-seen order. Case-insensitive dedup. Used by the service layer
    to build the ad-hoc additions block."""
    seen: dict = {}
    for pod in pods:
        items = (pod.metadata or {}).get("items") or []
        for raw in items:
            item = str(raw).strip()
            if not item:
                continue
            key = item.lower()
            if key not in seen:
                seen[key] = item
    return list(seen.values())
