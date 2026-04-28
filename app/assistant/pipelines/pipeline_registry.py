from __future__ import annotations

from typing import Dict, Optional, Protocol

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class Pipeline(Protocol):
    pipeline_id: str

    def run(
        self,
        *,
        target_date: Optional[str] = None,
        only_steps: Optional[list[str]] = None,
        run_id: Optional[str] = None,
        force: bool = False,
    ) -> Dict:
        ...


_pipelines: dict[str, Pipeline] = {}
_skipped_pipelines: set[str] = set()


def register_pipeline(pipeline: Pipeline) -> None:
    _pipelines[pipeline.pipeline_id] = pipeline


def get_pipeline(pipeline_id: str) -> Optional[Pipeline]:
    return _pipelines.get((pipeline_id or "").strip())


def _try_register(pipeline_id: str, factory) -> None:
    """Attempt to register a pipeline; skip gracefully if a dependency is missing."""
    if pipeline_id in _pipelines:
        return
    try:
        register_pipeline(factory())
    except (ImportError, ModuleNotFoundError) as e:
        if pipeline_id not in _skipped_pipelines:
            logger.info("Pipeline '%s' unavailable (missing optional dep: %s) -- skipped", pipeline_id, e)
            _skipped_pipelines.add(pipeline_id)


def _ensure_defaults_registered() -> None:
    _try_register("daily_insights", lambda: __import__(
        "app.assistant.pipelines.daily_insights.pipeline", fromlist=["DailyInsightsPipeline"]
    ).DailyInsightsPipeline())

    _try_register("dayflow", lambda: __import__(
        "app.assistant.pipelines.dayflow.pipeline", fromlist=["DayFlowPipeline"]
    ).DayFlowPipeline())

    _try_register("entity_cards", lambda: __import__(
        "app.assistant.pipelines.entity_cards.pipeline", fromlist=["EntityCardsPipeline"]
    ).EntityCardsPipeline())

    _try_register("kg_pipeline", lambda: __import__(
        "app.assistant.pipelines.kg_pipeline.pipeline", fromlist=["KGPipeline"]
    ).KGPipeline())

    _try_register("weekly_insights", lambda: __import__(
        "app.assistant.pipelines.weekly_insights.pipeline", fromlist=["WeeklyInsightsPipeline"]
    ).WeeklyInsightsPipeline())

    # Unified belief engine: one pipeline that loops over every domain
    # marked enabled in configs/belief_domains.yaml. Replaces the
    # formerly per-domain belief_engine_<domain> registrations.
    _try_register("belief_engine", lambda: __import__(
        "belief_engine.pipeline.routine_adapter", fromlist=["BeliefEngineAdapter"]
    ).BeliefEngineAdapter())

    _try_register("belief_engine_export", lambda: __import__(
        "belief_engine.pipeline.routine_adapter", fromlist=["BeliefEngineExportAdapter"]
    ).BeliefEngineExportAdapter())

    _try_register("kg_maintenance_pipeline", lambda: __import__(
        "app.assistant.pipelines.kg_maintenance_pipeline.pipeline", fromlist=["KGMaintenancePipeline"]
    ).KGMaintenancePipeline())

    _try_register("entity_card_maintenance_pipeline", lambda: __import__(
        "app.assistant.pipelines.entity_card_maintenance_pipeline.pipeline", fromlist=["EntityCardMaintenancePipeline"]
    ).EntityCardMaintenancePipeline())


def resolve_pipeline(pipeline_id: str) -> Optional[Pipeline]:
    _ensure_defaults_registered()
    return get_pipeline(pipeline_id)

