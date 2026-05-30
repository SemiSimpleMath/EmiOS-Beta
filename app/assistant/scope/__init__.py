"""Unified scope package.

Single entry point for loading a source's scope declaration into a runtime
``ScopeContext``. See ``loader.load_scope`` and the unified-scope design
(docs/architecture/SCOPE_AUDIT.md + project_unified_scope_design memory).
"""
from app.assistant.scope.loader import load_scope

__all__ = ["load_scope"]
