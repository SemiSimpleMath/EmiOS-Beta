from pydantic import BaseModel, Field


class bluesky_timeline_args(BaseModel):
    limit: int = Field(
        default=20,
        description="How many recent posts to fetch (1-50). Each is shown as a [bN] ref you can reply to or like.",
    )


class bluesky_timeline_arguments(BaseModel):
    tool_name: str
    arguments: bluesky_timeline_args
