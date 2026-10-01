from typing import List

from pydantic import BaseModel, Field


class Cluster(BaseModel):
    label: str = Field(
        min_length=1,
        description="The topic's name: a short noun phrase a person scanning the list recognises at "
                    "once. To add beliefs to an existing cluster, copy its label exactly.")
    belief_ids: List[str] = Field(
        description="The ids of the beliefs placed in this cluster, copied exactly from the beliefs to place.")


class AgentForm(BaseModel):
    clusters: List[Cluster] = Field(
        description="Every belief to place, in exactly one cluster: an existing one (its label copied) "
                    "or a new one.")
    reasoning: str = Field(description="Two or three sentences on the splits and merges you made.")
