from __future__ import annotations

from app.assistant.scope.loader import load_scope_for_source


def build_dj_scope_context(*, actor_id: str):
    # Permission is declared in app/assistant/pipelines/dj/scope.yaml.
    return load_scope_for_source(
        kind="pipeline",
        source_id="dj",
        actor_id=str(actor_id or "").strip() or "dj_runner",
    )
