from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, List, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.routine_manager.utils import read_json_file, write_json_file, write_latest_pointer_json
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import utc_to_local, local_to_utc, get_local_time_str, get_local_timezone

from app.assistant.pipelines.context import PipelineContext


class ArchiveDailyTimelineStep:
    name = "archive_daily_timeline"

    def inputs(self, ctx: PipelineContext) -> List[str]:
        return [
            str(ctx.snapshots_dir / "resource_daily_context_generator_output.json"),
            "unified_log(user messages in window)",
            "ticket_manager.get_tickets(since_utc, until_utc)",
            "agent: daily_context_normalizer",
        ]

    def outputs(self, ctx: PipelineContext) -> List[Path]:
        return [ctx.day_dir / "timeline_merged.json"]

    def run(self, ctx: PipelineContext) -> None:
        from app.assistant.ticket_manager import get_ticket_manager
        from app.models.base import get_session
        from app.assistant.database.db_handler import UnifiedLog2026
        from sqlalchemy import and_

        # 1) Load daily context snapshot and normalize via agent
        src_path = ctx.snapshots_dir / "resource_daily_context_generator_output.json"
        data = read_json_file(src_path)
        if not isinstance(data, dict):
            raise RuntimeError(f"Daily context snapshot missing or invalid: {src_path}")

        daily_context_text = json.dumps(data, indent=2, ensure_ascii=True)
        agent = DI.agent_factory.create_agent("daily_context_normalizer")
        if agent is None:
            raise RuntimeError("daily_context_normalizer agent unavailable")
        msg = Message(
            agent_input={
                "date_time": get_local_time_str(),
                "daily_context": daily_context_text,
                "task": "Normalize daily context into timeline",
            }
        )
        result = agent.action_handler(msg)
        normalized: dict[str, Any] = {}
        if hasattr(result, "data") and isinstance(result.data, dict):
            normalized = result.data
        elif isinstance(result, dict):
            normalized = result

        timeline = normalized.get("timeline") if isinstance(normalized.get("timeline"), list) else []

        merged: list[dict[str, Any]] = []

        def _time_to_utc(date_val: str, time_val: str) -> Optional[str]:
            if not time_val:
                return None
            text = str(time_val).strip()
            if text.lower() in ("[currently]", "currently"):
                return None
            if "-" in text and ":" in text:
                try:
                    return local_to_utc(text).isoformat()
                except Exception:
                    return None
            m = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*$", text, re.IGNORECASE)
            if m:
                hour = int(m.group(1))
                minute = int(m.group(2) or 0)
                mer = m.group(3).lower()
                if mer == "pm" and hour != 12:
                    hour += 12
                if mer == "am" and hour == 12:
                    hour = 0
                text = f"{hour:02d}:{minute:02d}"
            try:
                return local_to_utc(f"{date_val} {text}").isoformat()
            except Exception:
                return None

        for item in timeline:
            if not isinstance(item, dict):
                continue
            start_local_str = str(item.get("start_time_local") or "").strip()
            end_local_str = str(item.get("end_time_local") or "").strip()
            start_utc = _time_to_utc(ctx.date_str, start_local_str)
            end_utc = _time_to_utc(ctx.date_str, end_local_str) if end_local_str else None
            merged.append(
                {
                    "type": "context",
                    "start_time_local": start_local_str or None,
                    "end_time_local": end_local_str or None,
                    "start_time_utc": start_utc,
                    "end_time_utc": end_utc,
                    "label": item.get("label"),
                    "evidence": item.get("evidence"),
                    "ongoing": bool(item.get("ongoing", False)),
                }
            )

        # 2) Tickets
        ticket_manager = get_ticket_manager()
        tickets = ticket_manager.get_tickets(
            since_utc=ctx.since_utc,
            until_utc=ctx.until_utc,
            limit=10000,
            order_by="created_at",
            order_desc=False,
        )
        for t in tickets or []:
            ts = t.responded_at or t.created_at
            ts_utc = ts.isoformat() if ts else None
            ts_local = utc_to_local(ts).strftime("%Y-%m-%d %H:%M") if ts else None
            merged.append(
                {
                    "type": "ticket",
                    "start_time_local": ts_local,
                    "start_time_utc": ts_utc,
                    "ticket_id": getattr(t, "ticket_id", None),
                    "ticket_type": getattr(t, "ticket_type", None),
                    "suggestion_type": getattr(t, "suggestion_type", None),
                    "state": getattr(t, "state", None),
                    "user_action": getattr(t, "user_action", None),
                    "user_text": getattr(t, "user_text", None),
                    "title": getattr(t, "title", None),
                    "message": getattr(t, "message", None),
                }
            )

        # 3) User chat from unified_log_2026 — restricted to master_room.
        # Without this filter, role='user' rows from Slack/doc_editor/task_spec/
        # etc. would get pulled in and the downstream insights LLM treats every
        # non-assistant turn as the primary user, fabricating claims about
        # events that happened in other rooms (e.g. a colonoscopy-prep
        # insight about the primary user sourced from another Slack
        # participant's messages).
        session = get_session()
        try:
            rows = (
                session.query(UnifiedLog2026)
                .filter(
                    and_(
                        UnifiedLog2026.role == "user",
                        UnifiedLog2026.room_id == "master_room",
                        UnifiedLog2026.timestamp >= ctx.since_utc,
                        UnifiedLog2026.timestamp < ctx.until_utc,
                    )
                )
                .order_by(UnifiedLog2026.timestamp.asc())
                .limit(10000)
                .all()
            )
        finally:
            session.close()

        for row in rows or []:
            ts = row.timestamp
            ts_utc = ts.isoformat() if ts else None
            ts_local = utc_to_local(ts).strftime("%Y-%m-%d %H:%M") if ts else None
            merged.append(
                {
                    "type": "chat",
                    "start_time_local": ts_local,
                    "start_time_utc": ts_utc,
                    "message_id": getattr(row, "id", None),
                    "message": getattr(row, "message", None),
                    "source": getattr(row, "source", None),
                }
            )

        merged_sorted = sorted(merged, key=lambda item: ((item.get("start_time_utc") or ""), item.get("type", "")))

        out_path = ctx.day_dir / "timeline_merged.json"
        payload = {
            "date": ctx.date_str,
            "timezone": str(get_local_timezone()),
            "range": {
                "start_local": ctx.start_local.isoformat(),
                "end_local": ctx.end_local.isoformat(),
                "start_utc": ctx.since_utc.isoformat(),
                "end_utc": ctx.until_utc.isoformat(),
            },
            "counts": {
                "context_items": len(timeline),
                "tickets": len(tickets or []),
                "chat": len(rows or []),
                "merged": len(merged_sorted),
            },
            "timeline": merged_sorted,
            "notes": normalized.get("notes") if isinstance(normalized, dict) else "",
        }
        write_json_file(out_path, payload)
        write_latest_pointer_json(
            resource_filename="resource_daily_timeline_latest.json",
            date_str=ctx.date_str,
            archive_path=out_path,
            extra={"counts": payload.get("counts")},
        )
