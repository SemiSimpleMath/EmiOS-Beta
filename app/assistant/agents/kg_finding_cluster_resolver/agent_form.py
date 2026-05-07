from typing import List, Optional

from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Verdict on a candidate cluster of KG-maintenance findings.

    A "candidate cluster" is a set of pending findings that share a mechanical
    anchor (typically the same primary_node_id, sometimes also the same
    finding_type). The agent reads the human-readable reasons + evidence on
    each finding and decides whether they actually share ONE root question
    that would resolve all of them at once.

    If yes, it picks a lead, names the root question, and lists which of the
    candidate's finding_ids belong to the cluster. If no, it returns
    is_same_root=False and the cluster is dissolved (each finding stays
    independent).

    The agent never auto-acts; it only decides clustering. Resolution actions
    happen via the existing review/execute path on the lead.
    """

    is_same_root: bool = Field(
        description=(
            "True iff some SUBSET (size >= 2) of the candidate findings "
            "would be resolved by answering ONE underlying question. The "
            "subset doesn't have to include every candidate — list only "
            "the findings that share the root in member_finding_ids; "
            "non-matching ones stay independent. Return False only when "
            "no subset of >= 2 findings share a root."
        ),
    )

    root_question: Optional[str] = Field(
        default=None,
        description=(
            "When is_same_root=True: the single canonical question the user "
            "would answer to resolve the whole cluster. Phrased as a direct "
            "question to the user — e.g. 'Did Annika stop taking art "
            "lessons? When?', not 'There is a contradiction about Annika...'. "
            "Leave null when is_same_root=False."
        ),
    )

    lead_finding_id: Optional[str] = Field(
        default=None,
        description=(
            "When is_same_root=True: the id of one finding in the cluster "
            "to keep as the surfaced lead. Pick the one whose reason most "
            "directly states the root question. Other cluster members will "
            "be marked superseded_by=this_id and hidden from default views. "
            "Leave null when is_same_root=False."
        ),
    )

    member_finding_ids: List[str] = Field(
        default_factory=list,
        description=(
            "When is_same_root=True: the full set of finding_ids that "
            "belong to this cluster (including the lead). Must be a subset "
            "of the candidate's finding_ids. The agent MAY exclude some "
            "candidate findings if it judges they don't actually share the "
            "same root — those stay independent. Leave empty when "
            "is_same_root=False."
        ),
    )

    reason: str = Field(
        description=(
            "Short justification for the verdict. Names the shared assertion "
            "or fact when is_same_root=True; names what differs between the "
            "candidates when is_same_root=False."
        ),
    )
