from pydantic import BaseModel


class local_camera_snapshot_args(BaseModel):
    camera_alias: str
    timeout_seconds: int | None = None


class local_camera_snapshot_arguments(BaseModel):
    tool_name: str
    arguments: local_camera_snapshot_args
