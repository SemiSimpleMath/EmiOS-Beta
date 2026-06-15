from __future__ import annotations

import json
from pathlib import Path
from typing import List

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.subsystem_flags import is_subsystem_enabled

from app.assistant.pipelines.context import PipelineContext

logger = get_logger(__name__)


class BeliefV2ShadowIngestStep:
    """Ingest the day's enriched actionable_information into belief-engine v2, then export the
    belief resource file.

    Each item's structured extracted_claim becomes one observation in belief_v2_live.db (online
    canonical resolution + a consolidation sweep), the projection is rebuilt, then exported to the
    resource_user_beliefs.json shape the dayflow / health / insights readers consume.

    Two modes, gated by the `beliefs_v2_primary` subsystem flag:
      • PRIMARY (flag on): v2 owns the canonical resource_user_beliefs.json and failures FAIL LOUD
        (raise) so a broken belief update can't pass silently. This step runs LAST in the pipeline,
        so the raise surfaces as pipeline status=error WITHOUT skipping the archival steps.
      • SHADOW (flag off, default): writes the comparison file resource_user_beliefs_v2.json and
        swallows failures — it's a parallel path that must never break daily_insights.
    """
    name = "belief_v2_shadow_ingest"

    def inputs(self, ctx: PipelineContext) -> List[str]:
        return [str(ctx.day_dir / "resource_daily_insights.json")]

    def outputs(self, ctx: PipelineContext) -> List[Path]:
        return []   # writes belief_v2_live.db + the belief resource (outside the pipeline tree)

    def run(self, ctx: PipelineContext) -> None:
        primary = is_subsystem_enabled("beliefs_v2_primary")
        archive = ctx.day_dir / "resource_daily_insights.json"
        try:
            # 1) Ingest the day's enriched items, if any.
            if archive.exists():
                data = json.loads(archive.read_text(encoding="utf-8"))
                items = data.get("actionable_information") if isinstance(data, dict) else None
                if isinstance(items, list) and items:
                    from belief_engine_v2.ingest import ingest_actionable_information
                    summary = ingest_actionable_information(items, occurred_at=ctx.date_str)
                    logger.info("[belief_v2_ingest] %s -> appended=%s skipped=%s",
                                ctx.date_str, summary.get("appended"), summary.get("skipped"))
                else:
                    logger.info("[belief_v2_ingest] %s: no actionable_information to ingest", ctx.date_str)
            else:
                logger.warning("[belief_v2_ingest] no insights archive at %s; ingest skipped", archive)

            # 2) Export the belief resource. Always run (even with nothing new) so the canonical
            # file stays present when primary; canonical filename when primary, else the comparison.
            from belief_engine_v2.export import export_beliefs_v2
            out_name = "resource_user_beliefs.json" if primary else "resource_user_beliefs_v2.json"
            path = export_beliefs_v2(out_name=out_name)
            logger.info("[belief_v2_ingest] exported belief resource -> %s (primary=%s)", path, primary)
        except Exception as e:
            if primary:
                # v2 is the live producer — a silent failure would freeze the belief resource.
                logger.error("[belief_v2_ingest] PRIMARY ingest/export FAILED: %s", e)
                raise
            logger.error("[belief_v2_shadow_ingest] shadow ingest failed (non-fatal): %s", e)
            logger.debug("[belief_v2_shadow_ingest] ingest exception details", exc_info=True)
