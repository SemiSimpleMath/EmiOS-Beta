from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class InferredConnection(BaseModel):
    """One new edge (sometimes plus one new target node) the agent
    proposes adding to the KG, derived from reading the wiki page +
    surrounding KG context.

    The agent NEVER writes to the KG. It produces structured proposals
    that the pipeline pushes through the standard claim_proposal layer
    (where the promoter applies the same gates as for chat-extracted
    facts: dedup, time-frame check, hub-overlap filter, LLM merger).

    `target_node_id` should be set when the wiki page implies a connection
    to an entity that already exists in the KG (the agent is given the
    list of known entities). If the page implies a brand-new entity that
    has no current KG node, set `target_node_id=None` and provide
    `target_label` + `target_node_type` — the proposal pipeline will
    mint a new node when promoting.
    """

    subject_node_id: str = Field(
        description="The KG node id the wiki page is about (always known).",
    )

    target_node_id: Optional[str] = Field(
        default=None,
        description=(
            "The KG node id of the target. Use this when the page connects "
            "to an entity already in the `known_entities` list — never "
            "invent ids. Leave null when the connection is to an entity "
            "the KG doesn't yet have."
        ),
    )

    target_label: Optional[str] = Field(
        default=None,
        description=(
            "Required when target_node_id is null: the label of the new "
            "target entity to mint (e.g. 'Dylan'). Title case, no quotes. "
            "Leave null when target_node_id is set."
        ),
    )

    target_node_type: Optional[
        Literal["Person", "Place", "Event", "State", "Goal", "Organization", "Object", "Concept", "Entity"]
    ] = Field(
        default=None,
        description=(
            "Required when target_node_id is null: the type of the new "
            "target entity. Match the KG's existing taxonomy — Person, "
            "Place, Event, etc."
        ),
    )

    predicate: str = Field(
        description=(
            "The relationship_type, lowercase snake_case (e.g. "
            "'nephew_of', 'attends_school_at', 'lives_in', 'married_to'). "
            "Prefer predicates already used elsewhere in the KG over "
            "inventing novel ones."
        ),
    )

    sentence: str = Field(
        description=(
            "A present-tense canonical sentence stating the connection, as "
            "it would appear in the KG. E.g. 'Dylan is Jukka's nephew.' "
            "Not past tense, not first person."
        ),
    )

    evidence_quote: str = Field(
        description=(
            "The verbatim line(s) from the wiki page that imply this "
            "connection. Must be quotable text from the page, NOT prose "
            "the agent wrote itself."
        ),
    )

    inference_path: str = Field(
        description=(
            "Brief reasoning chain. E.g. 'Page says \"Diana's son\" → "
            "Diana sister_of Jukka in KG → Dylan nephew_of Jukka.' One "
            "sentence; this is what makes the proposal auditable."
        ),
    )

    confidence: float = Field(
        ge=0.0, le=1.0,
        description=(
            "Strength of the inference. >= 0.8 means 'I'm confident the "
            "wiki text + KG together imply this'; lower means speculative. "
            "Confidence here doesn't auto-promote — every proposal still "
            "goes through the standard claim_proposal gates."
        ),
    )

    not_already_in_kg: bool = Field(
        description=(
            "Set True only after checking the subject's neighborhood for "
            "an existing edge with this predicate to this target. Prevents "
            "the agent from re-proposing edges that already exist."
        ),
    )


class AgentForm(BaseModel):
    """Output of the wiki_connection_investigator agent.

    `connections` is a list of brand-new edges the wiki page implies but
    the KG doesn't yet have. Empty list is the right answer when the
    page's claims are already represented; producing speculative edges to
    fill the list hurts precision.
    """

    connections: List[InferredConnection]
    reason: str = Field(
        description=(
            "One-sentence summary of what was found. 'Found 2 new "
            "kinship edges (nephew, niece) implied by Diana's lineage' "
            "or 'No new connections — page is fully reflected in KG'."
        ),
    )
