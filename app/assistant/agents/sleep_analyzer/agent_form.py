from typing import List

from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Structured per-frame analysis output for `sleep_analyzer`.

    Each invocation receives the current frame plus (when available) the
    previous frame for the same camera. Motion is judged by comparing them.
    A nightly aggregator (TODO) will walk these sidecars and synthesize
    sleep-quality reports.
    """

    subject_in_bed: bool = Field(
        ...,
        description="True if a person is visibly in the bed (in the CURRENT frame).",
    )
    position: str = Field(
        ...,
        description=(
            "Position in the CURRENT frame. One of: side-left, side-right, "
            "supine (on back), prone (face down), sitting up, not in bed, unclear."
        ),
    )
    motion_vs_previous: str = Field(
        ...,
        description=(
            "Comparing current to previous frame: still, minimal, moderate, "
            "high, unclear, no_previous (when no comparable prior frame exists)."
        ),
    )
    light_state: str = Field(
        ...,
        description="Current frame: dark, dim, lit, unclear.",
    )
    awake_indicators: List[str] = Field(
        default_factory=list,
        description=(
            "Visible signs of being awake in the CURRENT frame (empty list if "
            "none): e.g. 'eyes open', 'phone illuminated', 'sitting up', "
            "'reading', 'out of bed'."
        ),
    )
    notes: str = Field(
        default="",
        description=(
            "Short free-form note about anything else worth recording in the "
            "nightly aggregate (intrusions, lights on, pet on bed, position "
            "change between frames, etc.). Empty string when nothing notable."
        ),
    )
