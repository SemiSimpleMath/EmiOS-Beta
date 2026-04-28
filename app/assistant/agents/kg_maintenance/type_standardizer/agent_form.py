from typing import List

from pydantic import BaseModel


class AgentForm(BaseModel):
    truly_smilar_clusters: List[List[str]]
