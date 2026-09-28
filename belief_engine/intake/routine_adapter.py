"""Shadow mode: the new belief intake runs nightly beside the old belief engine.

It reads the day's insights and timeline (written by the daily_insights pipeline at 00:05) and
the assistant's turns, and writes only its own `belief_intake_*` tables. Nothing the assistant reads —
user_beliefs, the export, the daily compile — is touched. The owner compares the two stores;
cutover is a separate, later change.

Runs every stored day up to yesterday (local calendar date) that the store has not finished. A day
is taken only once its calendar date has passed: the routine manager runs a missed daily slot at
startup, and on 2026-09-27 that ran the intake at 19:22 on the day still in progress, against
insights a daily_insights catch-up had written at 14:39 — the day was marked done with one atom
and the evening would never have been ingested. At the routine's own 01:00 slot "yesterday" is the
day the nightly insights run (00:05) just wrote. A day whose files are missing raises, so the
routine records the failure.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class BeliefIntakeAdapter:
    pipeline_id = "belief_intake"

    def run(
            self,
            *,
            target_date: Optional[str] = None,
            only_steps: Optional[List[str]] = None,
            run_id: Optional[str] = None,
            force: bool = False,
    ) -> Dict[str, Any]:
        _ = only_steps, force
        from datetime import timedelta
        from app.assistant.utils.time_utils import get_local_time
        from belief_engine.intake import day_items
        from belief_engine.intake.run import app_store, run_pending

        day = (target_date or (get_local_time().date() - timedelta(days=1)).isoformat()).strip()
        if day not in day_items.available_days():
            raise RuntimeError(f"belief_intake: no insights/timeline stored for {day} — did daily_insights run?")
        results = run_pending(app_store(), day, log=lambda line: logger.info("[belief_intake:%s] %s", run_id, line))
        return {"pipeline_id": self.pipeline_id, "run_id": run_id, "status": "success",
                "date": day, "days": results}
