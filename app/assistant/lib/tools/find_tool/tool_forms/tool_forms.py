from typing import Optional

from pydantic import BaseModel, Field


class find_tool_args(BaseModel):
    query: Optional[str] = Field(
        "",
        description="Tool search query by name or purpose.",
    )
    tool_kind: Optional[str] = Field(
        "all",
        description="Tool set to search: all | local | installed_mcp | mcp_available.",
    )
    limit: Optional[int] = Field(8, description="Maximum number of matches to return (up to 200).")
    include_descriptions: Optional[bool] = Field(
        True, description="Deprecated. Descriptions are always included in find_tool results."
    )


class find_tool_arguments(BaseModel):
    tool_name: str = "find_tool"
    arguments: find_tool_args


find_tool_arguments.model_rebuild()
