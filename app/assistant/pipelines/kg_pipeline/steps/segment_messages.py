"""
Stage 2: Segment resolved messages into coherent conversation units.

Calls knowledge_graph_add::conversation_boundary agent on a day-bounded span
of resolved messages. Agent returns per-message bounds; we collapse those
into window groupings and write one kg_window row per distinct group plus
N kg_window_message membership rows (keyed by unified_log_id).

Gating rule (prevents fragmenting live conversations):
  Don't process a day's tail until either:
    (a) at least MIN_MESSAGES contiguous resolved-but-unsegmented messages
        have accumulated, OR
    (b) the resolver is caught up — no unresolved chat-eligible messages
        exist for this day
"""
from __future__ import annotations

import uuid
from collections import OrderedDict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import or_

from app.assistant.database.db_handler import UnifiedLog2026
from app.assistant.database.kg_pipeline_models import (
    KGResolvedMessage,
    KGWindow,
    KGWindowMessage,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import get_local_timezone
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)


SEGMENTER_AGENT_NAME = "knowledge_graph_add::conversation_boundary"
SEGMENTER_VERSION = "kg_pipeline_v1"
MIN_MESSAGES_TO_SEGMENT = 6   # gate: at least this many resolved-unsegmented before segmenting

# Match the resolver's chat-eligibility filter so the "is this day fully
# resolved?" check uses the same denominator. Keep this set in sync with
# resolve_messages.CHAT_SOURCES (kg_maintenance_resolution removed
# 2026-06-10 — no producer ever wrote that source; see resolve_messages).
CHAT_SOURCES = {
    "chat", "room_slack", "room_sms", "room_ui",
}
CHAT_ROLES = {"user", "assistant"}
ALLOWED_ROOMS = {"master_room"}


def _eligible_filter(query):
    return query.filter(
        UnifiedLog2026.source.in_(CHAT_SOURCES),
        UnifiedLog2026.role.in_(CHAT_ROLES),
        UnifiedLog2026.message.isnot(None),
        UnifiedLog2026.message != "",
        or_(
            UnifiedLog2026.room_id.is_(None),
            UnifiedLog2026.room_id == "",
            UnifiedLog2026.room_id.in_(ALLOWED_ROOMS),
        ),
    )


