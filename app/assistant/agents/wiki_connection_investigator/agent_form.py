from typing import List, Optional

from pydantic import BaseModel, Field


class InferredSentence(BaseModel):
    """One synthetic factual sentence the wiki page implies but the KG
    doesn't yet have.

    These get written to a human-review queue (kg_maintenance_finding
    type='wiki_inferred_fact'). The user reviews + fills any missing
    dates + approves before the fact ever lands in the graph. NO
    auto-ingestion. The agent's job is precision and date extraction;
    final approval is always human.
    """

    sentence: str = Field(
        description=(
            "The synthetic fact, as a complete English sentence. "
            "Present-tense canonical (or past tense when the page itself "
            "places the fact strictly in the past with explicit dates). "
            "Names spelled in full. Examples: 'Jorma is Jukka's father.' "
            "/ 'Diana taught at UC Berkeley starting in 2018.'"
        ),
    )

    suggested_start_date: Optional[str] = Field(
        default=None,
        description=(
            "If the page provides an explicit start date for this fact, "
            "set it as ISO 'YYYY-MM-DD'. Use 'YYYY-01-01' if only the year "
            "is known. Leave null when no start date is in the page. "
            "Don't guess; the user will fill in missing dates at review time."
        ),
    )

    suggested_end_date: Optional[str] = Field(
        default=None,
        description=(
            "Same shape as suggested_start_date. Set when the page implies "
            "the fact has ended (e.g., 'lived in Oakland 2010-2018'). "
            "Leave null for ongoing or undated facts."
        ),
    )

    suggested_start_date_prose: Optional[str] = Field(
        default=None,
        description=(
            "When the page hints at WHEN something started but not "
            "precisely (e.g., 'in their early thirties', 'before the "
            "pandemic'), capture that fuzzy phrase here. The user can "
            "review and tighten it. Leave null otherwise."
        ),
    )

    suggested_end_date_prose: Optional[str] = Field(
        default=None,
        description="Fuzzy end-date phrase, same shape as suggested_start_date_prose.",
    )

    evidence_quote: str = Field(
        description=(
            "The verbatim line(s) from the wiki page that imply this "
            "fact. Must be quotable text from the page, not the agent's "
            "own prose."
        ),
    )

    inference_path: str = Field(
        description=(
            "Brief reasoning chain. E.g. 'Page says \"Diana's son Dylan\" "
            "→ KG: Diana sister_of Jukka → Dylan is Jukka's nephew.' "
            "One sentence; this makes the proposal auditable."
        ),
    )

    confidence: float = Field(
        ge=0.0, le=1.0,
        description=(
            "Strength of the inference. >= 0.8: the wiki text + KG "
            "together imply this beyond reasonable doubt. 0.6–0.8: a "
            "solid inference but with one shaky link. < 0.6: don't emit."
        ),
    )

    not_already_in_kg: bool = Field(
        description=(
            "Set True only after checking the subject's neighborhood for "
            "an existing connection that already covers this fact "
            "(possibly under a different predicate). When False, do not "
            "emit this sentence."
        ),
    )


class AgentForm(BaseModel):
    """Output of the wiki_connection_investigator agent.

    `sentences` is a list of brand-new synthetic facts the wiki page
    implies but the KG doesn't yet have. Empty list is the right answer
    when the page is fully reflected. Each sentence flows to a human
    review queue (kg_maintenance_finding) — never auto-applied.
    """

    sentences: List[InferredSentence]
    reason: str = Field(
        description=(
            "One-sentence summary of what was found. 'Found 2 new "
            "kinship facts from Diana's lineage.' or 'No new "
            "connections — page is fully reflected in KG.'"
        ),
    )
