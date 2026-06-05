import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.slack_interface.slack_room_config import get_slack_room_config
from app.assistant.utils.path_utils import get_repo_root

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB


def _real_slack_send_enabled() -> bool:
    cfg = get_slack_room_config()
    return bool(cfg.get("allow_real_slack_send", False))


def _get_slack_token() -> str:
    for key in ("SLACK_TOKEN", "EMI_SLACK_TOKEN", "SLACK_BOT_TOKEN"):
        token = str(os.environ.get(key) or "").strip()
        if token:
            return token
    return ""

class SlackTool(BaseTool):
    def __init__(self):
        super().__init__('slack_tool')
        self.client = WebClient(token=_get_slack_token())
        self.last_user_timestamps = {}
        self.bot_user_id = None

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            arguments = tool_message.tool_data.get("arguments", {})
            action = tool_message.tool_data.get("tool_name")
            if not action:
                raise ValueError("Missing tool_name in tool_data.")

            handler = getattr(self, f"handle_{action}", None)
            if not handler:
                raise ValueError(f"Unsupported Slack action: {action}")
            return handler(arguments)

        except Exception as e:
            logger.error("SlackTool execution error")
            logger.debug("slackTool execution error exception details", exc_info=True)
            return make_tool_error(
                error_code="slack_tool_execute_failed",
                message=str(e),
                abort_policy="abort_tool",
                retryable=False,
            )



    def handle_get_messages(self, arguments: Dict[str, Any]):
        try:
            channel_id = arguments.get("channel_id")
            oldest = arguments.get("oldest")
            if not channel_id:
                raise ValueError("channel_id is required")

            response = self.client.conversations_history(
                channel=channel_id,
                limit=100,
                oldest=oldest,
                inclusive=False
            )

            messages = response.get("messages", [])
            messages = [m for m in messages if m.get("subtype") != "bot_message"]

            user_cache = {}
            formatted_messages = []
            max_ts = oldest

            for msg in messages:
                user_id = msg.get("user", "unknown")
                text = msg.get("text", "")
                ts = msg.get("ts")
                if not ts:
                    continue
                if not max_ts or ts > max_ts:
                    max_ts = ts

                if user_id not in user_cache:
                    try:
                        name = self.resolve_speaker_name(msg, user_cache)
                        user_cache[user_id] = name
                    except (SlackApiError, TimeoutError, Exception) as e:
                        # Handle timeout and other errors gracefully
                        logger.warning(f"Error resolving user name for {user_id}: {e}")
                        user_cache[user_id] = user_id

                images = self._download_message_images(msg, ts)
                formatted_messages.append({
                    "name": user_cache[user_id],
                    "user_id": user_id,
                    "text": text,
                    "ts": ts,
                    "thread_ts": msg.get("thread_ts"),
                    "images": images,
                })

            formatted_messages.reverse()

            return ToolResult(result_type="messages", content="Messages retrieved", data_list=formatted_messages)

        except (SlackApiError, TimeoutError, Exception) as e:
            logger.error(f"Slack API error during get_messages: {e}")
            logger.debug("Slack get_messages exception details", exc_info=True)
            return make_tool_error(
                error_code="slack_get_messages_failed",
                message=f"Slack API error during get_messages: {e}",
                abort_policy="abort_tool",
                retryable=True,
            )

    def handle_send_message(self, arguments: Dict[str, Any]):
        try:
            channel_id = arguments.get("channel_id")
            text = arguments.get("text")
            thread_ts = arguments.get("thread_ts")
            if not channel_id or not text:
                raise ValueError("Both channel_id and text are required")

            if not _real_slack_send_enabled():
                logger.warning(
                    "Slack send blocked by safety gate (set configs/slack_room.json: allow_real_slack_send=true to enable). "
                    "channel_id=%s",
                    channel_id,
                )
                return ToolResult(
                    result_type="dry_run",
                    content="Slack send blocked by safety gate (allow_real_slack_send=false).",
                    data={"channel_id": channel_id, "text_preview": str(text)[:500]},
                )

            payload = {"channel": channel_id, "text": text}
            if isinstance(thread_ts, str) and thread_ts.strip():
                payload["thread_ts"] = thread_ts.strip()
            self.client.chat_postMessage(**payload)
            return ToolResult(result_type="success", content="Message sent.")
        except SlackApiError as e:
            logger.error("Slack API error during send_message")
            logger.debug("Slack send_message exception details", exc_info=True)
            return make_tool_error(
                error_code="slack_send_message_failed",
                message=str(e),
                abort_policy="abort_tool",
                retryable=True,
            )

    def handle_get_bot_user_id(self, arguments: Dict[str, Any]) -> ToolResult:
        try:
            if not self.bot_user_id:
                response = self.client.auth_test()
                self.bot_user_id = response.get("user_id")
            return ToolResult(result_type="bot_user_id", content=self.bot_user_id)
        except SlackApiError as e:
            return ToolResult(result_type="Error", content=e)


    def resolve_speaker_name(self, msg: dict, user_cache: dict) -> str:
        if "user" in msg:
            user_id = msg["user"]
            if user_id not in user_cache:
                try:
                    info = self.client.users_info(user=user_id)
                    user = info.get("user", {})
                    name = user.get("real_name") or user.get("name") or user_id
                    user_cache[user_id] = name
                except SlackApiError:
                    user_cache[user_id] = user_id
            return user_cache[user_id]

        if "bot_id" in msg:
            return msg.get("username") or msg["bot_id"]

        return "unknown"

    def _repo_root(self) -> Path:
        return get_repo_root()

    def _image_download_dir(self) -> Path:
        from app.assistant.utils.path_utils import get_uploads_dir
        return get_uploads_dir() / "temp" / "slack_images"

    def _sanitize_filename(self, name: str) -> str:
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", name.strip())
        return safe or "slack_image"

    def _download_message_images(self, msg: dict, ts: str) -> List[str]:
        files = msg.get("files", []) if isinstance(msg, dict) else []
        if not isinstance(files, list) or not files:
            return []
        images: List[str] = []
        for f in files:
            if not isinstance(f, dict):
                continue
            mimetype = str(f.get("mimetype") or "")
            filetype = str(f.get("filetype") or "")
            if not (mimetype.startswith("image/") or filetype.lower() in {"png", "jpg", "jpeg", "gif", "webp"}):
                continue
            size = f.get("size")
            if isinstance(size, int) and size > MAX_IMAGE_BYTES:
                logger.warning("Skipping large Slack image (size=%s bytes)", size)
                continue
            path = self._download_slack_file(f, ts)
            if path:
                images.append(path)
        return images

    def _download_slack_file(self, file_obj: Dict[str, Any], ts: str) -> Optional[str]:
        slack_token = _get_slack_token()
        if not slack_token:
            logger.warning("SLACK_TOKEN missing; cannot download Slack images.")
            return None
        url = file_obj.get("url_private_download") or file_obj.get("url_private")
        if not isinstance(url, str) or not url.strip():
            return None
        try:
            req = Request(url, headers={"Authorization": f"Bearer {slack_token}"})
            with urlopen(req, timeout=15) as resp:
                content_type = resp.headers.get("Content-Type", "")
                if isinstance(content_type, str) and content_type and not content_type.startswith("image/"):
                    logger.warning("Slack file is not an image (Content-Type=%s)", content_type)
                    return None
                content = resp.read()
            # Detect image type from magic bytes (imghdr removed in Python 3.13).
            img_kind = None
            if content[:8] == b'\x89PNG\r\n\x1a\n':
                img_kind = "png"
            elif content[:3] == b'\xff\xd8\xff':
                img_kind = "jpeg"
            elif content[:4] == b'GIF8':
                img_kind = "gif"
            elif content[:4] == b'RIFF' and content[8:12] == b'WEBP':
                img_kind = "webp"
            if img_kind is None:
                logger.warning("Downloaded Slack file is not a recognized image format")
                return None
            file_id = str(file_obj.get("id") or "file")
            name = str(file_obj.get("name") or f"image.{img_kind}")
            safe_name = self._sanitize_filename(name)
            ts_token = str(ts).replace(".", "_")
            ext = f".{img_kind}"
            if not safe_name.lower().endswith(ext):
                safe_name = f"{safe_name}{ext}"
            filename = f"slack_{file_id}_{ts_token}_{safe_name}"
            download_dir = self._image_download_dir()
            download_dir.mkdir(parents=True, exist_ok=True)
            path = download_dir / filename
            if path.exists():
                return str(path)
            path.write_bytes(content)
            return str(path)
        except (HTTPError, URLError, TimeoutError, Exception) as e:
            logger.warning("Failed to download Slack image %s: %s", url, e)
            return None


