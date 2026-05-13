# Edge canonicalization — Postgres-era design (archived 2026-05-12)

This directory contains design artifacts for an edge-canonicalization
layer that was scoped during the Postgres era and never made it to the
current SQLite stack. Preserved for reference; **do not run these
scripts against the live DB**.

## What's here

- **`create_edge_tables.py`** — DDL for `edge_canon` and `edge_alias`
  tables. Uses Postgres-only types (`UUID`, `VECTOR(384)`, `JSONB`,
  `gen_random_uuid()`) that won't run on SQLite. Would need a port.
- **`drop_edge_tables.py`** — companion teardown script. Same Postgres
  assumptions.
- **`seed_edge_types.py`** — the actual design value. ~94 canonical
  edge types across ~10 state categories (ownership, employment,
  family_relationship, social_relationship, preference, skill, property,
  communication, health, financial, …), each with `edge_type`,
  `inverse`, `domain`, `range`, `description`, `example`, `aliases`.

  Originally lived in a worktree (`.claude/worktrees/agent-a746c76d/`)
  and was copied here so the design survives worktree cleanup.

## Status (2026-05-12)

- No edge-canonicalization runs in production today
- Only the extractor system prompt does ad-hoc standardization
  (`has_state`, `participant`, `topic`, etc. — ~10 labels mentioned)
- Live KG has wide predicate sprawl as a result (e.g., Jan 2025 audit:
  127 distinct predicates from 431 edges)

## If activated later

The plan from `project_edge_name_canonicalization.md` memory is:
1. Standardized names at write time (extractor prompt — already partly done)
2. LLM-output canonicalization lookup table (proposed; uses `edge_alias`)
3. Periodic sweep to fold stragglers into canonical predicates

To activate the seed:
- Port the schema to SQLite (drop UUID/VECTOR/JSONB; use VARCHAR/TEXT/JSON)
- Pull `seed_edge_types.py` into main + rerun import path
- Add a canonicalization function at write time in `proposal_writer`
  that looks up emitted `relationship_type` in `edge_alias`
- Optional: rewrite-existing sweep in kg_maintenance pipeline
