# Belief Engine

A nightly inference system that derives a living set of **user beliefs** — concise, agent-ready statements about routines, habits, preferences, and constraints — from the last 14 days of daily insights and ticket signals. Beliefs are written to a dedicated SQLite store (`user_beliefs` + `belief_evidence` tables), then exported once per day to a single resource file (`resources/kg_derived/resource_user_beliefs.json`) that other agents read.

The belief engine lives in a top-level `belief_engine/` package (not under `app/assistant/pipelines/`), reflecting its design as a system that is structurally independent of the rest of the memory stack — see `belief_engine/__init__.py:1`. It plugs into the standard pipeline runner only via thin adapters in `belief_engine/pipeline/routine_adapter.py`.

> Cross-refs: pipeline + routine plumbing is documented in [06_PIPELINES_AND_ROUTINES.md](06_PIPELINES_AND_ROUTINES.md). The primary evidence source — daily insights — is described in [05_DAYFLOW.md](05_DAYFLOW.md).

## 1. Why beliefs are domain-scoped

Beliefs are partitioned by domain (routine, health, food, communication, sleep, work, general) and the engine runs one LLM pass per domain over a focused evidence slice. Two reasons:

- **Domain-scoped LLM context**: every step in the pipeline is told "current domain: `{{ domain }}`" and instructed to stay within it (`belief_updater/prompts/system.j2:96-99`). This keeps prompts focused, fits within model context, and lets the canonicalizer's "merge near-duplicates" pass operate on a constrained candidate pool.
- **Domain-filtered tag routing on intake**: each domain has a `tags` list and a `ticket_types` list (defined in `configs/belief_domains.yaml`); `CollectEvidenceStep` only emits evidence items whose tags / ticket types match the current domain. Daily insights are tagged at write-time; the belief engine fans out by tag.

A single `BeliefEnginePipeline` class (`belief_engine/pipeline/pipeline.py`) is parameterised by a `domain` string. The unified `BeliefEngineAdapter` (`belief_engine/pipeline/routine_adapter.py:35`) loops over every domain marked `enabled: true` in the YAML and runs the per-domain pipeline in sequence. A failure in one domain is logged but doesn't abort the rest — the adapter returns a per-domain status list plus `success` / `partial` / `error` overall.

## 2. Where tags come from (the upstream that feeds the filter)

Tags don't appear out of nowhere. Each `actionable_information` item in `resource_daily_insights.json` carries a `tags: List[str]` field that was assigned by an LLM agent — `daily_timeline_insights` — when the daily-insights pipeline ran the previous night. The agent is constrained to a fixed vocabulary passed in as `memory_available_tags`.

Since 2026-04-27 that vocabulary is the **union of every enabled domain's `tags`** in `configs/belief_domains.yaml`, computed by `belief_engine.config.list_all_tags()`. So the same YAML file controls both:

- **Production** — what the LLM is allowed to emit (`archive_daily_insights.py:35`).
- **Consumption** — which items each domain in the belief engine picks up (`collect_evidence.py`).

Adding a tag to a domain in the YAML automatically lets the daily-insights LLM emit it on its next run. There is no second list to keep in sync.

## 3. Domain configuration

Single source of truth: **`configs/belief_domains.yaml`**.

```yaml
domains:
  - id: routine
    enabled: true
    tags: [routine, schedule, timing, general]
    ticket_types: [standing_break, finger_stretch, hydration, coffee, walk, dog_walk, movement, stretch]
  - id: health
    enabled: true
    tags: [health, wellness, pain, nutrition, sleep, exercise]
    ticket_types: [standing_break, finger_stretch, hydration, sleep, exercise]
  - id: food
    enabled: true
    tags: [food, drink, nutrition]
    ticket_types: [coffee, hydration, meal, snack]
  - id: communication
    enabled: true
    tags: [communication, email, messaging, social]
    ticket_types: []
  - id: sleep
    enabled: true
    tags: [sleep, rest, bedtime, wakeup, nap, fatigue]
    ticket_types: [sleep]
  - id: work
    enabled: true
    tags: [work, productivity, focus, meeting, deep_work, deadline, project]
    ticket_types: []
  - id: general
    enabled: true
    tags: [general, lifestyle, preference, personal, miscellaneous]
    ticket_types: []
```

