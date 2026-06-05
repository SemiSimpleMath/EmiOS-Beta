from pydantic import BaseModel


class capture_camera_snapshot_args(BaseModel):
    camera_id: str


class capture_camera_snapshot_arguments(BaseModel):
    tool_name: str
    arguments: capture_camera_snapshot_args
