from typing import List, Optional

from pydantic import BaseModel, Field


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
    # ---- structured outcome (programmatic consumers) ----
    outcome: str = Field(
        description=(
            "One of: applied, escalated, no_action, error. "
            "applied = a kg mutation was committed; "
            "escalated = the finding was routed to user review; "
            "no_action = the finding was resolved without any KG change; "
            "error = something went wrong (see error_message)."
        ),
    )
    op_applied: Optional[str] = Field(
        default=None,
        description="Mutation op when outcome=applied: merge_nodes / rename_label / update_node_field. Null otherwise.",
    )
    revision_log_id: Optional[str] = Field(
        default=None,
        description="The kg_revision_log row id of the committed mutation. Null when no mutation ran.",
    )
    finding_status: Optional[str] = Field(
        default=None,
        description="Finding's status after this run: executed (resolved) or approved (escalated to user review).",
    )
    error_message: Optional[str] = Field(
        default=None,
        description="Set only when outcome=error.",
    )

    # ---- standard envelope (manager_exit_node + room formatters) ----
    final_answer_answer: str = Field(
        description="Markdown summary of what happened, suitable for human reading.",
    )
    result_summary: str = Field(
        default="",
        description="One-sentence outcome (≤150 chars).",
    )
    final_answer_sources: List[str] = Field(default_factory=list)
    final_answer_detail_level: str = "brief"
    final_answer_data_list: List[FinalAnswerDataItem] = Field(default_factory=list)
    final_answer_task: Optional[str] = ""
    final_answer_what_was_done: Optional[str] = ""
    final_answer_interesting_info: Optional[str] = ""
