from typing import List, Optional

from pydantic import BaseModel, Field


class capture_and_describe_monitors_args(BaseModel):
    monitor_indices: List[int] = Field(
        default_factory=lambda: [2, 3],
        description="Ordered monitor indices to capture and describe (1-based).",
    )
    question: Optional[str] = Field(
        default=None,
        description="Optional question passed to vision_image_describe for each screenshot.",
    )
    output_file_path: Optional[str] = Field(
        default="resources/dayflow_pipeline_outputs/resource_desktop_activity_recent.md",
        description="Path for the generated snippet when write_output is true.",
    )
    write_output: Optional[bool] = Field(
        default=True,
        description="Whether to overwrite output_file_path with the snippet.",
    )
    ensure_newline: Optional[bool] = Field(
        default=True,
        description="Ensure trailing newline when writing output.",
    )


class capture_and_describe_monitors_arguments(BaseModel):
    tool_name: str = "capture_and_describe_monitors"
    arguments: capture_and_describe_monitors_args


capture_and_describe_monitors_arguments.model_rebuild()
