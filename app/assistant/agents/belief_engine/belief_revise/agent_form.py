from typing import Literal

from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    outcome: Literal["revised", "unchanged"] = Field(
        description="revised: the whole evidence now says something different from the belief as held — "
                    "including a statement about the state itself made once. unchanged: the new evidence "
                    "confirms the belief, asks a question, or reports one occasion that went differently — "
                    "the belief stands as held.")
    statement: str = Field(
        description="The belief as it stands now, in the present: one claim, declarative, understood on its "
                    "own. When its state changed, one dated clause says what held before and until when. "
                    "When the outcome is unchanged, the belief as held, word for word.")
    kind: Literal[
        "durable_fact",
        "stable_relationship",
        "stable_preference",
        "routine_pattern",
        "episodic_context",
        "transient_state",
    ] = Field(description="How long the belief stays useful to know.")
    reasoning: str = Field(description="One sentence: which evidence decided the outcome, and how.")
