from datetime import datetime, timezone
from typing import List, Optional, Iterable, Set
import threading
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.message_visibility_policy import (
    DEFAULT_EXCLUDED_CHAT_TAGS,
    normalize_room_scope_filters,
    should_include_chat_message,
)
from app.assistant.utils.time_utils import to_utc
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class GlobalBlackBoard():
    # Upper bound on the in-memory message log. Long-running processes can
    # otherwise grow this list linearly with inbound/outbound traffic until
    # the heap is exhausted. Non-chat system messages are retained more
    # aggressively than chat messages because they carry cross-turn state.
    _MAX_MESSAGES = 20000
    _TRIM_TARGET = 15000  # after trim, keep this many most-recent messages

    def __init__(self):

        self.messages: List[Message] = []
        self.messages_lock = threading.RLock()  # Thread safety for messages
        self.state_lock = threading.RLock()     # Thread safety for shared state

        self.task = ""

        self.system_state_summary = {
            "news": [],
            "weather": [],
            "calendar": [],
            "scheduler": [],
            "email": [],
            "todo_task": []
        }
        self.system_state_timestamps = {  # Track last update time
            "news": None,
            "weather": None,
            "calendar": None,
            "scheduler": None,
            "email": None,
            "todo_task": None
        }

        self.state_dict = {}

    def add_msg(self, msg: Message):
        """
        Add a new message to the messages list and push it onto the state stack.

        Messages produced during special room modes (e.g. doc_creation_mode) are tagged
        with the mode name in sub_data_type so that pipelines like dayflow can exclude them
        via the DEFAULT_EXCLUDED_CHAT_TAGS filter without any call-site changes.
        """
        meta = getattr(msg, "metadata", None)
        if isinstance(meta, dict):
            room_mode = str(meta.get("room_mode") or "").strip()
            if room_mode and room_mode != "normal":
                current_tags = list(getattr(msg, "sub_data_type", None) or [])
                if room_mode not in current_tags:
                    if hasattr(msg, "model_copy"):
                        msg = msg.model_copy(update={"sub_data_type": current_tags + [room_mode]})
                    else:
                        msg = msg.copy(update={"sub_data_type": current_tags + [room_mode]})
        with self.messages_lock:
            self.messages.append(msg)
            if len(self.messages) > self._MAX_MESSAGES:
                # Keep the most recent _TRIM_TARGET entries; unified_log has the
                # full durable record, so trimming in-memory is lossless for
                # agents that query history through the DB. Log once per trim so
                # an operator can spot sustained high message volume.
                dropped = len(self.messages) - self._TRIM_TARGET
                self.messages = self.messages[-self._TRIM_TARGET:]
                logger.warning(
                    "GlobalBlackBoard: trimmed %d oldest messages (kept %d most recent).",
                    dropped, self._TRIM_TARGET,
                )

    def clear_chat_messages(self):
        with self.messages_lock:
            new_messages = []
            for msg in self.messages:
                if not msg.is_chat:
                    new_messages.append(msg)
            self.messages = new_messages

    def clear_messages(self):
        """
        Clear all messages from the messages list.
        """
        with self.messages_lock:
            self.messages = []

    def get_all_messages(self):
        with self.messages_lock:
            return self.messages.copy()  # Return copy to prevent external modification

    def get_messages(
            self,
            data_types: Optional[List[str]] = None,
            senders: Optional[List[str]] = None,
            receivers: Optional[List[str]] = None,
            last_n: Optional[int] = None
    ) -> List[Message]:
        """
        Retrieve messages filtered by data_type, sender, receiver, and limit.

        Parameters:
        - data_types (List[str], optional): List of data_type strings to filter messages.
        - senders (List[str], optional): List of sender identifiers to filter messages.
        - receivers (List[str], optional): List of receiver identifiers to filter messages.
        - last_n (int, optional): If specified, return only the last n messages matching the criteria.

        Returns:
        - List[Message]: List of filtered Message objects.
        """
        with self.messages_lock:
            filtered_messages = []
            for msg in self.messages:
                if data_types and msg.data_type not in data_types:
                    continue
                if senders and msg.sender not in senders:
                    continue
                if receivers and msg.receiver not in receivers:
                    continue
                filtered_messages.append(msg)
            if last_n:
                return filtered_messages[-last_n:]
            else:
                return filtered_messages

    def get_messages_str(self, N: int = -1) -> str:
        """
        Concatenate the content of the last N messages into a single string.

        Parameters:
        - N (int): Number of recent messages to include. If N is negative, include all messages.

        Returns:
        - str: Concatenated string of message contents.
        """
        with self.messages_lock:
            if N > len(self.messages):
                N = len(self.messages)
            if N > 0:
                hist_items = self.messages[-N:]
            else:
                hist_items = self.messages
            hist_str = ""
            for item in hist_items:
                hist_str += " " + item.content if item.content else ""
            return hist_str

    def get_messages_by_type(self, hist_types: List[str], last_n: Optional[int] = None) -> List[Message]:
        """
        Retrieve messages filtered by their data_type.

        Parameters:
        - hist_types (List[str]): List of data_type strings to filter messages.
        - last_n (int, optional): If specified, return only the last n messages matching the criteria.

        Returns:
        - List[Message]: List of filtered Message objects.
        """
        with self.messages_lock:
            hist_grab = [hist for hist in self.messages if hist.data_type in hist_types]
            if last_n is not None:
                return hist_grab[-last_n:]
            return hist_grab

    def get_messages_by_sub_type(self, sub_types: List[str], last_n: Optional[int] = None) -> List[Message]:
        """
        Retrieve messages filtered by their sub_data_type.

        Parameters:
        - sub_types (List[str]): List of sub_data_type strings to filter messages.
        - last_n (int, optional): If specified, return only the last n messages matching the criteria.

        Returns:
        - List[Message]: List of filtered Message objects.
        """
        with self.messages_lock:
            want = set(sub_types or [])
            hist_grab = [
                hist for hist in self.messages
                if want.intersection(set(getattr(hist, "sub_data_type", []) or []))
            ]
            if last_n is not None:
                return hist_grab[-last_n:]
            return hist_grab

    def get_recent_chat_since_utc(
        self,
        cutoff_utc: datetime,
        *,
        limit: Optional[int] = None,
        content_limit: Optional[int] = None,
        room_id: Optional[str] = None,
        room_surface: Optional[str] = None,
        room_context_id: Optional[str] = None,
        shared_room_ids: Optional[Iterable[str]] = None,
        # Defaults: "clean human chat" only (opt-in for special message classes).
        include_tags: Optional[Iterable[str]] = None,
        exclude_tags: Optional[Iterable[str]] = None,
        include_command_scopes: Optional[Iterable[str]] = None,
        include_summarized: bool = False,
    ) -> List[Message]:
        """
        Canonical chat retrieval for routing/history building.

        Defaults are intentionally conservative:
        - Includes only `is_chat=True`
        - Excludes common non-chat / meta messages by tag
        - Excludes messages marked metadata["summarized"]=True (unless include_summarized=True)
        - Excludes slash commands unless explicitly allowed via include_command_scopes

        Notes:
        - Returned list is chronological (oldest -> newest).
        - Does NOT mutate stored messages; applies content_limit via shallow copies.
        """

        cutoff = to_utc(cutoff_utc)
        if cutoff is None:
            # If caller passed a bad timestamp, return nothing instead of leaking full chat.
            return []

        excluded = set(exclude_tags) if exclude_tags is not None else set(DEFAULT_EXCLUDED_CHAT_TAGS)
        required_any = {t for t in (include_tags or []) if isinstance(t, str) and t}
        allowed_scopes = {s for s in (include_command_scopes or []) if isinstance(s, str) and s}

        with self.messages_lock:
            msgs = self.messages.copy()

        allowed_room_ids, scoped_room_surface, scoped_room_context_id = normalize_room_scope_filters(
            room_id=room_id,
            shared_room_ids=shared_room_ids,
            room_surface=room_surface,
            room_context_id=room_context_id,
        )

        selected: List[tuple[datetime, Message]] = []
        for m in msgs:
            try:
                if not should_include_chat_message(
                    msg=m,
                    cutoff_utc=cutoff,
                    allowed_room_ids=allowed_room_ids,
                    room_surface=scoped_room_surface,
                    room_context_id=scoped_room_context_id,
                    include_tags=required_any,
                    exclude_tags=excluded,
                    include_command_scopes=allowed_scopes,
                    include_summarized=include_summarized,
                ):
                    continue
                ts_utc = to_utc(getattr(m, "timestamp", None))
                if ts_utc is None:
                    continue

                # Context passed to prompts must not be content-truncated.
                # Keep `content_limit` in the signature for compatibility, but ignore it.

                selected.append((ts_utc, m))
            except Exception:
                logger.debug("global_blackboard: skipping message due to processing error", exc_info=True)
                continue

        if not selected:
            return []

        selected.sort(key=lambda x: x[0])
        out = [m for _, m in selected]

        if limit is not None and limit > 0 and len(out) > limit:
            out = out[-limit:]

        return out

    def get_task(self):
        return self.task

    def get_latest_system_state_summary(self, category: str) -> Optional[dict]:
        """
        Retrieve the latest state for a given category.
        """
        with self.state_lock:
            if category in self.system_state_summary and self.system_state_summary[category]:
                return self.system_state_summary[category][-1]
            return None

    def get_latest_system_state_summary_time(self, category: str) -> Optional[str]:
        """
        Retrieve the last update timestamp for a given category.
        """
        with self.state_lock:
            return self.system_state_timestamps.get(category)

    def add_system_state_summary(self, category: str, message: dict):
        """
        Add a new system state summary entry and update the timestamp.
        """
        with self.state_lock:
            if category not in self.system_state_summary:
                logger.warning(f"Invalid category: {category}")
                return

            self.system_state_summary[category].append(message)
            self.system_state_timestamps[category] = datetime.now(timezone.utc)
            logger.debug(f"Updated {category} state at {self.system_state_timestamps[category]}")

    def update_state_value(self, key, value):
        """Overwrites the value of the given key in state_dict."""
        with self.state_lock:
            self.state_dict[key] = value

    def append_state_value(self, key, value):
        """
        Appends a value to the list stored at the given key.
        Initializes the key as a list if not present or not already a list.
        """
        with self.state_lock:
            current = self.state_dict.get(key)
            if current is None or not isinstance(current, list):
                self.state_dict[key] = []
            if isinstance(value, list):
                self.state_dict[key].extend(value)
            else:
                self.state_dict[key].append(value)

    def get_state_value(self, key, default=None):
        """Retrieve a value from the blackboard's state_dict safely."""
        with self.state_lock:
            return self.state_dict.get(key, default)
