from typing import List, Optional

from pydantic import BaseModel, Field


class pod_search_args(BaseModel):
    tags: Optional[List[str]] = Field(
        default=None,
        description="Tag names to match (OR semantics). Example: ['food','health'].",
    )
    scope: Optional[str] = Field(
        default=None,
        description="Exact room_id filter (e.g. 'master_room'). Omit to search all rooms.",
    )
    since: Optional[str] = Field(
        default=None,
        description="Time window: '24h', '3d', '2w', '1m', 'today', or ISO timestamp.",
    )
    limit: Optional[int] = Field(
        default=20,
        description="Max headers to return. Default 20.",
    )


class pod_search_arguments(BaseModel):
    tool_name: str
    arguments: pod_search_args


pod_search_arguments.model_rebuild()
