from typing import List

from pydantic import BaseModel, Field


class BeliefOutcome(BaseModel):
    belief_key: str = Field(
        description="Copy the belief_key exactly as supplied. Never invent, shorten or paraphrase "
                    "one, and never return a key that was not given to you.")
    action: str = Field(
        description="What this work outcome does to the belief. 'no_change' — the belief still "
                    "holds as written and the outcome is only evidence for it. 'resolve' — the "
                    "condition the belief itself names as done has been met, so the belief should "
                    "stop driving work. 'revise' — the belief is still live but its wording no "
                    "longer matches what is true; supply the replacement statement.")
    valence: str = Field(
        description="How this outcome bears on the belief: 'support' (it confirms the belief), "
                    "'contradict' (it tells against the belief), 'qualify' (it narrows or adds a "
                    "condition). Report the relation you can defend from the record, not the "
                    "sentiment of the work's own status.")
    reasoning: str = Field(
        description="Why this action, quoting the specific words or verdict you are relying on. "
                    "Name the belief's own completion condition and say whether it was met.")
    statement: str = Field(
        default="",
        description="Required for 'revise': the complete replacement statement, self-contained and "
                    "preserving every condition that still applies. Leave empty otherwise.")


class AgentForm(BaseModel):
    outcomes: List[BeliefOutcome] = Field(
        default_factory=list,
        description="One entry for every belief supplied, in any order. Do not omit a belief and "
                    "do not add one.")
