"""
Belief Engine — nightly inference system for user beliefs.

Top-level package (not under ``app/assistant/``) reflecting that it is a
system structurally independent of the rest of the memory stack. Tables
live in the main ``emi.db`` (see ``belief_engine/db/ensure_schema.py:20``);
the package boundary is logical, not a separate database file.

Architecture: ``docs/architecture/16_BELIEF_ENGINE.md``

Plumbing:
    - Pipeline: ``belief_engine/pipeline/pipeline.py`` (4 steps per domain)
    - Routine adapter: ``belief_engine/pipeline/routine_adapter.py``
    - Schema migration: ``python -m belief_engine.db.ensure_schema``
    - Export to ``resources/kg_derived/resource_user_beliefs.json``
"""
