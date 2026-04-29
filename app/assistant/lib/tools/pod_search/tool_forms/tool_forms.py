from typing import List, Optional

from pydantic import BaseModel, Field


class pod_search_args(BaseModel):
    query: Optional[str] = Field(
        default=None,
        description="Free-text substring search across pod one_liner + body (case-insensitive). Multi-word queries are tokenized; matches in one_liner rank above body-only.",
    )
    kind: Optional[str] = Field(
        default=None,
        description="Restrict to one pod kind. Currently 'chat_cluster', 'email', or 'image'.",
    )
    linked_to_entity: Optional[str] = Field(
        default=None,
        description="Only pods that an Entity with this label has KG edges to. E.g. 'Jukka' → pods Jukka is depicted_in / has_profile_image-of / has_video-of / etc.",
    )
    linked_via: Optional[List[str]] = Field(
        default=None,
        description="When filtering by linked_to_entity, restrict to these edge relationship_types. E.g. ['depicted_in','has_profile_image']. Omit for any edge.",
    )
    tags: Optional[List[str]] = Field(
        default=None,
        description="Tag names to match (OR semantics). Example: ['food','health']. Chat_cluster pods only — emails have no tags.",
    )
    scope: Optional[str] = Field(
        default=None,
        description="Exact scope_id filter (e.g. room_id 'master_room', or email account_id). Omit to search all scopes.",
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
