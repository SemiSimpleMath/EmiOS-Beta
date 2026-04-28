from pydantic import BaseModel, Field


class web_scroll_args(BaseModel):
    direction: str = Field(default="down", description="Scroll direction: 'down' or 'up'.")
    amount: int = Field(default=420, description="Pixels per wheel step (positive integer).")
    steps: int = Field(default=1, description="Number of scroll steps to apply.")
    settle_ms: int = Field(default=120, description="Wait time after each step in milliseconds.")
    capture_snapshot: bool = Field(
        default=True,
        description="If true, capture browser_snapshot after scrolling and include it in result data.",
    )


class web_scroll_arguments(BaseModel):
    tool_name: str
    arguments: web_scroll_args

