from pydantic import BaseModel, Field


class InputSchema(BaseModel):
    """
    Input arguments for `shared::vision_prose_scout`.

    The caller must pass a screenshot file path so the agent can analyze it.
    """

    image: str = Field(
        ...,
        description="Absolute path to a PNG screenshot file on disk (e.g. /home/user/emiAi/uploads/temp/mcp_....png).",
    )

