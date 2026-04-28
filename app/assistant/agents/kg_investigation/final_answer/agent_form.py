from typing import List, Optional

from pydantic import BaseModel, Field


class EvidenceItem(BaseModel):
    query: str = Field(
        description="The exact SQL or tool call that produced this evidence.",
    )
    finding: str = Field(
        description="The result that mattered — a count, a comparison, a sample row.",
    )


class ProposedAction(BaseModel):
    op: str = Field(
        description=(
            "Suggested mutation op for a future kg_mutation_manager. One of: "
            "merge_nodes, split_node, delete_edge, update_node_field, no_action, escalate_user."
        ),
    )
    args: str = Field(
        default="",
        description=(
            "Free-form description of args for the op (node ids, field+value, partition spec, etc.). "
            "Empty when op is no_action or escalate_user."
        ),
    )
    reversibility: str = Field(
        description="One of: reversible, partially_reversible, irreversible.",
    )
    confidence: float = Field(
        description="Investigator confidence in the diagnosis on a 0.0-1.0 scale.",
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
    # ---- canonical structured investigation report (consumed by future
    # kg_mutation_manager and any other programmatic consumer) ----
    diagnosis: str = Field(
        description=(
            "One-paragraph plain-language statement of what was found. Lead with the conclusion. "
            "Cite the key numbers (counts, dates, ids) inline so the reader can sanity-check."
        ),
    )
    evidence: List[EvidenceItem] = Field(
        default_factory=list,
        description=(
            "Ordered list of (query, finding) pairs that ground the diagnosis. "
            "At least one item unless the investigation truly returned nothing."
        ),
    )
    proposed_action: Optional[ProposedAction] = Field(
        default=None,
        description=(
            "If the investigation suggests a concrete next step, populate this. "
            "Set op='no_action' when no change is warranted; op='escalate_user' when "
            "a human decision is required before any change."
        ),
    )
    open_questions: List[str] = Field(
        default_factory=list,
        description=(
            "Questions the data could not answer — would require additional input "
            "(e.g., user clarification, a query the read-only scope doesn't permit)."
        ),
    )

    # ---- standard envelope (consumed by manager_exit_node + room formatters) ----
    final_answer_answer: str = Field(
        description=(
            "Markdown rendering of the structured fields above, suitable for human reading. "
            "Sections: ## Diagnosis, ## Evidence, ## Proposed action, ## Open questions "
            "(omit Open questions section when empty)."
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
