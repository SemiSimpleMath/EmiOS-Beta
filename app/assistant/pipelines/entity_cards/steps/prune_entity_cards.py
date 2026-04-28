from __future__ import annotations

from pathlib import Path
from typing import List

from app.assistant.pipelines.context import PipelineContext
from app.assistant.routine_manager.utils import write_json_file, write_latest_pointer_json
from app.assistant.utils.time_utils import get_local_timezone


class PruneEntityCardsDryRunStep:
    """
    Conservative pruning report (dry run).

    This step DOES NOT deactivate cards. It generates an audit report of what would be pruned.
    Use the standalone script `app/assistant/kg_entity_card_pipeline/prune_entity_cards.py --apply` to apply.
    """

    name = "prune_entity_cards_dry_run"

    def inputs(self, ctx: PipelineContext) -> List[str]:
        return ["db: entity_cards", "kg: edges(degree)"]

    def outputs(self, ctx: PipelineContext) -> List[Path]:
        return [ctx.day_dir / "entity_cards_prune_dry_run.json"]

    def run(self, ctx: PipelineContext) -> None:
        from app.assistant.pipelines.entity_cards.kg_entity_card_pipeline.prune_entity_cards import prune_entity_cards

        report = prune_entity_cards(
            apply=False,
            min_age_days=7,
            max_degree_keep=20,
            max_degree_prune=2,
        )

        out_path = ctx.day_dir / "entity_cards_prune_dry_run.json"
        write_json_file(
            out_path,
            {
                "schema_version": 1,
                "pipeline_id": ctx.pipeline_id,
                "run_id": ctx.run_id,
                "date": ctx.date_str,
                "timezone": str(get_local_timezone()),
                "generated_at_utc": ctx.now_utc_iso(),
                "report": report,
            },
        )

        would = [
            d for d in (report.get("decisions") or []) if d.get("action") == "would_deactivate"
        ]
        write_latest_pointer_json(
            resource_filename="resource_entity_cards_prune_dry_run_latest.json",
            date_str=ctx.date_str,
            archive_path=out_path,
            extra={"would_deactivate": len(would)},
        )