Adding a new domain is one YAML row plus arranging for daily insights to actually emit items with matching tags. Disabling a domain is `enabled: false` — no code change, no routines.json edit. Loader: `belief_engine/config.py` — `list_enabled_domains()`, `get_domain_config(id)`.

The `id` becomes the `domain` column on belief rows, so renaming a domain breaks existing rows — prefer adding a new id and retiring the old.

## 3. Scheduling

One scheduled routine, one disabled-by-default routine kept for manual re-export:

| Routine | Time | What it does |
| --- | --- | --- |
| `belief_engine` | 00:30 daily | Calls `BeliefEngineAdapter.run()` — loops every enabled domain in YAML order, running the four-step pipeline for each. On success, calls `export_beliefs()` inline as the final step so the exported JSON cannot diverge from the DB. |
| `belief_engine_export` | (disabled) | Pipeline registration kept; routine `enabled: false`. Use `/run-routine belief_engine_export` for a manual re-export. Content-guarded: file is only rewritten when something changed. |

> Historical: prior to 2026-04-28, the export was a separate scheduled routine at 01:00 daily. Two-routine scheduling assumed the upstream `belief_engine` finished within 30 minutes; if it ran long, the export read partial DB state. Inlining the export into the same routine guarantees they're atomic — if the domain loop fails, export does not run and the previous good JSON stays in place.

> Historical: prior to 2026-04-27, each domain had its own `belief_engine_<domain>` pipeline registration plus its own routine entry, scheduled five minutes apart (00:30, 00:35, …, 00:55). The split into seven routines was over-engineered — the pipeline class was already domain-parametric, so the seven registrations all wrapped the same class with a different argument. They have been collapsed to one routine that loops over the YAML.

## 3. Pipeline structure (per domain)

Each domain pipeline is a fixed four-step sequence defined in `belief_engine/pipeline/pipeline.py:49-54`. Steps share a `_RunContext` dataclass and run sequentially; if any step raises, the pipeline aborts (`pipeline.py:65-75`).

```
CollectEvidenceStep  ──>  UpdateBeliefsStep  ──>  ReevaluateBeliefsStep  ──>  CanonicalizeBeliefSetStep
   (no LLM)                (LLM: belief_updater)    (LLM: belief_reevaluator,    (LLM: belief_canonicalizer,
                                                     conditional)                  multi-pass)
```

### Step 1 — `CollectEvidenceStep`

`belief_engine/pipeline/steps/collect_evidence.py`. Pure file IO, no LLM.

Walks the last 14 calendar days of `day_context/<YYYY>/<MM>/<YYYY-MM-DD>/`:

- `resource_daily_insights.json` — for each `actionable_information` item whose tags intersect this domain's `_DOMAIN_TAGS`, emits an `EvidenceItem` with the user's verbatim quote when present (`collect_evidence.py:113-131`). `temporal_scope == "chronic"` items get `weight=3.0`, others `weight=1.5`.
- `timeline_merged.json` — for ticket events of types in `_DOMAIN_TICKET_TYPES`, emits items with weighting tuned for signal strength (`collect_evidence.py:135-268`):
  - All rejections kept verbatim. With a user comment: `weight=4.0` (highest); without: `weight=2.5`.
  - Acceptances *with a user comment*: `weight=2.0`. Bare acceptances are aggregated, and the types `finger_stretch / standing_break / hydration / movement / stretch / walk` are skipped entirely as bare-acceptance noise (`collect_evidence.py:238-247`).
  - Snoozed-without-completion ("Later") tickets get aggregated as a *pacing signal* when ≥3 over the window (`collect_evidence.py:257-266`) — these become "user often delays this" qualifiers.

Output: an `EvidenceBundle` attached to `ctx.evidence_bundle`, sorted most-recent-first.

### Step 2 — `UpdateBeliefsStep`

`belief_engine/pipeline/steps/update_beliefs.py`. Calls the `belief_engine::belief_updater` agent with the full evidence block plus the top-K semantically similar existing beliefs (k=8, threshold 0.50; `update_beliefs.py:28-30`).

