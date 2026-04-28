from .pipeline_registry import get_pipeline

__all__ = ["get_pipeline"]

"""
Pipelines are deterministic, ordered multi-step processes (optionally calling agents/tools)
that produce archived artifacts under `day_context/`.
"""

