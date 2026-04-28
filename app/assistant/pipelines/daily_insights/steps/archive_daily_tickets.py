from __future__ import annotations

from pathlib import Path
from typing import List

from app.assistant.routine_manager.utils import write_json_file, write_latest_pointer_json
from app.assistant.utils.time_utils import get_local_timezone

from app.assistant.pipelines.context import PipelineContext


class ArchiveDailyTicketsStep:
    name = "archive_daily_tickets"

    def inputs(self, ctx: PipelineContext) -> List[str]:
        return ["ticket_manager.get_tickets(since_utc, until_utc)"]

    def outputs(self, ctx: PipelineContext) -> List[Path]:
        return [ctx.day_dir / "tickets.json"]

    def run(self, ctx: PipelineContext) -> None:
        from app.assistant.ticket_manager import get_ticket_manager

        ticket_manager = get_ticket_manager()
        tickets = ticket_manager.get_tickets(
            since_utc=ctx.since_utc,
            until_utc=ctx.until_utc,
            limit=10000,
            order_by="created_at",
            order_desc=False,
        )

        ticket_dicts = []
        for t in tickets or []:
            try:
                ticket_dicts.append(t.to_dict())
            except Exception:
                ticket_dicts.append({"ticket_id": getattr(t, "ticket_id", None), "error": "to_dict_failed"})

        out_path = ctx.day_dir / "tickets.json"
        payload = {
            "date": ctx.date_str,
            "timezone": str(get_local_timezone()),
            "range": {
                "start_local": ctx.start_local.isoformat(),
                "end_local": ctx.end_local.isoformat(),
                "start_utc": ctx.since_utc.isoformat(),
                "end_utc": ctx.until_utc.isoformat(),
            },
            "count": len(ticket_dicts),
            "tickets": ticket_dicts,
        }
        write_json_file(out_path, payload)
        write_latest_pointer_json(
            resource_filename="resource_daily_tickets_latest.json",
            date_str=ctx.date_str,
            archive_path=out_path,
            extra={"count": len(ticket_dicts)},
        )