Per-belief decisions from the agent (`agent_form.py`): `create | update | deprecate | no_change`. The store applies them via `BeliefStore.upsert_belief()` / `deprecate()`. When the LLM decreases confidence on an `update`, or explicitly returns `status=contested`, the belief is queued in `contested_keys` for Step 3 (`update_beliefs.py:206-222`). The agent must populate `evidence_refs` (1-based indices into the bundle) so the right evidence rows attach to the right belief (`update_beliefs.py:58-77`).

### Step 3 — `ReevaluateBeliefsStep` (conditional)

`belief_engine/pipeline/steps/reevaluate_beliefs.py`. Only runs if Step 2 produced contested keys. For each contested belief, fetches the **complete** evidence trail from `belief_evidence` (capped at 50 items, `reevaluate_beliefs.py:26`) and asks `belief_engine::belief_reevaluator` for an authoritative rewrite. Allowed actions: `rewrite | qualify | split | deprecate | confirm` (`belief_reevaluator/agent_form.py:39-46`). A `confirm` restores `status=active`; `split` produces two beliefs (one inheriting the original key, one with a dot-suffix).

### Step 4 — `CanonicalizeBeliefSetStep`

`belief_engine/pipeline/steps/canonicalize_belief_set.py`. After updates and re-evaluations, walks the **full** active belief set for the domain looking for near-duplicates and over-broad beliefs:

1. Loads all active beliefs in this domain.
2. Sorts them by **embedding proximity** using a greedy nearest-neighbour walk over the Chroma vectors (`canonicalize_belief_set.py:36-110`) so semantically related beliefs are adjacent.
3. Sends them in chunks of 40 (`CHUNK_SIZE`) to `belief_engine::belief_canonicalizer`. The agent emits `canonical_beliefs` with a surviving `belief_key`, a list of `deprecated_keys`, and merge reasoning. It can also *split* an over-broad belief into multiple new keys.
4. Repeats up to `MAX_PASSES=5` until a full pass produces zero merges (convergence). Logs a warning if it hits the cap without converging (`canonicalize_belief_set.py:304-308`).

Merges call `BeliefStore.merge_belief()` which deprecates redundant beliefs, removes their Chroma embeddings, **transfers their `observation_count` to the survivor** (`belief_store.py:393-411`), and writes a `canonicalization` evidence row with the merge reasoning so the audit trail survives the merge.

## 4. Belief schema

Two SQLite tables, defined in `belief_engine/db/schema.py` and mapped in `belief_engine/db/models.py`. They live in the **main** `emi.db` (see `ensure_schema.py:20`), not in a separate file — but they are *managed* as if they were a separate system (own ORM models, own migration entry point, own access layer).

### `user_beliefs`

| column | type | notes |
| --- | --- | --- |
| `id` | TEXT PK | UUID |
| `domain` | TEXT | `routine`, `health`, `food`, `sleep`, `general`, `communication`, `work` |
| `belief_key` | TEXT UNIQUE | stable dot-slug, e.g. `routine.dog_walk.morning`. Updates target this key. |
| `statement` | TEXT | prose, agent-ready (1–3 sentences, ~40–120 words; `belief_updater/prompts/system.j2:34`) |
| `confidence` | TEXT | `high` / `medium` / `low` |
| `scope` | TEXT | `chronic` (durable, weeks-months) / `temporary` (days-weeks) |
| `status` | TEXT | `active` / `contested` / `deprecated`. Only `active` rows are exported. |
| `conditions` | TEXT (JSON) | structured qualifiers; currently only the re-evaluator writes this, as `{"text": "..."}` (`reevaluate_beliefs.py:156-160`) |
| `observation_count` | INTEGER | incremented on every upsert; merges add the absorbed beliefs' counts (`belief_store.py:393`) |
| `first_observed`, `last_confirmed` | TEXT (ISO) | bookend dates |
| `created_at`, `updated_at` | TEXT (ISO) | row lifecycle |

### `belief_evidence`

One row per signal that touched a belief. `belief_id` is `ON DELETE CASCADE` against `user_beliefs(id)` (`schema.py:37`), so deleting a belief takes its evidence with it.

