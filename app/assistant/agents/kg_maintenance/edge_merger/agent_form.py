from typing import List

from pydantic import BaseModel


class AgentForm(BaseModel):
    edges_for_merging: List[str]
    human_review: bool
