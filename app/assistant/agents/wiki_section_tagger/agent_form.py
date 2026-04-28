from typing import List

from pydantic import BaseModel


class TagResult(BaseModel):
    """One row in the tagger's batched output."""

    number: int
    sections: List[str]


class AgentForm(BaseModel):
    """Batched tagging output.

    `results`: one entry per input bullet, preserving the 1-based `number`
    the caller used. Each entry's `sections` must be a subset of the
    `allowed_sections` list given in the user prompt. Empty list means the
    bullet is not worth surfacing in any section.
    `reason`: brief rationale — 30 words max, covers the batch overall.
    """

    results: List[TagResult]
    reason: str