class SegmentMessagesStep:
    name = "segment_messages"

    @staticmethod
    def _local_date(ts: datetime) -> date:
        tz = get_local_timezone()
        if ts.tzinfo is None:
            from datetime import timezone as _tz
            ts = ts.replace(tzinfo=_tz.utc)
        return ts.astimezone(tz).date()

    def _find_next_day_ready_to_segment(self) -> Optional[date]:
        """Find the earliest day with at least one resolved-unsegmented message."""
        with get_db_manager().read_session() as session:
            segmented_ids = session.query(KGWindowMessage.unified_log_id).subquery().select()
            row = (
                session.query(KGResolvedMessage.unified_timestamp)
                .filter(~KGResolvedMessage.unified_log_id.in_(segmented_ids))
                .order_by(KGResolvedMessage.unified_timestamp.asc())
                .first()
            )
            if row is None:
                return None
            return self._local_date(row[0])

    def pending_count(self) -> int:
        from sqlalchemy import func as sqlfunc
        with get_db_manager().read_session() as session:
            segmented_ids = session.query(KGWindowMessage.unified_log_id).subquery().select()
            return int(
                session.query(sqlfunc.count(KGResolvedMessage.id))
                .filter(~KGResolvedMessage.unified_log_id.in_(segmented_ids))
                .scalar() or 0
            )

    def _load_day_to_segment(self, local_day: date) -> Dict:
        """Load resolved-unsegmented messages on local_day + count of unresolved
        chat-eligible messages on that day.

        Returns {messages: [...], unresolved_count: int}.
        """
        tz = get_local_timezone()
        start_local = datetime.combine(local_day, datetime.min.time(), tzinfo=tz)
        end_local = start_local + timedelta(days=1)
        from datetime import timezone as _tz
        start_utc = start_local.astimezone(_tz.utc)
        end_utc = end_local.astimezone(_tz.utc)

        with get_db_manager().read_session() as session:
            segmented_ids = session.query(KGWindowMessage.unified_log_id).subquery().select()
            # Resolved-unsegmented rows on this day, joined to unified_log for role/speaker.
            rows = (
                session.query(KGResolvedMessage, UnifiedLog2026)
                .join(UnifiedLog2026, UnifiedLog2026.id == KGResolvedMessage.unified_log_id)
                .filter(KGResolvedMessage.unified_timestamp >= start_utc)
                .filter(KGResolvedMessage.unified_timestamp < end_utc)
                .filter(~KGResolvedMessage.unified_log_id.in_(segmented_ids))
                .order_by(KGResolvedMessage.unified_timestamp.asc(), KGResolvedMessage.id.asc())
                .all()
            )
            messages = [
                {
                    "id": ulog.id,                       # canonical id = unified_log_id
                    "role": ulog.role or "user",
                    "speaker_name": ulog.speaker_name,
                    "timestamp": ulog.timestamp,
                    "raw_text": ulog.message,
                    "resolved_text": rm.resolved_text or ulog.message,
                }
                for rm, ulog in rows
            ]

            # Count chat-eligible messages on this day NOT yet resolved.
            resolved_subq = session.query(KGResolvedMessage.unified_log_id).subquery().select()
            unresolved_count = _eligible_filter(
                session.query(UnifiedLog2026.id)
                .filter(UnifiedLog2026.timestamp >= start_utc)
                .filter(UnifiedLog2026.timestamp < end_utc)
                .filter(~UnifiedLog2026.id.in_(resolved_subq))
            ).count()

            return {"messages": messages, "unresolved_count": unresolved_count}

    def _call_segmenter(self, messages: List[Dict]) -> Dict:
        from app.assistant.ServiceLocator.service_locator import DI

        agent = DI.agent_factory.create_agent(SEGMENTER_AGENT_NAME)
        if agent is None:
            raise RuntimeError(f"Failed to create agent: {SEGMENTER_AGENT_NAME}")
        agent_input = {
            "messages": [
                {
                    "id": m["id"],
                    "role": m["role"],
                    "message": m["resolved_text"],
                    "timestamp": str(m["timestamp"]),
                }
                for m in messages
            ],
            "analysis_window_size": len(messages),
        }
        result = agent.action_handler(Message(agent_input=agent_input))
        return result.data if result and hasattr(result, "data") else {}

    def _collapse_to_windows(self, messages: List[Dict], data: Dict) -> List[Dict]:
        """Convert per-message bounds output into a list of windows with members."""
        bounds_list = data.get("message_bounds") or []
        if not bounds_list:
            return []

        groups: OrderedDict = OrderedDict()
        for entry in bounds_list:
            entry_d = entry if isinstance(entry, dict) else (entry.model_dump() if hasattr(entry, "model_dump") else dict(entry))
            mid = entry_d.get("message_id")
            b = entry_d.get("bounds") or {}
            b = b if isinstance(b, dict) else (b.model_dump() if hasattr(b, "model_dump") else dict(b))
            key = (b.get("start_message_id"), b.get("end_message_id"), bool(b.get("should_process", True)))
            groups.setdefault(key, {"member_ids": [], "reasoning": b.get("reasoning", "")})
            groups[key]["member_ids"].append(mid)

        msg_index = {m["id"]: m for m in messages}
        ordered = []
        for (start_id, end_id, should_process), info in groups.items():
            present_members = [mid for mid in info["member_ids"] if mid in msg_index]
            if not present_members:
                continue
            present_members.sort(key=lambda mid: msg_index[mid]["timestamp"])
            ordered.append({
                "start_id": start_id,
                "end_id": end_id,
                "should_process": should_process,
                "reasoning": info["reasoning"],
                "member_ids": present_members,
            })
        ordered.sort(key=lambda g: msg_index[g["member_ids"][0]]["timestamp"])
        return ordered

    def _persist_windows(self, messages: List[Dict], windows: List[Dict], analysis_summary: str) -> int:
        if not windows:
            return 0
        msg_index = {m["id"]: m for m in messages}
        with get_db_manager().transaction(op="segment.persist_windows") as session:
            written = 0
            for win in windows:
                first_id = win["member_ids"][0]
                last_id = win["member_ids"][-1]
                first_msg = msg_index[first_id]
                last_msg = msg_index[last_id]
                window_id = str(uuid.uuid4())

                session.add(KGWindow(
                    id=window_id,
                    start_unified_log_id=first_id,
                    end_unified_log_id=last_id,
                    start_timestamp=first_msg["timestamp"],
                    end_timestamp=last_msg["timestamp"],
                    message_count=len(win["member_ids"]),
                    summary=(analysis_summary or "")[:1000] if not win.get("reasoning") else (win["reasoning"] or "")[:1000],
                    standalone=bool(win["should_process"]),
                    related_previous_window_ids=None,
                    reason_for_boundary=(win["reasoning"] or "")[:1000],
                    segmenter_version=SEGMENTER_VERSION,
                ))
                for idx, uid in enumerate(win["member_ids"]):
                    session.add(KGWindowMessage(
                        window_id=window_id,
                        unified_log_id=uid,
                        item_order=idx,
                    ))
                written += 1
            return written

    def run_one_cycle(self, ctx) -> int:
        next_day = self._find_next_day_ready_to_segment()
        if next_day is None:
            return 0

        loaded = self._load_day_to_segment(next_day)
        msgs = loaded["messages"]
        unresolved_today = loaded["unresolved_count"]

        if not msgs:
            return 0

        ready_count = len(msgs)
        if unresolved_today > 0 and ready_count < MIN_MESSAGES_TO_SEGMENT:
            logger.info(
                "[kg_pipeline.segment] gated: day=%s ready=%d (need >=%d) unresolved_today=%d",
                next_day, ready_count, MIN_MESSAGES_TO_SEGMENT, unresolved_today,
            )
            return 0

        logger.info(
            "[kg_pipeline.segment] segmenting day=%s msgs=%d unresolved_today=%d",
            next_day, ready_count, unresolved_today,
        )
        try:
            data = self._call_segmenter(msgs)
        except Exception as exc:
            logger.exception("[kg_pipeline.segment] agent failed: %s", exc)
            return 0

        windows = self._collapse_to_windows(msgs, data)
        analysis_summary = data.get("analysis_summary") if isinstance(data, dict) else ""
        written = self._persist_windows(msgs, windows, analysis_summary or "")
        logger.info("[kg_pipeline.segment] day=%s wrote %d windows from %d messages",
                    next_day, written, ready_count)
        return written
