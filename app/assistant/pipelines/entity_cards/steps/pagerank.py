from __future__ import annotations

from pathlib import Path
from typing import List

from app.assistant.pipelines.context import PipelineContext


class PageRankStep:
    """
    Compute weighted PageRank over all KG nodes and persist scores to
    Node.pagerank_score before entity card generation runs.

    Delegates entirely to step_pagerank.run() from the kg_maintenance_pipeline.
    """

    name = "pagerank"

    def inputs(self, ctx: PipelineContext) -> List[str]:
        return ["kg: nodes+edges"]

    def outputs(self, ctx: PipelineContext) -> List[Path]:
        # No file output — result is written directly to the database.
        return []

    def run(self, ctx: PipelineContext) -> None:
        from app.assistant.pipelines.kg_maintenance_pipeline.step_pagerank import run
        run(ctx)
