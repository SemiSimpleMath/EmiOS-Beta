"""capture_camera_snapshot — agent-facing camera capture that ALWAYS mints a pod.

Sister to ``ring_camera_control`` but with a different intent: the agent
asked for an image to use, so the result MUST be a pod_id the agent can
immediately attach to email, pass to vision_image_describe, etc. The
background camera_dispatch routine still runs over the captured file
(scene analysis, escalation policy) but its skip-if-uninteresting pod
policy doesn't apply here — when an agent explicitly captures, the pod
gets minted regardless of analyzed category.

Architecture:
  ring_camera_control.get_snapshot  — low-noise: lets the dispatcher decide
                                       whether to mint based on scene policy.
                                       Right for background motion polls.
  capture_camera_snapshot           — always mints, returns pod_id directly.
                                       Right for agent-requested captures
                                       where the pod is the deliverable.

Today supports Ring cameras only. Extending to local (Tapo / RTSP) cameras
is a one-branch addition when needed.
"""
from __future__ import annotations

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.lib.tools.smart_home_gateway import send_smart_home_command
from app.assistant.pod_store.file_ingest import ingest_file
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from pathlib import Path

logger = get_logger(__name__)


class CaptureCameraSnapshotTool(BaseTool):
    requires_approval = True

    def __init__(self):
        super().__init__("capture_camera_snapshot")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
            arguments = tool_data.get("arguments", {}) if isinstance(tool_data.get("arguments"), dict) else {}
            camera_id = str(arguments.get("camera_id") or "").strip()
            if not camera_id:
                raise ValueError("capture_camera_snapshot requires non-empty camera_id.")

            # 1. Capture via smart_home_gateway (same backend as
            #    ring_camera_control.get_snapshot). The bridge endpoint
            #    wraps the snapshot fields under .result:
            #       {ok, integration, command, result: {camera_id,
            #        snapshot_path, size_bytes, captured_at}}
            response_data = send_smart_home_command(
                integration="ring",
                command="get_snapshot",
                arguments={"camera_id": camera_id},
                request_id=tool_message.request_id,
            )
            result_dict = (response_data or {}).get("result") or {}
            snapshot_path = str(result_dict.get("snapshot_path") or "").strip()
            if not snapshot_path:
                raise RuntimeError(
                    "smart_home_gateway returned no snapshot_path for "
                    f"camera_id={camera_id!r} (response keys: "
                    f"{sorted((response_data or {}).keys())}; "
                    f"result keys: {sorted(result_dict.keys())}); cannot mint pod."
                )
            src = Path(snapshot_path).resolve()
            if not src.is_file():
                raise FileNotFoundError(
                    f"snapshot_path {src} does not exist on disk; capture may have failed silently."
                )

            # 2. Mint pod from the file — bypasses the dispatcher's
            #    skip-unless-interesting policy. This pod is the agent's
            #    handle to the image for downstream tool calls.
            pod = ingest_file(src_path=src, source_kind="agent_camera_snapshot")
            md = pod.metadata or {}

            return ToolResult(
                result_type="capture_camera_snapshot_result",
                content=(
                    f"Captured snapshot from camera {camera_id} and minted pod {pod.pod_id}. "
                    f"Attach via pod_ids=[{pod.pod_id!r}]."
                ),
                data={
                    "ok": True,
                    "pod_id": pod.pod_id,
                    "camera_id": camera_id,
                    "snapshot_path": str(src),
                    "captured_at": result_dict.get("captured_at"),
                    "size_bytes": md.get("file_size_bytes"),
                    "mime_type": md.get("mime_type"),
                },
            )
        except Exception as e:
            logger.error("capture_camera_snapshot failed: %s", e)
            logger.debug("capture_camera_snapshot exception details", exc_info=True)
            return make_tool_error(
                error_code="capture_camera_snapshot_failed",
                message=str(e),
                abort_policy="abort_tool",
                retryable=False,
                details={"tool_name": "capture_camera_snapshot"},
            )


def get_tool_class():
    return CaptureCameraSnapshotTool
