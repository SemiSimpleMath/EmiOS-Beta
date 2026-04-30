from typing import List, Optional

from pydantic import BaseModel, Field


class InferredEdge(BaseModel):
    """A KG edge the page + neighborhood imply but the graph doesn't yet have.

    Reviewer should only emit these when the chain of facts in scope clearly
    supports it (e.g., "Lisa is Jukka's sister-in-law" + "Lisa has son Lucas"
    → "Lucas is_nephew_of Jukka"). When the inference requires guessing or
    the user's confirmation, surface as a UserQuestion instead.
    """

    subject: str = Field(..., description="Canonical entity label on the source side of the edge.")
    predicate: str = Field(..., description="Relationship name. Use canonical KG predicates when possible.")
    object: str = Field(..., description="Canonical entity label on the target side of the edge.")
    reasoning: str = Field(
        ...,
        description=(
            "One short paragraph explaining the chain of facts in the page + "
            "neighborhood that supports this edge. Cite specifics."
        ),
    )
    confidence: float = Field(..., ge=0.0, le=1.0)
    supersedes: Optional[str] = Field(
        None,
        description=(
            "If this edge contradicts an existing edge in the graph, name the "
            "existing one (subject + predicate + object) so the reviewer can "
            "decide whether to retract it."
        ),
    )


class UserQuestion(BaseModel):
    """A natural follow-up question the user is best-positioned to answer.

    Reviewer should only emit these when the gap is real (looks like obvious
    biographical content is missing for an entity of this type) AND deriving
    it from existing facts isn't possible. Don't ask trivia. Don't ask things
    the page already answers in another section. Don't ask about every blank
    section — be selective like a good reviewer would.
    """

    question: str = Field(
        ...,
        description=(
            "One natural sentence the user could read and answer in chat. "
            "Sound like a curious friend, not a form. Avoid 'do you have any...' "
            "openers; prefer a specific, concrete question."
        ),
    )
    theme: str = Field(
        ...,
        description=(
            "Short tag for the gap area (e.g. 'family', 'education', 'work', "
            "'hobbies', 'origin'). Used for queue diversity."
        ),
    )
    importance: float = Field(
        ...,
        ge=0.0, le=1.0,
        description=(
            "How much would answering this enrich what's known about the "
            "entity? 0.9+ = core biographical fact (parents, school). "
            "0.5–0.7 = enriching but not essential. <0.4 = nice-to-have."
        ),
    )


class AgentForm(BaseModel):
    """Output of one wiki page review.

    Both lists may be empty when the page already covers the entity well.
    A short ``page_assessment`` in 1-2 sentences gives the maintenance log a
    quick read on the page's overall completeness.
    """

    page_assessment: str = Field(
        ...,
        description="1-2 sentences on the page's overall completeness and notable gaps.",
    )
    inferred_edges: List[InferredEdge] = Field(default_factory=list)
    user_questions: List[UserQuestion] = Field(default_factory=list)
