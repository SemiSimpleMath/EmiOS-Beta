from typing import List

from pydantic import BaseModel, Field


class pod_fetch_args(BaseModel):
    pod_ids: List[str] = Field(
        ...,
        description="List of pod_ids to fetch. Example: ['datapod:chat_cluster:abc123'].",
    )


class pod_fetch_arguments(BaseModel):
    tool_name: str
    arguments: pod_fetch_args


pod_fetch_arguments.model_rebuild()
