from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """
    Structured output for `master_room::geoguessr_host`.

    The host handles all user-facing conversation during a GeoGuessr game.
    """

    chat_response: str = Field(
        description=(
            "Your reply to the user. If they asked for a hint, give one geographic clue "
            "without naming the location. If they triggered a reveal, you may now name the "
            "location and explain the evidence that led you there."
        )
    )

    reveal_answer: bool = Field(
        default=False,
        description=(
            "Set True ONLY when the user explicitly asks for the answer / to give up / reveal. "
            "Examples: 'tell me', 'give up', 'what is it', 'reveal', 'answer'. "
            "Do NOT set True for hints or general questions."
        )
    )

    best_guess: str = Field(
        default="",
        description=(
            "Populate ONLY when reveal_answer is True. "
            "The most specific location you can determine — country, region, city, and street if possible. "
            "Copy your secret best guess from the system prompt exactly. "
            "Leave empty when not revealing."
        )
    )

    game_over: bool = Field(
        default=False,
        description=(
            "Set True when the game should end (user says stop/quit/end game, "
            "or after a successful reveal and user is done)."
        )
    )