| column | notes |
| --- | --- |
| `id` | UUID PK |
| `belief_id` | FK → `user_beliefs.id` ON DELETE CASCADE |
| `source_type` | `daily_insights`, `ticket_rejection`, `ticket_acceptance`, `kg_edge`, `canonicalization`, `manual` |
| `source_date` | YYYY-MM-DD of the source day |
| `source_ref` | optional id back into source (e.g. `ticket_id`) |
| `signal_type` | `confirms` / `qualifies` / `contradicts` / `rejects` |
| `summary` | one-sentence digest, used in re-evaluation prompts |
| `raw_text` | original verbatim quote when available |
| `weight` | 0.0–5.0 (see weight rules in §3 step 1) |

### Embeddings — `BeliefChroma`

`belief_engine/chroma/belief_chroma.py`. A dedicated ChromaDB collection (`belief_engine_beliefs`) keyed by `belief_id`, sharing the project's embedding model and Chroma client but managed separately. Used for two things only: (a) `find_similar()` during update so the LLM sees existing beliefs that might match new evidence; (b) the proximity walk in canonicalization.

Embeddings are upserted on every `BeliefStore.upsert_belief` (`belief_store.py:299-304`) and deleted on merge (`belief_store.py:441-443`). Plain `deprecate()` does **not** delete the embedding — only `merge_belief()` does. This is a real consequence: deprecated-but-not-merged beliefs can still surface in `find_similar` searches; `BeliefStore.find_similar` filters them out by re-checking `status == "active"` after the Chroma hit (`belief_store.py:218-220`).

## 5. The export step (`export_beliefs`)

`belief_engine/export/export_beliefs.py`. Called inline from `BeliefEngineAdapter.run()` after a successful domain loop. Also adapted to the pipeline runner via `BeliefEngineExportAdapter` for manual re-export.

What it does:

1. Reads **all active** beliefs across all domains via `BeliefStore.list_all(status="active")` (or one domain if `domain=` is passed; the routine always uses all).
2. Serializes each row to a flat dict (`belief_key`, `domain`, `statement`, `confidence`, `scope`, `status`, `observation_count`, `first_observed`, `last_confirmed`, optional `conditions`).
3. Wraps in a `_metadata` envelope (`resource_id`, `schema_version=1.0`, `generated_at`, `entry_count`, plus a description that calls out the user by primary name).
4. **Content-guard**: serializes just the `beliefs` list (sorted, ignoring metadata) and compares it byte-for-byte against the same projection of the existing file. If unchanged, **the file is not rewritten** (`export_beliefs.py:73-83`). Only the timestamp would have changed otherwise — skipping the write avoids polluting downstream change detection (file-mtime watchers, dayflow stages keyed off the resource).
5. Writes atomically to `resources/kg_derived/resource_user_beliefs.json` — the JSON is written to a sibling `.tmp` and renamed, so a crash mid-write leaves the previous good file in place.

Synchronisation: the export is called inline at the end of `BeliefEngineAdapter.run()` rather than scheduled separately. This is the synchronisation contract — there is no fixed-time gap between the two for a slow upstream run to outrun.

## 6. Where beliefs surface

`resource_user_beliefs.json` is read by:

- **Dayflow routine stage** — `app/assistant/pipelines/dayflow/steps/dayflow_routine_stage.py:32`. Feeds beliefs into the agent that generates `resource_dayflow_routine.md` (a hourly-regenerated, belief-enriched daily routine doc that is then injected into all agents).
- **Health status stage** — `app/assistant/pipelines/dayflow/steps/health_status_stage.py:30`.
- **Entertainment advisor stage** — `app/assistant/pipelines/dayflow/steps/entertainment_advisor_stage.py:27`.
- **Dayflow orchestrator room** — `app/assistant/rooms/dayflow_orchestrator/access.json:8` lists `resource_user_beliefs` as an allowed resource, making it injectable into any agent acting under that room's scope.
- **Insights UI tab** — `/api/insights/beliefs` (`app/routes/insights.py:156-176`) returns the JSON; rendered in the `beliefs` panel of `app/templates/insights.html` with a domain filter and search box (`app/static/js/insights.js:252-308`).

There is no live database query path from agent prompts. All consumers go through the exported JSON. This is intentional: the resource boundary makes belief reads cheap, deterministic, and version-able.

## 7. Decay mechanism — planned, not built

