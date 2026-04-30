from __future__ import annotations
from app.assistant.kg_core.user_identity import PRIMARY_USER_NODE_LABEL

from pathlib import Path
from typing import List

from app.assistant.pipelines.context import PipelineContext
from app.assistant.routine_manager.utils import write_json_file, write_latest_pointer_json
from app.assistant.utils.time_utils import get_local_timezone


class GenerateEntityCardsStep:
    """
    Create missing entity cards from the KG (safe catch-up mode by default).

    Important safety properties are enforced in the underlying generator:
    - do not overwrite existing high-degree cards (>=20 connected nodes)
    - never overwrite the primary user node once it exists
    - default: create only missing cards (no overwrite)
    """

    name = "generate_entity_cards"

    def inputs(self, ctx: PipelineContext) -> List[str]:
        return ["kg: nodes+edges", "db: entity_cards", "agent: entity_card_generator"]

    def outputs(self, ctx: PipelineContext) -> List[Path]:
        return [ctx.day_dir / "entity_cards_generation_results.json"]

    def run(self, ctx: PipelineContext) -> None:
        # Import inside run so pipeline module loads even if optional deps are missing in some environments.
        from app.assistant.pipelines.entity_cards.bulk_runner import run_bulk_entity_cards

        # Catch-up mode: scan all nodes but only create missing cards (default behavior).
        result = run_bulk_entity_cards(incremental=False)

        out_path = ctx.day_dir / "entity_cards_generation_results.json"
        write_json_file(
            out_path,
            {
                "schema_version": 1,
                "pipeline_id": ctx.pipeline_id,
                "run_id": ctx.run_id,
                "date": ctx.date_str,
                "timezone": str(get_local_timezone()),
                "generated_at_utc": ctx.now_utc_iso(),
                "result": result,
            },
        )

        write_latest_pointer_json(
            resource_filename="resource_entity_cards_generation_results_latest.json",
            date_str=ctx.date_str,
            archive_path=out_path,
            extra={
                "processed": result.get("processed"),
                "errors": result.get("errors"),
                "skipped": result.get("skipped"),
                "agent_rejected": result.get("agent_rejected"),
            },
        )

