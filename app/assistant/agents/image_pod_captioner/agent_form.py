"""Structured output for the image_pod_captioner.

Runs once per image pod (chat-attached) to enrich it from `vision_extraction_status:
pending` to "done" with a real description. Combines what's visible in
the image with what the user just said about it (the trigger message
plus surrounding chat) so downstream search has both signals.
"""
from typing import List

from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    """Structured caption result, written back to the image pod."""
    model_config = ConfigDict(extra="forbid")

    one_liner: str = Field(
        max_length=160,
        description=(
            "Short user-facing label for the image. Goes to pod.one_liner. "
            "Should bind the visual content with any user-supplied context "
            "(name of subject, occasion, location). Example: "
            "'Jukka standing on the porch of the new house (Sept 2026)' "
            "for an image the user described as 'here is me at the new place'."
        ),
    )
    body: str = Field(
        max_length=1200,
        description=(
            "Richer description for keyword search and downstream agent "
            "context. Structure: Visual description (what's literally in "
            "the frame) + User context (what the user said about it) + "
            "Notable details. Plain prose, ~3-6 sentences."
        ),
    )
    tags: List[str] = Field(
        default_factory=list,
        description=(
            "Lowercase short keywords for tag-based filtering. Include "
            "person names mentioned in chat, location, occasion, objects. "
            "Keep to ~3-8 tags."
        ),
    )
    depicted_entities: List[str] = Field(
        default_factory=list,
        description=(
            "Specific named people / pets / places the image depicts "
            "(derived from both visual + user context). e.g. ['Jukka', "
            "'Katy'] when the user said 'this is us at dinner' AND two "
            "people are visible. Empty when the image is generic and the "
            "user didn't name anyone. Used by a future KG-edge writer to "
            "link the image to entities."
        ),
    )
