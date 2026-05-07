from typing import List, Optional

from pydantic import BaseModel, Field


class MutationApplied(BaseModel):
    op: str = Field(
        description=(
            "The mutation tool that was called: kg_close_state, "
            "kg_update_node_field, kg_create_state_node, kg_rename_label, "
            "kg_create_edge, kg_delete_edge, kg_delete_node."
        ),
    )
    node_id: Optional[str] = Field(
        default=None,
        description="Primary node the mutation targeted, when applicable.",
    )
    summary: str = Field(
        description="One-line human description of what changed.",
    )
    revision_log_id: Optional[str] = Field(
        default=None,
        description="kg_revision_log row id if the tool returned one.",
    )


class RegenerationApplied(BaseModel):
    kind: str = Field(
        description="One of: 'entity_card', 'wiki_page'.",
    )
    entity_label: str
    summary: str = Field(
        description="One-line summary of what was regenerated.",
    )


class FindingResolved(BaseModel):
    finding_id: str
    action: str = Field(
        description="One of: 'executed', 'rejected', 'investigated', 'escalated'.",
    )
    reason: str = Field(default="", description="Short reason for this finding's outcome.")


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
    """Output of the resolution manager's final-answer agent.

    Captures what the manager actually changed — mutations, regenerations,
    findings closed — so the maintenance UI can show the user a precise
    "here's what was done" report instead of a generic "ok we processed it."
    """

    summary: str = Field(
        description=(
            "One-paragraph plain-English summary of the resolution: which "
            "node(s) changed, what changed about them, what was regenerated, "
            "how many findings were closed. Lead with the outcome."
        ),
    )

    user_prose: str = Field(
        description=(
            "The exact prose the user submitted, echoed back so the report "
            "stands alone (audit-ready)."
        ),
    )

    mutations: List[MutationApplied] = Field(
        default_factory=list,
        description=(
            "Every KG mutation that was successfully applied during this "
            "resolution. Order matches application order. Empty list "
            "means no mutations happened (escalation case)."
        ),
    )

    regenerations: List[RegenerationApplied] = Field(
        default_factory=list,
        description=(
            "Entity cards and wiki pages that were regenerated after the "
            "mutations. Empty list when no dependents needed refreshing."
        ),
    )

    findings_resolved: List[FindingResolved] = Field(
        default_factory=list,
        description=(
            "Every finding in the cluster, with its outcome. The cluster "
            "lead and its siblings should each appear here."
        ),
    )

    open_questions: List[str] = Field(
        default_factory=list,
        description=(
            "Questions that came up during the resolution but couldn't be "
            "answered without further input. Empty when fully resolved."
        ),
    )

    completed: bool = Field(
        description=(
            "True when the resolution is fully done (all findings resolved, "
            "all dependents regenerated). False when partial or escalated."
        ),
    )

    # Standard envelope
    final_answer_answer: str = Field(
        description=(
            "Markdown rendering of the structured fields above, suitable "
            "for human reading. Sections: ## Resolution summary, "
            "## Mutations applied, ## Regenerations, ## Findings closed, "
            "## Open questions (omit when empty)."
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
