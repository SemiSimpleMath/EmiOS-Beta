from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.email_tool.utils.email_utils import EmailUtils
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)

_EMAIL_ADDR_RE = re.compile(r"<([^>]+)>")


def _extract_address(raw: str) -> str:
    """Pull bare email address out of 'Display Name <addr@host>' or return raw."""
    m = _EMAIL_ADDR_RE.search(raw)
    return m.group(1).strip().lower() if m else raw.strip().lower()


def _to_utc(date_str: str) -> datetime | None:
    """Parse RFC 2822 date header to UTC-aware datetime, or return None."""
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception as e:
        logger.debug("_to_utc: cannot parse %r: %s", date_str, e)
        return None


def _iso(dt: datetime | None) -> str:
    return dt.isoformat() if dt else ""


class GetEmailStatsTool(BaseTool):
    """
    Aggregate statistics for email traffic matching a Gmail query.

    Uses header-only metadata fetches (no body download) so it is fast even
    over large result sets.  Returns per-sender counts, temporal distribution,
    thread count, and subject samples.
    """

    def __init__(self):
        super().__init__("get_email_stats")
        self._email_utils: EmailUtils | None = None

    def _utils(self) -> EmailUtils:
        if self._email_utils is None:
            self._email_utils = EmailUtils()
        return self._email_utils

    @staticmethod
    def _parse_max_results(raw: Any) -> int:
        if raw is None:
            return 5000
        try:
            v = int(raw)
        except Exception as e:
            logger.error("[GetEmailStatsTool] Invalid max_results %r: %s", raw, e)
            logger.debug("[GetEmailStatsTool] max_results parse exception details", exc_info=True)
            raise ValueError(f"max_results must be an integer, got {raw!r}") from e
        if v < 1:
            raise ValueError("max_results must be >= 1")
        return min(v, 50_000)

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
            args = tool_data.get("arguments") if isinstance(tool_data.get("arguments"), dict) else {}

            query = str(args.get("query") or "").strip()
            if not query:
                raise ValueError("query is required.")
            account_id = str(args.get("account_id") or "").strip() or None
            max_results = self._parse_max_results(args.get("max_results"))
            group_by_sender = bool(args.get("group_by_sender", True))
            group_by_day = bool(args.get("group_by_day", False))
            top_senders_limit = int(args.get("top_senders_limit") or 10)

            utils = self._utils()
            gmail_client = utils.get_gmail_client(account_id)
            result = gmail_client.search_emails_metadata(
                query,
                max_results=max_results,
                metadata_sample_size=500,
            )

            total = result["total"]
            total_threads = result["total_threads"]
            rows = result["rows"]
            sampled = result["sampled"]
            logger.info(
                "[GetEmailStatsTool] query=%r total=%d sampled=%d",
                query, total, sampled,
            )

            if total == 0:
                return ToolResult(
                    result_type="get_email_stats",
                    content="No emails matched the query.",
                    data={
                        "query": query,
                        "total": 0,
                        "first_received": "",
                        "last_received": "",
                        "date_span_days": 0,
                        "unique_senders": 0,
                        "unique_threads": 0,
                        "senders": [],
                        "daily_counts": [],
                        "subject_samples": [],
                    },
                )

            sender_counts: dict[str, int] = defaultdict(int)
            sender_subjects: dict[str, list[str]] = defaultdict(list)
            dates: list[datetime] = []
            day_counts: dict[str, int] = defaultdict(int)
            subject_samples: list[str] = []

            for row in rows:
                raw_from = row.get("from", "")
                addr = _extract_address(raw_from) if raw_from else "unknown"
                sender_counts[addr] += 1
                subj = row.get("subject", "").strip()
                if subj and len(sender_subjects[addr]) < 3:
                    sender_subjects[addr].append(subj)

                dt = _to_utc(row.get("date", ""))
                if dt:
                    dates.append(dt)
                    if group_by_day:
                        day_counts[dt.strftime("%Y-%m-%d")] += 1

                if subj and len(subject_samples) < 10:
                    subject_samples.append(subj)

            first_dt = min(dates) if dates else None
            last_dt = max(dates) if dates else None
            span_days = (
                round((last_dt - first_dt).total_seconds() / 86400, 1)
                if first_dt and last_dt
                else 0
            )

            scale = total / sampled if sampled > 0 else 1.0

            senders_sorted = sorted(sender_counts.items(), key=lambda x: x[1], reverse=True)
            senders_out = [
                {
                    "email": addr,
                    "count_sampled": cnt,
                    "count_estimated": round(cnt * scale),
                    "pct": round(cnt / sampled * 100, 1),
                    "subject_samples": sender_subjects.get(addr, []),
                }
                for addr, cnt in senders_sorted[:top_senders_limit]
            ]

            daily_out = (
                [{"date": d, "count": c} for d, c in sorted(day_counts.items())]
                if group_by_day
                else []
            )

            avg_per_day = round(total / span_days, 1) if span_days > 0 else None

            is_sampled = sampled < total

            result_data = {
                "query": query,
                "total": total,
                "sampled": sampled,
                "is_sampled": is_sampled,
                "first_received": _iso(first_dt),
                "last_received": _iso(last_dt),
                "date_span_days": span_days,
                "avg_per_day": avg_per_day,
                "unique_senders_in_sample": len(sender_counts),
                "unique_threads": total_threads,
                "senders": senders_out if group_by_sender else [],
                "daily_counts": daily_out,
                "subject_samples": subject_samples,
            }

            summary_parts = [f"{total} email(s) matched '{query}'."]
            if is_sampled:
                summary_parts.append(f"(Sender/date breakdowns based on a sample of {sampled}.)")
            if first_dt:
                summary_parts.append(f"First: {_iso(first_dt)}.  Last: {_iso(last_dt)}.")
            if span_days:
                summary_parts.append(f"Span: {span_days} days")
                if avg_per_day:
                    summary_parts.append(f"(avg {avg_per_day}/day).")
            if group_by_sender and senders_out:
                summary_parts.append("Senders:")
                for s in senders_out:
                    display = s.get('display_name') or s.get('email', '')
                    email = s.get('email', '')
                    count = s.get('count_estimated', s.get('count', '?'))
                    pct = s.get('pct', '?')
                    line = f"  {display}"
                    if display != email:
                        line += f" <{email}>"
                    line += f" | {count} msgs ({pct}%)"
                    summary_parts.append(line)
            summary_parts.append(f"Unique threads: {total_threads}.")

            return ToolResult(
                result_type="get_email_stats",
                content=" ".join(summary_parts),
                data=result_data,
            )

        except Exception as e:
            logger.error("[GetEmailStatsTool] failed: %s", e)
            logger.debug("[GetEmailStatsTool] exception details", exc_info=True)
            return make_tool_error(
                error_code="get_email_stats_failed",
                message=f"get_email_stats failed: {e}",
                abort_policy="abort_tool",
                retryable=False,
                details={"tool": "get_email_stats"},
            )


def get_tool_class():
    return GetEmailStatsTool
