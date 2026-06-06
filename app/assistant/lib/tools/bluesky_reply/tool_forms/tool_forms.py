from pydantic import BaseModel, Field


class bluesky_reply_args(BaseModel):
    post_ref: str = Field(
        ...,
        description="The [bN] ref of the post to reply to (from a prior bluesky_timeline call), e.g. 'b1'.",
    )
    text: str = Field(
        ...,
        description="The reply text (max 300 characters).",
    )


class bluesky_reply_arguments(BaseModel):
    tool_name: str
    arguments: bluesky_reply_args
