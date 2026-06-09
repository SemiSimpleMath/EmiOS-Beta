from __future__ import annotations

import json
from pathlib import Path
from typing import List

from app.assistant.utils.logging_config import get_logger

from app.assistant.pipelines.context import PipelineContext

logger = get_logger(__name__)


class BeliefV2ShadowIngestStep:
    """SHADOW step (belief-engine v2 §3a seam, step 2).

    Feeds the day's enriched `actionable_information` (each item's structured extracted_claim) into
    the v2 observation log in its OWN db, parallel to the still-running old engine. Best-effort by
    design: a failure here (LLM verifier hiccup, import error, anything) is logged and swallowed — it
    must NEVER fail the production daily_insights pipeline (this is an experimental parallel path).
    """
    name = "belief_v2_shadow_ingest"

    def inputs(self, ctx: PipelineContext) -> List[str]:
        return [str(ctx.day_dir / "resource_daily_insights.json")]

    def outputs(self, ctx: PipelineContext) -> List[Path]:
        return []   # writes to belief_engine_v2/data/belief_v2_live.db (outside the pipeline tree)

    def run(self, ctx: PipelineContext) -> None:
        archive = ctx.day_dir / "resource_daily_insights.json"
        if not archive.exists():
            logger.warning("[belief_v2_shadow_ingest] no insights archive at %s; skipping", archive)
            return
        try:
            data = json.loads(archive.read_text(encoding="utf-8"))
            items = data.get("actionable_information") if isinstance(data, dict) else None
            if not isinstance(items, list) or not items:
                logger.info("[belief_v2_shadow_ingest] %s: no actionable_information; nothing to ingest",
                            ctx.date_str)
                return
            from belief_engine_v2.ingest import ingest_actionable_information
            summary = ingest_actionable_information(items, occurred_at=ctx.date_str)
            logger.info("[belief_v2_shadow_ingest] %s -> appended=%s skipped=%s",
                        ctx.date_str, summary.get("appended"), summary.get("skipped"))
        except Exception as e:
            # Shadow path: never break the production insights pipeline on a v2/LLM failure.
            logger.error("[belief_v2_shadow_ingest] ingest failed (non-fatal): %s", e)
            logger.debug("[belief_v2_shadow_ingest] ingest exception details", exc_info=True)
