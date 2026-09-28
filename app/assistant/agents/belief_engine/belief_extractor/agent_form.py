from typing import List, Literal

from pydantic import BaseModel, Field


class Belief(BaseModel):
    insight_ref: int = Field(
        description="The number `n` of the chronic insight this belief restates.")
    statement: str = Field(
        description="ONE claim about the person or their world, declarative, understood on its own "
                    "by a reader who never saw this day. A wish or judgment names who holds it.")
    kind: Literal[
        "durable_fact",
        "stable_relationship",
        "stable_preference",
        "routine_pattern",
        "episodic_context",
        "transient_state",
    ] = Field(
        description="How long this stays useful to know: durable_fact = years; stable_relationship "
                    "= a persistent tie; stable_preference = an enduring taste or wish; "
                    "routine_pattern = a recurring habit; episodic_context = a situation lasting "
                    "days to weeks; transient_state = gone within a day.")
    sources: List[int] = Field(
        min_length=1,
        description="Numbers `n` of the timeline items this belief rests on — the person's own "
                    "words, or a prompt they declined or deferred.")
    reasoning: str = Field(
        description="One sentence: which words or responses establish this claim.")


class AgentForm(BaseModel):
    beliefs: List[Belief] = Field(
        default_factory=list,
        description="The lasting beliefs this day's chronic insights establish. Few and "
                    "high-signal; an empty list is a correct answer.")
