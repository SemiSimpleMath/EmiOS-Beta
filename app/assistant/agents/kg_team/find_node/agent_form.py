from pydantic import BaseModel
from typing import List, Union


class Node(BaseModel):
    node_id: str
    node_type: str
    node_label: str
    semantic_label: str
    description: Union[str,None]

class ResultSummary(BaseModel):
    result_nodes: Union[List[Node],None]

class AgentForm(BaseModel):
    what_i_am_thinking: str
    find_node_checklist: List[str]
    find_node_found_information: str
    plan: str
    current_step: str
    action: str
    action_input: str
    result: ResultSummary

