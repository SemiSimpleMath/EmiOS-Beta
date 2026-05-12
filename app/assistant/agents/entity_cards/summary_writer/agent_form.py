from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Summary writer output: a 2-3 sentence condensed view of the card."""

    summary: str = Field(
        ...,
        description=(
            "2-3 sentence summary condensing the most important facts from "
            "the card's bullet sections. Present tense, anchored to the "
            "subject. For people in the user's life, third-person framing "
            "naming the user ('Jukka's wife', 'Jukka's daughter Annika') — "
            "card prose is read by agents, not the user. For the user "
            "themselves, third person ('Jukka is a software engineer')."
        ),
    )
