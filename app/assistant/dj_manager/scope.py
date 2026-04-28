from __future__ import annotations

from app.assistant.pipelines.scope_policy import build_pipeline_scope_context


def build_dj_scope_context(*, actor_id: str):
    return build_pipeline_scope_context(
        pipeline_id="dj",
        actor_id=str(actor_id or "").strip() or "dj_runner",
        room_surface="pipeline",
    )
