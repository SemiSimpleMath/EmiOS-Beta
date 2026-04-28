
from pydantic import BaseModel

class AgentForm(BaseModel):
    reasoning: str
    merge_nodes: bool
    merged_node_id: str