A belief decay mechanism (deterministic scan + LLM review to retire stale beliefs) is **planned but not implemented**. Searching `belief_engine/` for `decay`, `stale`, or `retire` returns no hits. The intent is recorded in user memory (`feedback/project memo: "Belief decay design"` — deterministic scan + LLM review for stale beliefs), but no code, no scheduled routine, and no candidate-finder exists today.

What code-grounded hooks already exist that a decay job could lean on:

- `last_confirmed` is maintained on every upsert and `no_change` (`update_beliefs.py:170-178`); a decay scan would read this column.
- `observation_count` and `confidence` give a coarse staleness vs. strength signal.
- `BeliefStore.deprecate()` + the existing `status='deprecated'` enum value already exist; a decay job would be a fourth pipeline step or a separate routine that flips `active → deprecated` after a review.
- The export is content-guarded, so retiring beliefs would propagate cleanly (one fewer entry in the JSON, mtime updates, downstream agents pick up on next dayflow run).

> Note: until decay ships, `last_confirmed` is the only signal that anything has gone stale. There is currently no path that closes a belief without either an explicit user contradiction (caught by the updater/re-evaluator) or a manual deprecate.

## 8. Why the beliefs DB is "fragile / handle with extreme care"

The user-memory note flagging the belief DB as fragile reflects several structural realities visible in the code:

1. **Tables live in `emi.db`, not a separate file.** `ensure_schema.py:20` writes the schema directly into the main DB. A schema migration mistake (or a `DELETE FROM user_beliefs` from the wrong shell) hits the same file as the unified log, KG, and pod store.
2. **`belief_evidence.belief_id` is `ON DELETE CASCADE`** (`schema.py:37`). Removing a single belief takes its entire evidence trail with it — there is no soft-delete safety net at the FK layer; only application-level `deprecate()` (which sets `status='deprecated'`) is safe. A direct `DELETE` cascades silently.
3. **Two-store consistency.** A belief lives in both SQLite (`user_beliefs`) and Chroma (`belief_engine_beliefs` collection). The store keeps them in sync on writes (`belief_store.py:299-304`, `:441-443`), but a direct DB edit will leave Chroma orphaned. Conversely, deprecated beliefs that were never merged keep their embeddings, and `find_similar` only filters them out post-hoc.
4. **No backups beyond what the user creates manually.** A `data/beliefs_backup_20260318_224637.db` exists in the tree, suggesting hand-made snapshots, not automated rotation.
5. **No `dry-run` on the canonicalizer's merge step in production.** `CanonicalizeBeliefSetStep.run` accepts a `dry_run=False` kwarg (`canonicalize_belief_set.py:229`) but the routine adapter never passes it. A bad LLM merge cluster gets persisted; the only audit is the `merge_reasoning` written into the `canonicalization` evidence row.

Practical implication: any script that wants to prune, rebalance, or re-seed beliefs should go through `BeliefStore` (`belief_engine/store/belief_store.py`) and never touch the tables directly. Per the user's standing rule on belief-store work: ask before running heavy DB scripts while Emi is running.

## 9. Key files

