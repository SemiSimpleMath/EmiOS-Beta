from pydantic import BaseModel, Field


class read_tool_result_args(BaseModel):
    tool_result_id: str = Field(..., description="The tool_result_id marker from history (e.g., [tool_result_id: ...]).")
    field: str = Field(
        default="",
        description="Optional dotted path into the stored JSON (e.g., tool_result.data.call_response.result).",
    )
    max_chars: int = Field(
        default=20000,
        description="Maximum characters to return. Use 0 for no truncation (can be very large).",
    )
    contains: str = Field(
        default="",
        description=(
            "Optional substring filter. If provided, only lines containing this substring will be returned "
            "(applied after selecting `field` and before `max_chars` truncation). Useful for huge snapshots, "
            "e.g. contains='ref=' or contains='link '."
        ),
    )


class read_tool_result_arguments(BaseModel):
    tool_name: str
    arguments: read_tool_result_args

