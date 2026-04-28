from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Present-tense canonical statement of a State/Event/Goal node's fact.

    Stripped of temporal anchors and source-relative phrases — those live
    in start_date / end_date / valid_during. Used as Node.original_sentence
    after promotion to KG; downstream agents (wiki_writer, chat_gate, etc.)
    read this as the proposition, not the verbatim utterance. Verbatim
    utterance remains accessible via window_id and the evidence tables.
    """

    canonical_sentence: str = Field(
        ...,
        description=(
            "Present-tense statement of the fact. No temporal phrases like "
            "'last year', 'in 2025', 'yesterday'. Use canonical entity names, "
            "not pronouns. One sentence."
        ),
    )