| File | Purpose |
| --- | --- |
| `belief_engine/__init__.py` | Module-level docstring with quickstart |
| `belief_engine/pipeline/pipeline.py` | `BeliefEnginePipeline` — 4-step orchestrator, one domain per call |
| `belief_engine/pipeline/routine_adapter.py` | `BeliefEngineRoutineAdapter` (per-domain), `BeliefEngineExportAdapter` |
| `belief_engine/pipeline/steps/collect_evidence.py` | Domain-tag-filtered scan of last 14 days of insights + tickets |
| `belief_engine/pipeline/steps/update_beliefs.py` | LLM `belief_updater` — create/update/deprecate/no_change |
| `belief_engine/pipeline/steps/reevaluate_beliefs.py` | LLM `belief_reevaluator` — full evidence trail rewrite for contested |
| `belief_engine/pipeline/steps/canonicalize_belief_set.py` | LLM `belief_canonicalizer` — multi-pass merge/split until convergence |
| `belief_engine/store/belief_store.py` | Sole DB+Chroma access layer (short sessions, no LLM under lock) |
| `belief_engine/chroma/belief_chroma.py` | Dedicated `belief_engine_beliefs` Chroma collection |
| `belief_engine/db/schema.py` | DDL for `user_beliefs` + `belief_evidence` |
| `belief_engine/db/models.py` | SQLAlchemy ORM mirroring the schema |
| `belief_engine/db/ensure_schema.py` | Idempotent migration entry point |
| `belief_engine/export/export_beliefs.py` | Content-guarded JSON export |
| `belief_engine/seed/seed_from_memory_files.py` | One-time importer from legacy `resource_user_*.json` files |
| `app/assistant/agents/belief_engine/belief_updater/` | `gpt-5.2` strong-tier; `agent_form.py` defines `BeliefOutput` |
| `app/assistant/agents/belief_engine/belief_reevaluator/` | `gpt-5.2` strong-tier; `RevisedBelief` form |
| `app/assistant/agents/belief_engine/belief_canonicalizer/` | `gpt-5.2` strong-tier; `CanonicalBelief` form |
| `app/assistant/pipelines/pipeline_registry.py:69-77` | Registers all seven domain pipelines + the export pipeline |
| `configs/routines.json:210-336` | Six scheduled domain routines + the export routine |
| `app/routes/insights.py:156-176` | `/api/insights/beliefs` UI surface |
| `resources/kg_derived/resource_user_beliefs.json` | The exported file consumed by dayflow + UI |
| `belief_engine/scripts/show_domain.py`, `dry_run_canonicalize.py` | CLI debugging utilities |

## 10. Cookbook

### Add a new belief domain

1. Pick a domain slug (e.g. `finance`).
2. Add it to the loop in `app/assistant/pipelines/pipeline_registry.py:69`. The factory will build a `BeliefEngineRoutineAdapter("finance")` automatically.
3. Add tag/ticket-type entries to `_DOMAIN_TAGS` and (if relevant) `_DOMAIN_TICKET_TYPES` in `belief_engine/pipeline/steps/collect_evidence.py:28-42`. Without this, `_collect_daily_insights` will never emit anything for the new domain.
4. Add a routine entry in `configs/routines.json` with a unique `time_local` that does not collide with existing belief domains. Set `runner: "pipeline"` and `spec.pipeline_id: "belief_engine_finance"`.
5. The export step picks up new domains automatically — `list_all()` is domain-agnostic.
6. Make sure upstream daily-insights generation tags items with the new domain's tag(s); otherwise the pipeline will run nightly with an empty evidence bundle and skip Step 2.

### Debug a belief

| Symptom | Where to look |
| --- | --- |
| Belief is in the DB but missing from the UI / consumers | Check `status` in `user_beliefs` — only `active` is exported. Then check `_metadata.generated_at` in the JSON; the export is content-guarded so an unchanged set won't bump the timestamp. |
| Belief was unexpectedly merged | Look at the `belief_evidence` row with `source_type='canonicalization'` on the surviving belief — `summary` contains the canonicalizer's reasoning and the absorbed statements. |
| Belief flipped to `contested` | `update_beliefs.py:206-222` — confidence dropped vs. previous value, or LLM explicitly returned `status=contested`. The next `belief_reevaluator` pass writes the resolution. |
| "Why didn't this evidence make it in?" | Run `belief_engine/scripts/show_domain.py`. Then trace: does the daily-insights item carry a tag in `_DOMAIN_TAGS[domain]`? Did the LLM omit its index from `evidence_refs`? Empty `evidence_refs` means the LLM chose not to cite (`update_beliefs.py:65-67`). |
| Want to re-run canonicalization without writing | `CanonicalizeBeliefSetStep.run(ctx, dry_run=True)` — returns the proximity-sorted chunks without LLM calls. The routine adapter does not expose this; use the helper in `belief_engine/scripts/dry_run_canonicalize.py`. |
| Force a re-export after manual DB poking | `python -m belief_engine.export.export_beliefs`. Note the content guard — if you only changed metadata-irrelevant fields, the file won't update; touch a `statement` to force a write. |
| Pipeline crashed mid-domain | The four-step loop aborts on the first exception (`pipeline.py:65-75`) and returns `status='error'` with the failing step's error text. Subsequent domains in the schedule still run independently because each is its own routine invocation. |
