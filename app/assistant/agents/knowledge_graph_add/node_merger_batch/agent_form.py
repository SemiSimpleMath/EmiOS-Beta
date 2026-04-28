from typing import List

from pydantic import BaseModel


class NodeDecisionItem(BaseModel):
    node_index: int
    # Required pre-decision analysis. Fill BEFORE choosing merge_nodes.
    # 1) new_node_subject: who/what the new node is about (from its sentence).
    # 2) candidate_subjects: one short line per candidate: "<candidate_id>: <subject>".
    # 3) subject_match_verdict: for the chosen candidate (if any), say explicitly
    #    whether the subjects are the same real-world entity/participants.
    new_node_subject: str
    candidate_subjects: str
    subject_match_verdict: str
    reasoning: str
    merge_nodes: bool
    merged_node_id: str


class AgentForm(BaseModel):
    decisions: List[NodeDecisionItem]
