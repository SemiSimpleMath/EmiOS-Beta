from typing import Optional

from pydantic import BaseModel, Field


class install_tool_args(BaseModel):
    tool_name: Optional[str] = Field(
        None,
        description="Namespaced MCP tool, e.g. mcp::npm/server-google-maps::maps_distance_matrix.",
    )
    server_id: Optional[str] = Field(
        None,
        description="Trusted MCP server_id, e.g. npm/server-google-maps.",
    )
    mcp_tool_name: Optional[str] = Field(
        None,
        description="MCP tool name from the selected server, e.g. maps_distance_matrix.",
    )
    install_source: Optional[str] = Field(
        "agent",
        description="Install source tag for registry metadata.",
    )
    launch_id: Optional[str] = Field(
        None,
        description="Optional launch option id from server entry.",
    )
    timeout_s: Optional[float] = Field(
        15.0,
        description="Timeout in seconds for cache refresh launch when needed.",
    )


class install_tool_arguments(BaseModel):
    tool_name: str = "install_tool"
    arguments: install_tool_args


install_tool_arguments.model_rebuild()
