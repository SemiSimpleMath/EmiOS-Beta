from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """
    Structured output for `master_room::geoguessr_analyst`.

    The analyst inspects a new screenshot and updates the geographic analysis.
    It must NOT reveal the final answer — only reasoning clues and a regional guess.
    """

    is_geoguessr_visible: bool = Field(
        default=True,
        description=(
            "Set False when the screenshot does NOT show a GeoGuessr scene — e.g. the user "
            "switched to a browser tab, chat window, desktop, or any other non-game screen. "
            "When False the prior confidence and best_guess must be preserved unchanged; "
            "set visible_clue and commentary to empty strings."
        ),
    )

    # What reasoning clue to show the user in the panel (1-3 sentences, no answer spoiler)
    visible_clue: str = Field(
        description=(
            "A geographic observation visible in the screenshot — language of signs, "
            "vegetation type, road markings, architecture style, sun angle, etc. "
            "Must NOT name the country or city outright. Think like a detective presenting evidence. "
            "Empty string when is_geoguessr_visible is False."
        )
    )

    # Internal region-level guess stored in session but not shown until reveal
    best_guess: str = Field(
        description=(
            "Your current best geographic guess at region/country level. "
            "This is stored internally and revealed only when the user asks. "
            "Preserve the previous best_guess unchanged when is_geoguessr_visible is False."
        )
    )

    # 0–100 confidence
    confidence: int = Field(
        ge=0,
        le=100,
        description=(
            "Confidence 0-100 in the current best_guess. "
            "Preserve the previous confidence unchanged when is_geoguessr_visible is False."
        ),
    )

    # Emi's commentary visible in panel — intriguing, doesn't give the answer
    commentary: str = Field(
        description=(
            "Short, engaging commentary for the side panel (1-2 sentences). "
            "Tease the mystery. Do not name the location. "
            "Empty string when is_geoguessr_visible is False."
        )
    )
