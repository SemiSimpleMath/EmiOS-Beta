from typing import List, Optional

from pydantic import BaseModel, Field


class EvidenceItem(BaseModel):
    query: str = Field(
        description="The exact SQL or tool call that produced this evidence.",
    )
    finding: str = Field(
        description="The result that mattered — a count, a comparison, a sample row.",
    )


class FinalAnswerDataItem(BaseModel):
    data_type: Optional[str] = None
    key: Optional[str] = None
    value: Optional[str] = None
    url: Optional[str] = None
    link: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    source: Optional[str] = None
    timestamp: Optional[str] = None


class AgentForm(BaseModel):
    """Investigation report shape.

    The investigator produces a PROSE `recommendation` that describes what
    KG mutations should happen and why. That prose is what the executor
    (kg_resolution_manager) reads and acts on; it is also what the dev
    page renders as the primary card content. The structured `evidence`
    and `diagnosis` fields exist for the dev-page detail view and for
    debugging — they don't drive execution.

    Routing:
    - disposition='auto_apply' → dev page, 24h grace timer, auto-applies
      if not Accepted/Declined within window
    - disposition='needs_user_review' → user queue, no timer; user must
      respond. `user_question` is the specific question they need to
      answer.
    """

    # ---- load-bearing ----
    recommendation: str = Field(
        description=(
            "Prose plan: what KG mutations should happen and why. Cite "
            "specific node ids, dates, and fields. Each step's reasoning "
            "should be readable enough that a human can audit it and "
            "decide whether they agree. Do NOT mention wiki page or "
            "entity card regen — those are auto-handled downstream."
        ),
    )
    disposition: str = Field(
        description=(
            "'auto_apply' (most cases — recommendation is confident enough "
            "to run after a 24h grace window) OR 'needs_user_review' "
            "(genuine ambiguity — only the user can decide; e.g., dates "
            "are missing and the source doesn't disambiguate, or two "
            "plausible interpretations conflict)."
        ),
    )
    user_question: Optional[str] = Field(
        default=None,
        description=(
            "Required when disposition='needs_user_review'. The specific "
            "question the user needs to answer. Example: 'Did Annika stop "
            "art lessons before or after the cabin trip Nov 5-12?'"
        ),
    )
    confidence: float = Field(
        description=(
            "Investigator confidence in the recommendation, 0.0-1.0. Used "
            "for filtering / sorting on the dev page; auto-apply gating "
            "happens via disposition rather than a confidence threshold."
        ),
    )

    # ---- dev-page detail / debugging ----
    diagnosis: str = Field(
        description=(
            "One-paragraph plain-language statement of what was found. "
            "Lead with the conclusion. Cite key numbers (counts, dates, "
            "ids) inline. Used for the dev-page expand-detail view."
        ),
    )
    evidence: List[EvidenceItem] = Field(
        default_factory=list,
        description=(
            "Ordered list of (query, finding) pairs that ground the "
            "diagnosis. At least one item unless the investigation truly "
            "returned nothing."
        ),
    )
    open_questions: List[str] = Field(
        default_factory=list,
        description=(
            "Questions the data couldn't answer. When disposition="
            "'needs_user_review', the load-bearing question goes in "
            "`user_question`; secondary unanswered questions go here."
        ),
    )

    # ---- standard envelope (consumed by manager_exit_node + room formatters) ----
    final_answer_answer: str = Field(
        description=(
            "Markdown rendering of the structured fields above, suitable "
            "for human reading. Sections: ## Recommendation, ## Diagnosis, "
            "## Evidence, ## Open questions (omit empty sections)."
        ),
    )
    result_summary: str = Field(
        default="",
        description="One-sentence outcome for downstream agents (max ~150 chars).",
    )
    final_answer_sources: List[str] = Field(default_factory=list)
    final_answer_detail_level: str = "full"
    final_answer_data_list: List[FinalAnswerDataItem] = Field(default_factory=list)
    final_answer_task: Optional[str] = ""
    final_answer_what_was_done: Optional[str] = ""
    final_answer_interesting_info: Optional[str] = ""
