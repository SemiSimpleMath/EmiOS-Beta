from pydantic import BaseModel


class web_spatial_snapshot_args(BaseModel):
    # Optional question to bias scoring ("close modal", "find Add to Cart", etc.).
    question: str = ""
    # Proximity radius (pixels) for pulling text blocks near each interactive anchor.
    radius_px: int = 160
    # Maximum number of interactive anchors to return (sorted by score).
    max_anchors: int = 60
    # Max number of nearby text snippets to attach to each anchor.
    per_anchor_nearby: int = 4
    # Safety cap on text candidates scanned before proximity filter.
    max_text_candidates: int = 900


class web_spatial_snapshot_arguments(BaseModel):
    tool_name: str
    arguments: web_spatial_snapshot_args

