from typing import List

from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Structured per-frame analysis output for `sleep_analyzer`.

    One Ring frame in → one structured snapshot of what was visible. A
    nightly aggregator (TODO) will walk these sidecars and synthesize
    sleep-quality reports.
    """

    subject_in_bed: bool = Field(
        ...,
        description="True if a person is visibly in the bed.",
    )
    position: str = Field(
        ...,
        description=(
            "One of: side-left, side-right, supine (on back), prone (face down), "
            "sitting up, not in bed, unclear."
        ),
    )
    motion_level: str = Field(
        ...,
        description=(
            "Visible motion in this single frame: still, minimal, moderate, high, "
            "unclear. (Single-frame inference; not motion vs prior frame.)"
        ),
    )
    light_state: str = Field(
        ...,
        description="dark, dim, lit, unclear.",
    )
    awake_indicators: List[str] = Field(
        default_factory=list,
        description=(
            "Visible signs of being awake (empty list if none): e.g. 'eyes open', "
            "'phone illuminated', 'sitting up', 'reading', 'out of bed'."
        ),
    )
    notes: str = Field(
        default="",
        description=(
            "Short free-form note about anything else worth recording in the "
            "nightly aggregate (intrusions, lights on, pet on bed, etc.). "
            "Empty string when nothing notable."
        ),
    )
