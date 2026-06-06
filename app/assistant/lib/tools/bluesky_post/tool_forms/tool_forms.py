from pydantic import BaseModel, Field


class bluesky_post_args(BaseModel):
    text: str = Field(
        ...,
        description="The post text (max 300 characters). For replies use bluesky_reply instead.",
    )


class bluesky_post_arguments(BaseModel):
    tool_name: str
    arguments: bluesky_post_args
