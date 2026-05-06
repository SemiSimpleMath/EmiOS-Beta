# Taxonomy editing + classification — archived 2026-05-06

**DO NOT DELETE.** This is a reference snapshot of working code, parked here in case the taxonomy track is revived. History is preserved via `git mv`, so `git log --follow` on any file inside this directory will trace back to its original location.

## Why archived

The taxonomy track was retired in practice well before this archive:

- The "enable taxonomy" toggle in `features_settings.html` and `quiet_mode_settings.html` is `disabled` (greyed out in the UI).
- Last `node_taxonomy_link` row created **2026-03-25**; nothing since.
- No production caller ever invoked `run_taxonomy_processing` (the `maintenance_manager` idle worker that drove batch classification).
- The `taxonomy_team_manager` agent stack (delegator → planner → 6 mutation tools) was a manual editing surface that hadn't been used in months.

The data is still load-bearing for KG queries that filter by `taxonomy_paths`, so the **data layer is preserved live** (see "What stays live" below). What's archived is the *code that grows or edits the taxonomy* — the agentic editing pipeline, the LLM-driven classification pipeline, and the reviewer web UI.

## What's archived (3 layers)

### Layer A — agentic editing surface
The `taxonomy_team_manager` and its agents/tools. This was the user-facing way to merge/rename/move/create categories via the assistant.

- `app/assistant/agents/taxonomy_team/` (delegator + planner + final_answer)
- `app/assistant/multi_agents/taxonomy_team_manager/`
- `app/assistant/lib/tools/taxonomy_team_manager/` (manager-as-tool wrapper)
- `app/assistant/lib/tools/taxonomy_create_category/`
- `app/assistant/lib/tools/taxonomy_merge_categories/`
- `app/assistant/lib/tools/taxonomy_move_category/`
- `app/assistant/lib/tools/taxonomy_path_finder/`
- `app/assistant/lib/tools/taxonomy_rename_category/`
- `app/assistant/lib/tools/taxonomy_update_description/`
- `app/assistant/lib/core_tools/taxonomy_tool/` (shared core for the 6 mutation tools)
- `app/assistant/tests/manager_tests/taxonomy_team/`
- `app/assistant/tests/manager_tests/manager_creation.py` (initial-release test runner whose only `__main__` example invoked `taxonomy_team_manager`; superseded by `_runner.py`)
- `app/assistant/tests/test_taxonomy_team_manager_schema.py`
- `app/assistant/tests/test_taxonomy_path_finder.py`

### Layer B — KG-add classification agents
LLM agents that classified KG nodes into the taxonomy tree as part of node ingest:

- `app/assistant/agents/knowledge_graph_add/taxonomy_branch_selector/`
- `app/assistant/agents/knowledge_graph_add/taxonomy_critic/`
- `app/assistant/agents/knowledge_graph_add/taxonomy_integrity_validator/`
- `app/assistant/agents/knowledge_graph_add/taxonomy_path_corrector/`
- `app/assistant/agents/knowledge_graph_add/taxonomy_path_generator/`

### Layer C — pipeline + reviewer UI
The classification pipeline that wired the Layer-B agents together, the maintenance/integrity batch jobs, the orchestrator, and the standalone reviewer web app:

- `app/assistant/kg_core/taxonomy/taxonomy_pipeline.py`
- `app/assistant/kg_core/taxonomy/taxonomy_maintenance_agent.py`
- `app/assistant/kg_core/taxonomy/taxonomy_integrity_pipeline.py`
- `app/assistant/kg_core/taxonomy/orchestrator.py`
- `app/assistant/kg_core/taxonomy/reviewer_web.py`
- `app/assistant/kg_core/taxonomy/static/` (reviewer UI assets)
- `app/assistant/kg_core/taxonomy/templates/` (reviewer UI templates)
- `app/routes/taxonomy_viewer.py` (Flask blueprint that mounted the reviewer)

## What stays live (still in `app/assistant/kg_core/taxonomy/`)

These are the **data-layer survivors** — KG queries can still filter by taxonomy path, taxonomy data is still readable, but nothing grows or edits it autonomously:

- `models.py` — SQLAlchemy table definitions: `Taxonomy`, `NodeTaxonomyLink`, `TaxonomySuggestion`, `TaxonomySuggestions`, `NodeTaxonomyReviewQueue`. **Required** by `kg_utils/kg_tools.py` for the `taxonomy_paths` query filter.
- `manager.py` — read accessor (`TaxonomyManager`).
- `utils.py` — `get_taxonomy_by_path`, `get_category_info` (read-only path lookups used by the live KG-query filter).
- `__init__.py`
- `EXPORT_FORMAT.md` (docs)
- `cleanup_duplicate_reviews.py`, `reset_taxonomy_reviews.py`, `export.py` — one-shot operational scripts (manual invocation only).
- `exports/` — JSON snapshots of the taxonomy tree (data backups).
- `taxonomy_ontology/` — seed ontology data.
- `logs/`

## Production unwiring done as part of this archive

Several files outside this archive directory referenced the archived modules. They were edited (not moved) so the live system boots cleanly:

- `app/assistant/maintenance_manager/maintenance_manager.py` — removed `run_taxonomy_processing()` and `_taxonomy_processing_worker()` (no callers; the import would have broken at runtime).
- `app/assistant/multi_agents/emi_team_manager/config.yaml` — dropped `taxonomy_team_manager` from `always_show`; dropped `taxonomy_path_finder` and the 5 mutation leaf tools from `hidden_tools`.
- `app/assistant/multi_agents/entertainment_manager/config.yaml` — dropped the same 6 leaf tools from `hidden_tools`.
- `app/assistant/multi_agents/kg_query_manager/config.yaml` — removed `taxonomy_path_finder` from `tools.allowed_tools`.
- `app/assistant/multi_agents/kg_team_manager/config.yaml` — removed `taxonomy_path_finder` from `tools.allowed_tools` and from `scope_contract.tools.allowed_tools`.
- `app/assistant/agents/kg_query/find_nodes/config.yaml` — `allowed_tools` was `[taxonomy_path_finder]`, now `[]`. The planner is degenerate (no tools) and effectively a no-op; can return control. Could be archived in a follow-up if `kg_query_manager` no longer routes to it.
- `app/assistant/agents/kg_team/find_nodes/config.yaml` — same as above.
- `app/create_app.py` and `app/routes/__init__.py` — dropped the `taxonomy_viewer_bp` import + registration.
- `app/assistant/lib/data_conversion_module/DataConversion.py` — removed the `taxonomy_paths` dispatch entry and the `_convert_taxonomy_paths` formatter.

## Restoration

To restore: `git mv` the directories back to their original paths (mirror this archive's tree onto the repo root) and revert the unwiring commits in `app/assistant/maintenance_manager/`, `app/assistant/multi_agents/{emi,entertainment,kg_query,kg_team}_*manager/config.yaml`, `app/assistant/agents/{kg_query,kg_team}/find_nodes/config.yaml`, `app/create_app.py`, `app/routes/__init__.py`, and `app/assistant/lib/data_conversion_module/DataConversion.py`. The data tables defined in `models.py` were never dropped, so existing taxonomy rows survive intact.
