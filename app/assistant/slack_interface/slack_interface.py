import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.slack.slack import SlackTool
from app.assistant.rooms.room_bootstrap import ensure_slack_room, make_slack_room_id
from app.assistant.slack_interface.slack_room_config import (
    get_slack_channel_id,
    get_slack_room_config,
)
from app.assistant.utils.identity_names import (
    get_required_assistant_name,
    resolve_display_name,
)
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.pydantic_classes import ToolMessage
from app.assistant.utils.logging_config import get_logger
from app.assistant.runtime import start_monitored_thread

logger = get_logger(__name__)


class SlackInterface:
    """
    Slack polling adapter for room-based flow.

    - Polls a Slack channel.
    - Detects genuinely new inbound user messages.
    - Routes latest relevant message(s) into RoomSessionManager.
    - Never triggers room manager when no new inbound chat exists.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if not cls._instance:
                cls._instance = super(SlackInterface, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        self.tool = SlackTool()
        self.channel_id = get_slack_channel_id()
        self.poll_interval = 20
        self._stop_flag = threading.Event()
        self._status_lock = threading.Lock()
        self._enabled = False
        self.thread = None

        # Only start polling if Slack credentials are configured.
        import os
        slack_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
        if not slack_token:
            logger.info("SlackInterface: No SLACK_BOT_TOKEN configured, polling disabled.")
            return

        if os.environ.get("SLACK_POLLING_DISABLED", "").strip().lower() in ("1", "true", "yes"):
            logger.info("SlackInterface: SLACK_POLLING_DISABLED set, polling disabled. Events webhook only.")
            return

        cfg = get_slack_room_config()
        self.use_room_mode = bool(cfg.get("use_room_mode", False))
        self.room_send_reply = bool(cfg.get("send_reply", False))
        self.message_persistence_mode = str(cfg.get("message_persistence_mode") or "global_blackboard_only").strip()
        self.poll_limit = int(cfg.get("poll_limit", 100))
        self.poll_overlap_seconds = int(cfg.get("poll_overlap_seconds", 120))
        self.status_file = self._resolve_status_file(str(cfg.get("status_resource_file", "")).strip())
        self._status_cache = self._load_status()
        self._enabled = True

        self.thread = start_monitored_thread(
            owner="slack_interface",
            name="slack-poll-loop",
            target=self._poll_loop,
            daemon=True,
            kind="poll_loop",
            metadata={"component": "slack_interface", "channel_id": self.channel_id},
        )

        logger.info(
            "[SlackInterface] init: channel=%s use_room_mode=%s send_reply=%s persistence_mode=%s poll_limit=%s overlap=%ss status=%s",
            self.channel_id,
            self.use_room_mode,
            self.room_send_reply,
            self.message_persistence_mode,
            self.poll_limit,
            self.poll_overlap_seconds,
            self.status_file,
        )

    @staticmethod
    def _repo_root() -> Path:
        return get_repo_root()

    def _resolve_status_file(self, rel_or_abs_path: str) -> Path:
        if not rel_or_abs_path:
            return self._repo_root() / "resources" / "status" / "resource_slack_room_status.json"
        p = Path(rel_or_abs_path)
        return p if p.is_absolute() else (self._repo_root() / p)

    @staticmethod
    def _ts_to_float(ts: Any) -> float:
        try:
            return float(str(ts or 0))
        except Exception:
            return 0.0

    def _load_status(self) -> Dict[str, Any]:
        with self._status_lock:
            try:
                if not self.status_file.exists():
                    return {"channels": {}}
                import json

                data = json.loads(self.status_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("channels"), dict):
                    return data
            except Exception:
                logger.warning("[SlackInterface] Failed reading status file: %s", self.status_file)
            return {"channels": {}}

    def _save_status(self) -> None:
        with self._status_lock:
            try:
                import json

                self.status_file.parent.mkdir(parents=True, exist_ok=True)
                self.status_file.write_text(
                    json.dumps(self._status_cache, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as e:
                logger.warning("[SlackInterface] Failed writing status file %s: %s", self.status_file, e)

    def _get_channel_cursor(self, channel_id: str) -> float:
        channels = self._status_cache.get("channels")
        if not isinstance(channels, dict):
            return 0.0
        item = channels.get(channel_id)
        if not isinstance(item, dict):
            return 0.0
        return self._ts_to_float(item.get("last_seen_ts"))

    def _set_channel_cursor(self, channel_id: str, ts_float: float) -> None:
        channels = self._status_cache.setdefault("channels", {})
        if not isinstance(channels, dict):
            self._status_cache["channels"] = {}
            channels = self._status_cache["channels"]
        current = self._get_channel_cursor(channel_id)
        if ts_float <= current:
            return
        channels[channel_id] = {"last_seen_ts": ts_float}
        self._save_status()

    def _derive_room_id_for_message(self, msg: Dict[str, Any]) -> str:
        _ = msg  # room is channel-scoped in the new structure
        return make_slack_room_id(self.channel_id)

    def _already_ingested_in_room_history(self, *, channel_id: str, ts: str) -> bool:
        """
        Extra dedupe layer against in-memory room history.
        (Later this can include unified_log once room persistence is enabled.)
        """
        if not ts:
            return False
        try:
            msgs = DI.global_blackboard.get_messages()
        except Exception:
            return False
        for m in reversed(msgs or []):
            try:
                if getattr(m, "room_surface", None) != "slack":
                    continue
                if str(getattr(m, "transport_to", "") or "").strip() != channel_id:
                    continue
                if str(getattr(m, "transport_message_id", "") or "").strip() == ts:
                    return True
            except Exception:
                continue
        return False

    def _poll_slack_messages(self) -> Tuple[List[Dict[str, Any]], float]:
        if not self.channel_id:
            logger.debug("[SlackInterface] channel_id missing; skipping poll.")
            return [], 0.0

        last_seen = self._get_channel_cursor(self.channel_id)
        oldest = None
        if last_seen > 0:
            oldest = str(max(0.0, last_seen - float(self.poll_overlap_seconds)))

        try:
            result = self.tool.execute(
                ToolMessage(
                    tool_name="get_messages",
                    tool_data={
                        "tool_name": "get_messages",
                        "arguments": {
                            "channel_id": self.channel_id,
                            "limit": int(self.poll_limit),
                            "oldest": oldest,
                        },
                    },
                )
            )
        except Exception as e:
            logger.error("[SlackInterface] Slack poll failed: %s", e)
            logger.debug("[SlackInterface] Slack poll exception details", exc_info=True)
            return [], last_seen

        if hasattr(result, "data_list") and isinstance(result.data_list, list):
            messages = result.data_list
        elif hasattr(result, "data") and isinstance(result.data, list):
            messages = result.data
        elif isinstance(result, list):
            messages = result
        else:
            messages = []

        messages = [m for m in messages if isinstance(m, dict)]
        messages.sort(key=lambda x: self._ts_to_float(x.get("ts")))
        return messages, last_seen

    def _extract_new_inbound_messages(
        self, messages: List[Dict[str, Any]], last_seen: float
    ) -> Tuple[List[Dict[str, Any]], float]:
        inbound: List[Dict[str, Any]] = []
        max_seen = last_seen
        assistant_name = get_required_assistant_name().strip().lower()
        for m in messages:
            ts = str(m.get("ts") or "").strip()
            tsf = self._ts_to_float(ts)
            if tsf <= last_seen:
                continue
            if tsf > max_seen:
                max_seen = tsf

            sender_name = resolve_display_name(
                raw_name=str(m.get("name") or "").strip(),
                role="participant",
                external_id=str(m.get("user_id") or "").strip(),
                prefer_external_id_for_participant=True,
            )
            if sender_name.lower() == assistant_name:
                continue
            content = str(m.get("text") or "").strip()
            images = m.get("images") if isinstance(m.get("images"), list) else []
            if not content and not images:
                continue
            if self._already_ingested_in_room_history(channel_id=self.channel_id, ts=ts):
                continue
            inbound.append(m)
        return inbound, max_seen

    def _dispatch_room_messages(self, inbound_messages: List[Dict[str, Any]]) -> None:
        # One message per room per poll cycle (latest wins).
        latest_by_room: Dict[str, Dict[str, Any]] = {}
        for m in inbound_messages:
            room_id = self._derive_room_id_for_message(m)
            prev = latest_by_room.get(room_id)
            if prev is None or self._ts_to_float(m.get("ts")) >= self._ts_to_float(prev.get("ts")):
                latest_by_room[room_id] = m

        for room_id, m in latest_by_room.items():
            sender_name = resolve_display_name(
                raw_name=str(m.get("name") or "").strip(),
                role="participant",
                external_id=str(m.get("user_id") or "").strip(),
                prefer_external_id_for_participant=True,
            )
            content = str(m.get("text") or "").strip()
            image_paths = m.get("images") if isinstance(m.get("images"), list) else []
            if not content and not image_paths:
                continue
            message_ts = str(m.get("ts") or "").strip()
            sender_id = str(m.get("user_id") or "").strip()
            thread_ts = str(m.get("thread_ts") or "").strip()
            try:
                ensure_slack_room(
                    room_id=room_id,
                    channel_id=self.channel_id,
                    sender_name=sender_name,
                )
                outcome = DI.room_session_manager.handle_slack_inbound(
                    channel_id=self.channel_id,
                    body=content,
                    room_id=room_id,
                    message_ts=message_ts,
                    sender_name=sender_name,
                    sender_id=sender_id,
                    thread_ts=thread_ts,
                    image_paths=image_paths,
                    send_reply=self.room_send_reply,
                    message_persistence_mode=self.message_persistence_mode,
                )
                logger.info(
                    "[SlackInterface] room handled ts=%s room_id=%s sender=%s reply=%s",
                    message_ts,
                    room_id,
                    sender_name,
                    str(outcome.get("reply_text") or "")[:160],
                )
            except Exception as e:
                logger.error(
                    "[SlackInterface] room dispatch failed ts=%s room_id=%s sender=%s err=%s",
                    message_ts,
                    room_id,
                    sender_name,
                    e,
                )
                logger.debug("[SlackInterface] room dispatch exception details", exc_info=True)

    def _poll_loop(self):
        while not self._stop_flag.is_set():
            try:
                self.poll_once()
            except Exception as e:
                logger.error("[SlackInterface] Polling loop error: %s", e)
                logger.debug("[SlackInterface] polling loop exception details", exc_info=True)
            time.sleep(self.poll_interval)

    def poll_once(self):
        if not self.use_room_mode:
            logger.debug("[SlackInterface] use_room_mode=false; skipping poll.")
            return

        messages, last_seen = self._poll_slack_messages()
        if not messages:
            return

        inbound, max_seen = self._extract_new_inbound_messages(messages, last_seen)
        if not inbound:
            # Point 1: if no genuinely new inbound chat, do not spin up room manager.
            if max_seen > last_seen:
                self._set_channel_cursor(self.channel_id, max_seen)
            logger.debug("[SlackInterface] No new inbound user chat; skipping room manager.")
            return

        self._dispatch_room_messages(inbound)
        if max_seen > last_seen:
            self._set_channel_cursor(self.channel_id, max_seen)

    def stop(self):
        self._stop_flag.set()
        self.thread.join(timeout=5)