def test_get_messages():
    tool = SlackTool()
    channel_id = "C08AB0R54HM"

    msg = ToolMessage(
        tool_name="get_messages",
        tool_data={
            "tool_name": "get_messages",
            "arguments": {
                "channel_id": channel_id,
                "oldest": None  # or a string timestamp if testing incrementally
            }
        }
    )

    result = tool.execute(msg)

    print("=== Get Messages Result ===")
    if isinstance(result, str):
        print("Error or timestamp:", result)
    elif isinstance(result, tuple) and len(result) == 2:
        messages = result

        for msg in messages:
            print(f"- [{msg['ts']}] {msg['name']}: {msg['text']}")
    else:
        print("Unexpected result format:", result)


def test_send_message():
    tool = SlackTool()
    channel_id = "C08AB0R54HM"

    msg = ToolMessage(
        tool_name="send_message",
        tool_data={
            "tool_name": "send_message",
            "arguments": {
                "channel_id": channel_id,
                "text": "Hello from SlackTool test 🎯"
            }
        }
    )

    result = tool.execute(msg)
    print("=== Send Message Result ===")
    if hasattr(result, "json"):
        print(result.model_dump_json(indent=2))
    else:
        print(result)



if __name__ == "__main__":
    test_get_messages()
    #test_send_message()
