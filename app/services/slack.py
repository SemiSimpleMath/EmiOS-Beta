from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class SlackConfig:
    token: str


class SlackService:
    """Minimal Slack outbound sender for asynchronous emits.

    Used by `EmiEventRelay` when a background event publishes a
    `socket_emit` whose `reply_to.type == "slack"`. The synchronous
    inbound→manager→reply path lives in
    `app/assistant/room_session_manager/services/surfaces/slack_inbound_service.py`
    and uses its own `slack_transport.send_reply` — separate codepath,
    same `slack_sdk.WebClient.chat_postMessage` call underneath.

    Env vars (first non-empty wins):
    - ``SLACK_TOKEN``
    - ``EMI_SLACK_TOKEN``
    - ``SLACK_BOT_TOKEN``
    """

    def __init__(self, config: SlackConfig | None = None) -> None:
        if config is None:
            config = SlackConfig(token=_resolve_token())
        self._config = config

        if not self._config.token:
            raise RuntimeError(
                "Slack outbound not configured. Set SLACK_TOKEN (or EMI_SLACK_TOKEN / SLACK_BOT_TOKEN)."
            )

        try:
            from slack_sdk import WebClient
        except Exception as e:
            raise RuntimeError(
                "slack_sdk not installed. Install with `pip install slack_sdk`."
            ) from e

        self._client = WebClient(token=self._config.token)

    def send_message(
        self,
        *,
        channel_id: str,
        text: str,
        thread_ts: Optional[str] = None,
    ) -> dict[str, Any]:
        channel_id = (channel_id or "").strip()
        text = (text or "").strip()
        if not channel_id or not text:
            raise ValueError("Missing channel_id or text for Slack send.")

        payload: dict[str, Any] = {"channel": channel_id, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts

        try:
            from slack_sdk.errors import SlackApiError
        except Exception:
            SlackApiError = Exception  # type: ignore[assignment, misc]

        try:
            resp = self._client.chat_postMessage(**payload)
        except SlackApiError as e:  # type: ignore[misc]
            logger.error(
                "Slack send failed: channel=%s err=%s", channel_id, getattr(e, "response", e)
            )
            raise

        return getattr(resp, "data", {}) if hasattr(resp, "data") else dict(resp)


def _resolve_token() -> str:
    for key in ("SLACK_TOKEN", "EMI_SLACK_TOKEN", "SLACK_BOT_TOKEN"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return ""
