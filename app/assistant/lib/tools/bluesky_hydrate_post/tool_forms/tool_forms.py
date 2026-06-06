from pydantic import BaseModel, Field


class bluesky_hydrate_post_args(BaseModel):
    post_ref: str = Field(
        ...,
        description="The [bN] ref of the post to open (from a prior bluesky_timeline call), e.g. 'b1'.",
    )


class bluesky_hydrate_post_arguments(BaseModel):
    tool_name: str
    arguments: bluesky_hydrate_post_args
