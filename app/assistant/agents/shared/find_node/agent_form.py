from pydantic import BaseModel
from typing import List


class ResultSummary(BaseModel):
    result_nodes: List[str]
    methodology: str

class AgentForm(BaseModel):
    what_i_am_thinking: str
    find_node_checklist: List[str]
    find_node_found_information: str
    plan: str
    current_step: str
    action: str
    action_input: str
    result: ResultSummary

