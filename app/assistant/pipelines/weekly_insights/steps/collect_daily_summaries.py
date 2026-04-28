from __future__ import annotations

import json
from app.assistant.utils.logging_config import get_logger
from datetime import date, timedelta
from pathlib import Path
from typing import Any, List

from app.assistant.utils.path_utils import get_repo_root as _get_repo_root
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_DAY_CONTEXT_ROOT = _get_repo_root() / "day_context"


def _day_context_dir(date_str: str) -> Path:
    year, month = date_str[:4], date_str[5:7]
    return _DAY_CONTEXT_ROOT / year / month / date_str


def week_date_range(end_date_str: str, num_days: int = 7) -> List[str]:
    """Return list of YYYY-MM-DD strings for the `num_days` ending on end_date_str (inclusive)."""
    end = date.fromisoformat(end_date_str)
    return [(end - timedelta(days=i)).isoformat() for i in range(num_days - 1, -1, -1)]


class CollectDailySummariesStep:
    """
    Reads resource_daily_assessment_summary.json for each of the 7 days in the week
    and writes a collected JSON (list of summaries) to the week_dir.
    """

    name = "collect_daily_summaries"

    def inputs(self, ctx: Any) -> List[Path]:
        return []

    def outputs(self, ctx: Any) -> List[Path]:
        return [ctx.week_dir / "daily_summaries_collected.json"]

    def run(self, ctx: Any) -> dict:
        dates = week_date_range(ctx.end_date_str, num_days=ctx.num_days)
        summaries = []
        missing = []

        for d in dates:
            day_dir = _day_context_dir(d)
            summary_path = day_dir / "resource_daily_assessment_summary.json"
            if not summary_path.exists():
                logger.warning("[CollectDailySummaries] Missing summary for %s", d)
                missing.append(d)
                continue
            try:
                data = json.loads(summary_path.read_text(encoding="utf-8"))
                summary = data.get("summary") or data
                summaries.append({"date": d, "summary": summary})
                logger.info("[CollectDailySummaries] Loaded summary for %s", d)
            except Exception as exc:
                logger.debug("[CollectDailySummaries] Failed to read %s: %s", summary_path, exc)
                missing.append(d)

        if not summaries:
            raise ValueError(
                f"[CollectDailySummaries] No daily summaries found for week ending {ctx.end_date_str}. "
                f"Missing: {missing}"
            )

        out_path = ctx.week_dir / "daily_summaries_collected.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps({"week_end": ctx.end_date_str, "days": summaries, "missing": missing}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info(
            "[CollectDailySummaries] Collected %d/%d summaries → %s",
            len(summaries), ctx.num_days, out_path,
        )
        return {"collected": len(summaries), "missing": missing}
