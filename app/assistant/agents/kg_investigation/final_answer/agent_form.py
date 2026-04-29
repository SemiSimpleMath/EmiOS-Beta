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
            "merge_nodes, split_state, delete_edge, update_node_field, "
            "batch_text_substitute, no_action, escalate_user."
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


class AffectedRecordRecommendation(BaseModel):
    op: str = Field(
        description=(
            "What to do with this specific record. One of: "
            "update_text (change `field` to `new`), "
            "no_change (record was inspected but should not be modified — `reason` REQUIRED), "
            "delete_edge (only valid when table=kg_edge_metadata), "
            "delete_node (only valid when table=kg_node_metadata)."
        ),
    )
    new: Optional[str] = Field(
        default=None,
        description=(
            "Required when op=update_text — the replacement value for the field. "
            "Must be different from current_value. Null for other ops."
        ),
    )
    reason: Optional[str] = Field(
        default=None,
        description=(
            "Required when op=no_change — short explanation of why this record was "
            "considered but should not be touched (e.g. 'faithful to source narrative')."
        ),
    )


class AffectedRecord(BaseModel):
    """One concrete record the investigator inspected, with a per-record recommendation.

    Populated by harvesting the planner's enumeration turns — never invented.
    The executor's batch_text_substitute primitive consumes this list directly:
    every entry's (table, id, field, current_value) is verified at apply time
    so a stale plan refuses to mutate.
    """
    table: str = Field(
        description="DB table the record lives in. Currently 'kg_node_metadata' or 'kg_edge_metadata'.",
    )
    id: str = Field(
        description="Primary-key id of the record in that table.",
    )
    field: str = Field(
        description=(
            "Column name being addressed. For nodes: 'original_sentence', 'description', "
            "'label'. For edges: 'sentence'. Other columns require op != update_text."
        ),
    )
    current_value: Optional[str] = Field(
        default=None,
        description=(
            "The value currently in `<table>.<field>` for `<id>`, as observed during the "
            "investigation. The executor compares this against the live DB value before "
            "mutating and refuses on drift."
        ),
    )
    recommendation: AffectedRecordRecommendation = Field(
        description="Per-record action. Use op='no_change' rather than omitting records.",
    )
    rationale: str = Field(
        description="One-line why this record needs (or doesn't need) the recommended action.",
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
            "a human decision is required before any change. "
            "If op='batch_text_substitute', `affected_records` MUST be populated with "
            "every record to touch; the executor reads only that list, not `args`."
        ),
    )
    affected_records: List[AffectedRecord] = Field(
        default_factory=list,
        description=(
            "Per-record inventory — every concrete row the investigation inspected, "
            "with a per-record recommendation (op=update_text|no_change|delete_edge|delete_node). "
            "Populated by harvesting the planner's enumeration turns — every entry "
            "must trace to a kg_query result the planner emitted. NEVER invent records "
            "the planner didn't list. Empty list is fine when the diagnosis doesn't "
            "concern specific rows (e.g. design questions, narrative-vs-calendar tension)."
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
