from pydantic import BaseModel, Field


class bluesky_like_args(BaseModel):
    post_ref: str = Field(
        ...,
        description="The [bN] ref of the post to like (from a prior bluesky_timeline call), e.g. 'b1'.",
    )


class bluesky_like_arguments(BaseModel):
    tool_name: str
    arguments: bluesky_like_args
