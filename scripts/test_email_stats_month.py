"""
Integration test: run get_email_stats for a month-wide window and see what
comes back. Verifies that the tool can pull a month without tripping bounds
or timing out with the current (post-fix) batch + retry configuration.

Usage:
    .venv/Scripts/python.exe scripts/test_email_stats_month.py
    .venv/Scripts/python.exe scripts/test_email_stats_month.py --year 2026 --month 3
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime

import app.assistant.tests.test_setup  # noqa: F401 — bootstrap DI

from app.assistant.lib.tools.get_email_stats.get_email_stats import GetEmailStatsTool
from app.assistant.utils.pydantic_classes import ToolMessage


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    """Gmail query bounds for a single calendar month (start inclusive, end exclusive)."""
    start = f"{year:04d}/{month:02d}/01"
    if month == 12:
        end = f"{year + 1:04d}/01/01"
    else:
        end = f"{year:04d}/{month + 1:02d}/01"
    return start, end


def main():
    ap = argparse.ArgumentParser()
    now = datetime.now()
    # Default: last fully-elapsed month (safer than current — no partial).
    default_year = now.year if now.month > 1 else now.year - 1
    default_month = now.month - 1 if now.month > 1 else 12
    ap.add_argument("--year", type=int, default=default_year)
    ap.add_argument("--month", type=int, default=default_month)
    ap.add_argument("--max-results", type=int, default=50000, help="upper bound on IDs counted")
    ap.add_argument("--sample", type=int, default=500, help="metadata_sample_size")
    ap.add_argument("--account", default=None, help="optional account_id override")
    args = ap.parse_args()

    start, end = _month_bounds(args.year, args.month)
    query = f"after:{start} before:{end}"
    print(f"Query: {query!r}")
    print(f"max_results={args.max_results}  metadata_sample_size={args.sample}")

    tool = GetEmailStatsTool()
    arguments = {
        "query": query,
        "max_results": args.max_results,
        "group_by_sender": True,
        "group_by_day": False,
        "top_senders_limit": 20,
    }
    if args.account:
        arguments["account_id"] = args.account

    msg = ToolMessage(tool_name="get_email_stats", tool_data={"tool_name": "get_email_stats", "arguments": arguments})

    t0 = time.perf_counter()
    result = tool.execute(msg)
    elapsed = time.perf_counter() - t0

    print("-" * 60)
    print(f"result_type: {result.result_type}")
    print(f"wall time:   {elapsed:.2f}s")
    if result.result_type == "error":
        print(f"content:\n{result.content}")
        err = result.data if isinstance(result.data, dict) else {}
        print(f"error detail: {err}")
        raise SystemExit(1)

    data = result.data if isinstance(result.data, dict) else {}
    print(f"total:             {data.get('total')}")
    print(f"sampled:           {data.get('sampled')}")
    print(f"is_sampled:        {data.get('is_sampled')}")
    print(f"unique_threads:    {data.get('unique_threads')}")
    print(f"unique_senders:    {data.get('unique_senders_in_sample')}")
    print(f"first_received:    {data.get('first_received')}")
    print(f"last_received:     {data.get('last_received')}")
    print(f"date_span_days:    {data.get('date_span_days')}")
    print(f"avg_per_day:       {data.get('avg_per_day')}")

    senders = data.get("senders") or []
    if senders:
        print("top senders (sampled counts shown; count_estimated = scaled to total):")
        for s in senders[:10]:
            print(f"  {s.get('email','?'):<48} sampled={s.get('count_sampled'):>4}  estimated={s.get('count_estimated'):>5}  ({s.get('pct'):>4}%)")

    if elapsed > 60:
        print(f"\n⚠️  Wall time > 60s — worth checking whether the planner's own timeout is higher.")


if __name__ == "__main__":
    main()
