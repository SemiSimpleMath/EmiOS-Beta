"""
room_chat_summary.py, per-room history compaction.

On every post-turn trigger the runner sends a structured window of eligible
room messages to the room-specific `room_summary` agent, which returns
surgical per-message decisions for that room only.

Room-scoped actions:
  hide_ids
      -> metadata["context_suppressed_by_room"][room_id] = True
  pin_ids
      -> metadata["context_pinned_by_room"][room_id] = True
  summary_pairs
      -> a new `history_summary` message is written to the global blackboard
         and persisted to unified log for that group; the grouped messages are
         marked suppressed for this room only

Design rules:
- Already suppressed messages for this room are not sent to the summary agent.
- Already processed messages for this room are not sent to the summary agent.
- The newest `_RECENT_TAIL` new messages are protected and never sent to the
  summary agent.
- Room history is loaded from the same merged history universe used by
  RoomHistoryBuilder so prompt building and summarization stay aligned.
- Summarizer scope is bounded to the current Emi day, using the shared
  day-reset cutoff from RoomHistoryBuilder.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.maintenance_manager.save_to_unified_db import save_to_unified_db
from app.assistant.room_session_manager.services.room_history_builder import RoomHistoryBuilder
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.message_visibility_policy import _NON_NORMAL_ROOM_MODES
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import utc_to_local

logger = get_logger(__name__)

_AGENT_NAME = "room_summary"

_RECENT_TAIL = 10
_MIN_NEW_MESSAGES_TO_RUN = 20
_MAX_SUMMARIZER_MESSAGES = 120

_DAYFLOW_ORCHESTRATOR_ROOM_ID = "dayflow_orchestrator"

_SUPPRESSED_BY_ROOM_KEY = "context_suppressed_by_room"
_PINNED_BY_ROOM_KEY = "context_pinned_by_room"
_PROCESSED_AT_BY_ROOM_KEY = "room_summary_processed_at_by_room"


def _persist_message_to_unified_log(message: Message, *, source: str = "room_summary") -> None:
    """
    Persist a Message to UnifiedLog2026.

    Message is the internal runtime model.
    UnifiedLog2026 is the persisted storage model.
    This function performs the explicit translation between the two.
    """
    metadata_json = message.metadata if isinstance(message.metadata, dict) else {}
    data_json = message.data if isinstance(message.data, dict) else {}

    if getattr(message, "data_type", None):
        metadata_json = dict(metadata_json)
        data_json = dict(data_json)
        metadata_json.setdefault("data_type", message.data_type)
        data_json.setdefault("data_type", message.data_type)

    if isinstance(message.sub_data_type, list):
        metadata_json = dict(metadata_json)
        metadata_json.setdefault("sub_data_type", message.sub_data_type)

    if bool(getattr(message, "test_mode", False)):
        metadata_json = dict(metadata_json)
        metadata_json.setdefault("test_mode", True)

    payload = {
        "id": message.id,
        "timestamp": message.timestamp,
        "role": message.role or "assistant",
        "message": message.content or "",
        "source": source,
        "processed": bool(getattr(message, "processed", False)),
        "request_id": message.request_id,
        "room_id": message.room_id,
        "room_surface": message.room_surface,
        "room_context_id": message.room_context_id,
        "direction": message.room_message_direction,
        "speaker_id": message.room_speaker_id,
        "speaker_name": message.room_speaker_name,
        "speaker_role": message.room_speaker_role,
        "speaker_external_id": message.room_speaker_external_id,
        "transport_message_id": message.transport_message_id,
        "transport_from": message.transport_from,
        "transport_to": message.transport_to,
        "content_type": None,
        "media_items_json": None,
        "link_items_json": None,
        "metadata_json": metadata_json or None,
        "data_json": data_json or None,
    }

    save_to_unified_db([payload], source=source)


def _ts_utc(m: Message) -> Optional[datetime]:
    ts = getattr(m, "timestamp", None)
    if not ts:
        return None
    try:
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    except Exception:
        logger.debug("room_chat_summary: could not convert timestamp to UTC", exc_info=True)
        return None


def _get_meta(m: Message) -> dict:
    meta = getattr(m, "metadata", None)
    return meta if isinstance(meta, dict) else {}


def _get_room_meta_dict(meta: dict, key: str) -> dict:
    value = meta.get(key)
    return value if isinstance(value, dict) else {}


def _is_suppressed(m: Message, room_id: str) -> bool:
    meta = _get_meta(m)
    by_room = _get_room_meta_dict(meta, _SUPPRESSED_BY_ROOM_KEY)
    return bool(by_room.get(room_id, False))


def _is_pinned(m: Message, room_id: str) -> bool:
    meta = _get_meta(m)
    by_room = _get_room_meta_dict(meta, _PINNED_BY_ROOM_KEY)
    return bool(by_room.get(room_id, False))


def _is_processed(m: Message, room_id: str) -> bool:
    meta = _get_meta(m)
    by_room = _get_room_meta_dict(meta, _PROCESSED_AT_BY_ROOM_KEY)
    value = by_room.get(room_id)
    return isinstance(value, str) and bool(value.strip())


def _set_meta(
        m: Message,
        now_utc: datetime,
        *,
        room_id: str,
        suppressed: Optional[bool] = None,
        pinned: Optional[bool] = None,
) -> None:
    meta = _get_meta(m).copy()

    suppressed_by_room = _get_room_meta_dict(meta, _SUPPRESSED_BY_ROOM_KEY).copy()
    pinned_by_room = _get_room_meta_dict(meta, _PINNED_BY_ROOM_KEY).copy()
    processed_at_by_room = _get_room_meta_dict(meta, _PROCESSED_AT_BY_ROOM_KEY).copy()

    if suppressed is not None:
        suppressed_by_room[room_id] = bool(suppressed)
    if pinned is not None:
        pinned_by_room[room_id] = bool(pinned)

    processed_at_by_room[room_id] = now_utc.isoformat()

    meta[_SUPPRESSED_BY_ROOM_KEY] = suppressed_by_room
    meta[_PINNED_BY_ROOM_KEY] = pinned_by_room
    meta[_PROCESSED_AT_BY_ROOM_KEY] = processed_at_by_room

    m.metadata = meta


def _set_meta_and_persist(
        m: Message,
        now_utc: datetime,
        *,
        room_id: str,
        source: str,
        suppressed: Optional[bool] = None,
        pinned: Optional[bool] = None,
) -> None:
    _set_meta(
        m,
        now_utc,
        room_id=room_id,
        suppressed=suppressed,
        pinned=pinned,
    )
    _persist_message_to_unified_log(m, source=source)


def _text(m: Message) -> str:
    return str(getattr(m, "content", "") or "").strip()


def _sender(m: Message) -> str:
    return str(getattr(m, "sender", "") or "").strip()


def _is_history_summary(m: Message) -> bool:
    sub_data_type = set(getattr(m, "sub_data_type", []) or [])
    return "history_summary" in sub_data_type


def _is_chat_origin_dayflow_item(m: Message) -> bool:
    """True iff this Message is a dayflow_item that was ingested from chat.

    The dayflow_orchestrator room contains many synthetic items
    (plan_task, plan_synopsis, action_log, action_dispatch, artifact). Only
    the ones created by chat_ingestion from an external room carry
    metadata.source_type == "chat" — those are the only messages the
    dayflow room-summary agent is allowed to compress.
    """
    meta = _get_meta(m)
    return str(meta.get("source_type") or "").strip().lower() == "chat"


class RoomChatSummaryRunner:
    """
    Runs the room-specific `room_summary` agent over eligible room messages and
    applies the returned per-message decisions back to the in-memory global
    blackboard and unified log.
    """

    def __init__(self, room_id: str, agent_name: str = _AGENT_NAME):
        if not isinstance(room_id, str) or not room_id.strip():
            raise ValueError("RoomChatSummaryRunner requires a non-empty room_id.")
        self.room_id = room_id.strip()
        self.agent_name = agent_name
        self._history_builder = RoomHistoryBuilder()

    def _room_messages(self) -> list[tuple[datetime, Message]]:
        """
        Return timestamped room messages from the same merged history universe
        used by RoomHistoryBuilder, bounded to the current Emi day.
        """
        messages = self._history_builder.load_room_history_messages(
            room_id=self.room_id,
            room_surface=None,
            room_context_id=None,
            shared_chat_room_ids=[],
            excluded_room_modes=_NON_NORMAL_ROOM_MODES,
            max_age_hours=24,
        )

        day_reset_cutoff_utc = self._history_builder._get_day_reset_utc()
        is_dayflow_room = self.room_id == _DAYFLOW_ORCHESTRATOR_ROOM_ID

        room_msgs: list[tuple[datetime, Message]] = []
        for m in messages:
            try:
                if _is_history_summary(m):
                    continue

                # In the dayflow_orchestrator room, the summarizer only touches
                # chat-origin dayflow_items (absorbed from master_room, slack,
                # telegram). Plan tasks, synopses, action logs, dispatches,
                # and other internal items are state-machined and must not be
                # compressed away.
                if is_dayflow_room and not _is_chat_origin_dayflow_item(m):
                    continue

                ts = _ts_utc(m)
                if ts is None:
                    continue

                if ts < day_reset_cutoff_utc and not _is_pinned(m, self.room_id):
                    continue

                room_msgs.append((ts, m))
            except Exception:
                logger.debug(
                    "room_chat_summary: error normalizing shared history message room=%s",
                    self.room_id,
                    exc_info=True,
                )
                continue

        room_msgs.sort(key=lambda x: x[0])

        if len(room_msgs) > _MAX_SUMMARIZER_MESSAGES:
            room_msgs = room_msgs[-_MAX_SUMMARIZER_MESSAGES:]

        return room_msgs

    def _build_payload_rows(self, messages: list[Message]) -> list[dict]:
        rows: list[dict] = []
        day_reset_cutoff_utc = self._history_builder._get_day_reset_utc()
        for i, m in enumerate(messages):
            msg_id = str(getattr(m, "id", "") or "").strip()
            ts = _ts_utc(m)
            time_str = utc_to_local(ts).strftime("%H:%M") if ts else ""
            is_previous_day = bool(ts is not None and ts < day_reset_cutoff_utc)

            rows.append(
                {
                    "seq": i,
                    "msg_id": msg_id,
                    "time": time_str,
                    "sender": _sender(m),
                    "content": _text(m),
                    "already_pinned": _is_pinned(m, self.room_id),
                    "already_suppressed": _is_suppressed(m, self.room_id),
                    "is_previous_day": is_previous_day,
                    "sub_data_type": list(getattr(m, "sub_data_type", []) or []),
                    "is_chat": bool(getattr(m, "is_chat", False)),
                }
            )
        return rows

    def run(self) -> bool:
        """
        Run one compaction pass for the room.

        Policy:
        - Count all new messages in-scope for this room
          (unprocessed and not already suppressed for this room).
        - Do nothing unless there are at least `_MIN_NEW_MESSAGES_TO_RUN`
          new messages.
        - Never send the most recent `_RECENT_TAIL` new messages to the
          summarizer.
        - Only older new messages are eligible for hide/pin/summary decisions.
        """
        blackboard = DI.global_blackboard
        now_utc = datetime.now(timezone.utc)
        persist_source = f"room_summary::{self.room_id}"

        room_msgs = self._room_messages()
        if not room_msgs:
            logger.debug("room_chat_summary: no messages for room=%s", self.room_id)
            return False

        all_room_msgs = [m for _, m in room_msgs]

        new_msgs: list[Message] = []
        suppressed_count = 0
        processed_count = 0

        for m in all_room_msgs:
            if _is_suppressed(m, self.room_id):
                suppressed_count += 1
                continue
            if _is_processed(m, self.room_id):
                processed_count += 1
                continue
            new_msgs.append(m)

        if len(new_msgs) < _MIN_NEW_MESSAGES_TO_RUN:
            logger.debug(
                "room_chat_summary: new=%d below min=%d for room=%s total=%d suppressed=%d processed=%d",
                len(new_msgs),
                _MIN_NEW_MESSAGES_TO_RUN,
                self.room_id,
                len(all_room_msgs),
                suppressed_count,
                processed_count,
            )
            return False

        if len(new_msgs) > _RECENT_TAIL:
            protected_msgs = new_msgs[-_RECENT_TAIL:]
            eligible_msgs = new_msgs[:-_RECENT_TAIL]
        else:
            protected_msgs = new_msgs
            eligible_msgs = []

        protected_ids = {
            str(getattr(m, "id", "") or "").strip()
            for m in protected_msgs
            if str(getattr(m, "id", "") or "").strip()
        }

        if not eligible_msgs:
            logger.debug(
                "room_chat_summary: all %d new messages are inside protected tail=%d for room=%s",
                len(new_msgs),
                _RECENT_TAIL,
                self.room_id,
            )
            return False

        logger.info(
            "┌─ room_chat_summary [%s] ─────────────────────────────",
            self.room_id,
        )
        logger.info(
            "│  total=%d  new=%d  eligible=%d  protected=%d  suppressed=%d  processed=%d",
            len(all_room_msgs),
            len(new_msgs),
            len(eligible_msgs),
            len(protected_msgs),
            suppressed_count,
            processed_count,
        )

        rows = self._build_payload_rows(eligible_msgs)
        payload = json.dumps(
            {
                "room_id": self.room_id,
                "message_count": len(rows),
                "messages": rows,
            },
            ensure_ascii=False,
        )

        agent = DI.agent_factory.create_agent(self.agent_name)
        result = agent.action_handler(Message(agent_input=payload))

        data = result.data or {}
        summary_pairs = data.get("summary_pairs") or []
        hide_ids = [str(x).strip() for x in (data.get("hide_ids") or []) if str(x).strip()]
        pin_ids = [str(x).strip() for x in (data.get("pin_ids") or []) if str(x).strip()]

        logger.info(
            "│  agent returned: hide=%d  pin=%d  summary_pairs=%d",
            len(hide_ids),
            len(pin_ids),
            len(summary_pairs),
        )

        eligible_lookup: dict[str, Message] = {}
        for m in eligible_msgs:
            mid = str(getattr(m, "id", "") or "").strip()
            if mid:
                eligible_lookup[mid] = m

        applied = 0

        if not hide_ids and not pin_ids and not summary_pairs:
            for m in eligible_msgs:
                _set_meta_and_persist(
                    m,
                    now_utc,
                    room_id=self.room_id,
                    source=persist_source,
                )
            logger.info("│  no actions taken, eligible window marked processed")
            logger.info("└─ room_chat_summary done ─────────────────────────────────")
            return False

        for mid in hide_ids:
            m = eligible_lookup.get(mid)
            if m is None:
                logger.debug(
                    "room_chat_summary: hide_id=%r not found among eligible messages for room=%s",
                    mid,
                    self.room_id,
                )
                continue

            if mid in protected_ids:
                logger.warning("room_chat_summary: refusing to hide protected message id=%r", mid)
                continue

            _set_meta_and_persist(
                m,
                now_utc,
                room_id=self.room_id,
                source=persist_source,
                suppressed=True,
                pinned=False,
            )
            preview = _text(m)[:60].replace("\n", " ")
            logger.info("│  HIDE  [%s] %r", mid[:8], preview)
            applied += 1

        for mid in pin_ids:
            m = eligible_lookup.get(mid)
            if m is None:
                logger.debug(
                    "room_chat_summary: pin_id=%r not found among eligible messages for room=%s",
                    mid,
                    self.room_id,
                )
                continue

            if mid in protected_ids:
                logger.warning("room_chat_summary: refusing to pin protected message id=%r", mid)
                continue

            _set_meta_and_persist(
                m,
                now_utc,
                room_id=self.room_id,
                source=persist_source,
                pinned=True,
            )
            preview = _text(m)[:60].replace("\n", " ")
            logger.info("│  PIN   [%s] %r", mid[:8], preview)
            applied += 1

        for pair in summary_pairs:
            if not isinstance(pair, dict):
                continue

            summary_text = str(pair.get("summary") or "").strip()
            raw_group_ids = pair.get("group_msg_ids") or []

            if not summary_text or not isinstance(raw_group_ids, list):
                continue

            deduped_group_ids: list[str] = []
            for gid in raw_group_ids:
                gid = str(gid or "").strip()
                if gid and gid not in deduped_group_ids:
                    deduped_group_ids.append(gid)

            valid_group_ids = [gid for gid in deduped_group_ids if gid in eligible_lookup]
            if not valid_group_ids:
                logger.debug(
                    "room_chat_summary: summary pair had no valid eligible group ids for room=%s",
                    self.room_id,
                )
                continue

            group_msgs = [eligible_lookup[gid] for gid in valid_group_ids]
            group_msgs.sort(key=lambda m: _ts_utc(m) or datetime.max.replace(tzinfo=timezone.utc))

            ordered_group_ids = [
                str(getattr(m, "id", "") or "").strip()
                for m in group_msgs
                if str(getattr(m, "id", "") or "").strip()
            ]

            anchor = group_msgs[0]
            anchor_id = str(getattr(anchor, "id", "") or "").strip()
            anchor_ts = _ts_utc(anchor)

            if anchor_id in protected_ids:
                logger.warning(
                    "room_chat_summary: refusing to summarize group with protected anchor id=%r",
                    anchor_id,
                )
                continue

            # Invariant: a summary must be strictly shorter than the combined
            # text of the messages it replaces. Otherwise compaction is a
            # net loss and we refuse to persist it — the originals stay
            # un-suppressed and the summary message is not written.
            originals_chars = sum(len(_text(m)) for m in group_msgs)
            summary_chars = len(summary_text)
            if summary_chars >= originals_chars:
                logger.warning(
                    "room_chat_summary: rejecting summary pair for room=%s — "
                    "summary %d chars is not shorter than originals %d chars (group=%d msgs)",
                    self.room_id, summary_chars, originals_chars, len(group_msgs),
                )
                continue

            summary_msg = Message(
                data_type="agent_msg",
                sub_data_type=["history_summary"],
                sender=self.agent_name,
                role="assistant",
                receiver=None,
                content=summary_text,
                is_chat=True,
                room_id=self.room_id,
                room_surface=getattr(anchor, "room_surface", None),
                room_context_id=getattr(anchor, "room_context_id", None),
                timestamp=anchor_ts or now_utc,
                metadata={
                    "summary_kind": "room_chat_group",
                    "room_id": self.room_id,
                    "summarized_through_utc": anchor_ts.isoformat() if anchor_ts else None,
                    "summary_created_at": now_utc.isoformat(),
                    "anchor_msg_id": anchor_id,
                    "group_msg_ids": ordered_group_ids,
                },
            )

            blackboard.add_msg(summary_msg)
            _persist_message_to_unified_log(summary_msg, source=persist_source)

            logger.info(
                "│  GROUP [%d msgs] → %r",
                len(ordered_group_ids),
                summary_text[:80],
            )

            for gid in ordered_group_ids:
                gm = eligible_lookup.get(gid)
                if gm is None:
                    continue
                if gid in protected_ids:
                    logger.warning(
                        "room_chat_summary: refusing to suppress protected group member id=%r",
                        gid,
                    )
                    continue
                _set_meta_and_persist(
                    gm,
                    now_utc,
                    room_id=self.room_id,
                    source=persist_source,
                    suppressed=True,
                )

            applied += 1

        for m in eligible_msgs:
            if not _is_processed(m, self.room_id):
                _set_meta_and_persist(
                    m,
                    now_utc,
                    room_id=self.room_id,
                    source=persist_source,
                )

        logger.info(
            "│  applied=%d  eligible_marked=%d",
            applied,
            len(eligible_msgs),
        )
        logger.info("└─ room_chat_summary done ─────────────────────────────────")
        return applied > 0