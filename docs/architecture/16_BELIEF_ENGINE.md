# Belief Engine

A nightly inference system that derives a living set of **user beliefs** — concise, agent-ready statements about routines, habits, preferences, and constraints — from the last 14 days of daily insights and cross-day ticket signals. Beliefs live in five SQLite tables (`user_beliefs`, `belief_evidence`, `belief_tags`, `belief_short_id`, `belief_merges`) plus two runtime archive tables, all in the main `emi.db`. After each run the active set is exported to a single resource file (`resources/kg_derived/resource_user_beliefs.json`) that downstream readers consume, while the meal engine additionally queries the DB live (`belief_engine/retrieval.py`).

The belief engine lives in a top-level `belief_engine/` package (not under `app/assistant/pipelines/`), reflecting its design as a system that is structurally independent of the rest of the memory stack — see `belief_engine/__init__.py`. It plugs into the standard pipeline runner only via thin adapters in `belief_engine/pipeline/routine_adapter.py`.

> **This is "v1" (revived).** A from-scratch "v2" rebuild (`belief_engine_v2/`) was built and then **retired as the primary producer** on 2026-06-16. The `beliefs_v2_primary` flag in `configs/subsystems.yaml` is `false`, so **v1 is the sole producer**. v2 code is still on disk (dormant). Three dead-vs-live traps are flagged in §11 — read it before editing anything that looks duplicated.

> Cross-refs: pipeline + routine plumbing is documented in [06_PIPELINES_AND_ROUTINES.md](06_PIPELINES_AND_ROUTINES.md). The primary evidence source — daily insights — is described in [05_DAYFLOW.md](05_DAYFLOW.md).

## 1. Why beliefs are domain-scoped

Beliefs are partitioned by **domain** (`routine`, `health`, `food`, `meal`, `communication`, `sleep`, `work`, `general`) and the engine runs one pipeline pass per domain over a focused evidence slice. Two reasons:

- **Domain-scoped LLM context**: the update / reevaluate steps are told the current domain and stay within it. This keeps prompts focused and fits within model context.
- **Domain-filtered tag routing on intake**: each domain has a `tags` list and a `ticket_types` list (in `configs/belief_domains.yaml`); `CollectEvidenceStep` only emits evidence items whose tags / ticket types match the current domain. Daily insights are tagged at write-time; the engine fans out by tag.

A single `BeliefEnginePipeline` class (`belief_engine/pipeline/pipeline.py`) is parameterised by a `domain` string. `BeliefEngineAdapter` (`belief_engine/pipeline/routine_adapter.py`) loops over every domain marked `enabled: true` in the YAML and runs the per-domain pipeline in sequence. A failure in one domain is collected; the adapter raises once at the end if any domain failed, so the routine records the run as failed (but the other domains still ran).

> **`domain` vs `tags`.** `domain` is the **derivation lane** — which evidence the engine fans out to which pipeline pass. `belief_tags` (§7) is a separate **additive retrieval layer** over the standardized vocab in `configs/belief_tags.yaml`. They are not the same axis: a belief filed under `domain=food` can carry the `dietary` tag and be pulled by the health consumer.

## 2. Where insight tags come from (the upstream that feeds the filter)

Each `actionable_information` item in `resource_daily_insights.json` carries a `tags: List[str]` field assigned by the `daily_timeline_insights` LLM agent when the daily-insights pipeline ran the previous night, constrained to a fixed vocabulary passed as `memory_available_tags`.

That vocabulary is the **union of every enabled domain's `tags`** in `configs/belief_domains.yaml`, computed by `belief_engine.config.list_all_tags()`. So one YAML file controls both production (what the daily-insights LLM may emit) and consumption (which items each belief domain picks up). Adding a tag to a domain automatically lets the daily-insights LLM emit it on its next run.

> Note: this insight-side `domain.tags` vocabulary is **distinct** from the standardized retrieval vocabulary in `configs/belief_tags.yaml` (§7). The former gates daily-insight emission; the latter gates belief retrieval.

## 3. Domain configuration

Single source of truth: **`configs/belief_domains.yaml`**. Loader: `belief_engine/config.py` — `list_enabled_domains()`, `get_domain_config(id)`, `list_all_tags()`. Loaded once per process; `reload_domains()` clears the cache.

Each row is a `DomainConfig(id, enabled, tags, ticket_types, decay_enabled)`. Eight domains are enabled today. Two are noteworthy:

- **`meal`** carries empty `tags`/`ticket_types` on purpose. `feedback_extractor` writes beliefs under `domain='meal'` from user feedback (not insight-mining), so `CollectEvidence` finds nothing and `UpdateBeliefsStep` cleanly skips — but `RecomputeBeliefSnapshot` + `Reevaluate` + `Canonicalize` still run over the existing rows (which were otherwise orphaned: never weighted, never reconciled).
- **`decay_enabled`** is now a **vestigial / dead YAML key**. It was the on/off flag for the old `DecayStaleBeliefsStep`. That step was replaced by `RecomputeBeliefSnapshotStep` (2026-05-11), which runs **universally** with no per-domain flag (see §4). `config.py` still parses `decay_enabled` and `belief_domains.yaml` still sets it on `routine`, but **nothing in the live pipeline reads it.** Do not wire new behaviour to it.

`id` becomes the `domain` column on belief rows, so renaming a domain breaks existing rows — prefer adding a new id and retiring the old.

## 4. Pipeline structure (per domain)

Five steps, defined in `belief_engine/pipeline/pipeline.py`. They share a `_RunContext` dataclass and run sequentially; if any step raises, the pipeline aborts and returns `status='error'` with the failing step.

```
CollectEvidenceStep ─> UpdateBeliefsStep ─> RecomputeBeliefSnapshotStep ─> ReevaluateBeliefsStep ─> CanonicalizeBeliefSetStep
   (no LLM)             (LLM: belief_updater)   (no LLM, universal)         (LLM: belief_reevaluator,    (LLM: merge_verifier,
                                                                             conditional)                 pairwise)
```

A single per-run **scope context** is built at pipeline entry (`load_scope_for_source(kind="subsystem", source_id="belief_engine", …)`) and threaded to the three LLM steps via `ctx.scope_context` — steps never build their own.

### Step 1 — `CollectEvidenceStep`

`belief_engine/pipeline/steps/collect_evidence.py`. Pure file IO, no LLM. Walks the last 14 calendar days of `day_context/<YYYY>/<MM>/<YYYY-MM-DD>/` from two sources:

- **`resource_daily_insights.json`** — for each `actionable_information` item whose tags intersect the domain's tags, emit an `EvidenceItem`. The verbatim user quote (`evidence[0]`) is folded into the summary when present. `temporal_scope == "chronic"` → `weight=3.0`, `signal=confirms`; otherwise `weight=1.5`, `signal=qualifies`.
- **`timeline_merged.json`** — ticket events, **cross-day AGGREGATES only**. Per-event ticket signal already flows through daily insights (extracted from the same timeline), so re-emitting it would double-count. What insights structurally cannot see is a pattern *across* days. So this path tallies `(suggestion_type, outcome)` over the window and emits a signal only above a threshold:
  - **rejected ≥ 2** → `signal=rejects`, `weight = 3.0 + min((n−2)·0.5, 2.0)` (caps at 5.0).
  - **snoozed/deferred ≥ 3** → `signal=qualifies` (pacing-too-frequent), `weight = 2.5 + min((n−3)·0.5, 2.0)`.
  - **accepted** → `signal=confirms`, `weight = 1.0 + min(n/10, 2.0)`, but the noise types `finger_stretch / standing_break / hydration / movement / stretch / walk` are skipped as bare-acceptance noise.

There is **no `kg_edge` evidence path** anymore — `collect_evidence` reads only insights + timelines. Output: an `EvidenceBundle` on `ctx.evidence_bundle`, sorted most-recent-first.

### Step 2 — `UpdateBeliefsStep`

`belief_engine/pipeline/steps/update_beliefs.py`. Calls `belief_engine::belief_updater` with the full evidence block plus the top-K semantically similar existing beliefs (one combined query over the batch; `k=8`, threshold `0.50`).

Per-belief decisions (`agent_form.py`): `create | update | deprecate | no_change`. Applied via `BeliefStore`. Key behaviours:

- **Lock honored**: if the target belief has `locked=1` (owner correction via `/beliefs`), a mutating `update`/`deprecate` is downgraded to `no_change` — evidence still attaches and `observation_count`/`last_confirmed` advance, but statement and status stay exactly as the owner set them.
- **`no_change`** still upserts so `observation_count` increments and cited evidence attaches.
- **Contestation**: a belief is queued in `contested_keys` (and `mark_contested`'d) for Step 4 when the agent returns `status=contested` (on create or update), or when an `update` lowers confidence vs the stored value.
- The agent must populate `evidence_refs` (1-based indices into the bundle) so the right evidence rows attach to the right belief; out-of-range/empty refs attach nothing.
- A `kind` field from the agent (§6) is passed through to the upsert; absent, the store heuristic-classifies it.

### Step 3 — `RecomputeBeliefSnapshotStep`

`belief_engine/pipeline/steps/recompute_belief_snapshot.py` → `belief_engine/decay/recompute.py`. No LLM, idempotent. **This replaced `DecayStaleBeliefsStep` (2026-05-11) and runs universally** — there is no `decay_enabled` gate. It is placed *between* Update (which bumps `last_confirmed`) and Reevaluate (which consumes the contested keys it emits).

For each active belief in the domain it aggregates the belief's `belief_evidence` rows into evidence-weighted support/contradiction weights (see decay v2, §5), derives a confidence band, and writes back `current_support_weight`, `current_contradiction_weight`, `current_net_weight`, `current_confidence_band`, and `last_contradicted_at`. Then:

- band == `faded` → `status='deprecated'` (terminal).
- band ∈ {`contested`, `deprecated_by_contradiction`} → `status='contested'`, and the key is appended onto `ctx.belief_update_result["contested_keys"]` so `ReevaluateBeliefsStep` picks it up.

**All status transitions are gated by `COALESCE(locked,0)=0`** — a locked belief is never faded or flipped.

### Step 4 — `ReevaluateBeliefsStep` (conditional)

`belief_engine/pipeline/steps/reevaluate_beliefs.py`. Runs only if there are contested keys (from Step 2 or Step 3). For each, fetches the **complete** evidence trail from `belief_evidence` (capped at 50, oldest-first) and asks `belief_engine::belief_reevaluator` for an authoritative rewrite. Actions: `rewrite | qualify | split | deprecate | confirm`. `confirm` restores `status=active` and bumps `last_confirmed`. `split` is expected to reuse the original key for one child; a safety net deprecates the original if a split emitted only new keys (orphan guard).

### Step 5 — `CanonicalizeBeliefSetStep`

`belief_engine/pipeline/steps/canonicalize_belief_set.py`. **Rewritten 2026-06-16 to a pairwise-verifier design** (the old chunk-based canonicalizer is dead — §11). Two recall channels *propose* candidate duplicate **pairs** over the domain's active belief set; `belief_engine::merge_verifier` *decides* each pair.

- **Channel 1 — embedding NN**: pairs with cosine `>= MERGE_THRESHOLD (0.80)`. Recall-biased — the verifier is the precision gate.
- **Channel 2 — shared distinctive keyword**: pairs sharing a lightly-stemmed token whose document frequency across the set is in `[KEYWORD_DF_MIN=2, KEYWORD_DF_MAX=12]` **and** cosine `>= KEYWORD_MIN_COSINE (0.50)`. This catches divergent-phrasing dups that embed just below 0.80 ("standing-break nudges" vs "standing break reminders"). The DF band excludes unique words (nothing to pair) and corpus-common/topical words (embedding's job). `_STOP` only pre-drops universal glue.

Pairs are sorted strongest-first and capped at `MAX_PAIRS=4000`. The verifier is **asymmetric** — default not-same; a wrong merge silently destroys a distinct belief — and returns a reconciled `canonical_statement`. On a `same` verdict the better-supported belief (higher `observation_count`) survives, its statement is **rewritten to the canonical statement** (so a superset collapses without dropping the extra clause), and the loser is deprecated via `store.merge_belief`. A local union-find prevents re-merging a belief already folded in this pass. **Owner-locked beliefs are excluded** from both sides.

**Two modes** (`belief_engine/state/sweep_tracker.py`, `decide_mode()`):

- **`full`** — propose pairs across *all* active beliefs in the domain. Runs on the first bootstrap run and every `FULL_SWEEP_INTERVAL_DAYS=7`.
- **`new_only`** — the other ~6 nights. Propose only pairs where at least one side was created since the last full sweep — but compared against the *whole* active set, so a new dup of an old belief is still caught.

The mode is decided **once per parent run** in `BeliefEngineAdapter` (so a midnight rollover can't split domains across modes) and threaded via `ctx.canonicalization_mode`. After a successful `full` run the adapter calls `mark_full_sweep_completed()` (atomic temp-file write to `data/belief_engine_state.json`, gitignored). `dry_run=True` returns embedding-only candidate clusters without LLM calls (used by `scripts/dry_run_canonicalize.py`); the routine adapter never passes it.

## 5. Decay v2 — evidence-weighted half-life

`belief_engine/decay/model.py` (pure math, no DB) + `recompute.py` (the snapshot job, §4 Step 3). Replaces the old "unconfirmed for >N days → deprecate" threshold logic with a **true half-life** model: `w × 0.5^(age_days / half_life)` (50% at one half-life).

**Six belief kinds**, each with a half-life (`HALF_LIFE_DAYS`):

| kind | half-life | example |
| --- | --- | --- |
| `durable_fact` | **None (no decay)** | born in Espoo, has a PhD |
| `stable_relationship` | 1825d (5y) | married to the user's partner |
| `stable_preference` | 365d | likes mayo |
| `routine_pattern` | 90d (DEFAULT) | morning dog walk |
| `episodic_context` | 14d | has a cold this week |
| `transient_state` | 1d | running late |

**Two-weight model**: each evidence event has a `valence` (`support` / `contradict` / `qualify`; `qualify` counts as light support) derived from `signal_type` (or stored explicitly). `compute_belief_weights` decays each event by its belief's kind half-life and sums into `support_weight` / `contradiction_weight`; `net = support − contradiction`, `conflict_ratio = contradiction / total`.

**Band classification** (`band_for_weights`, order matters, `BandThresholds`):
1. `conflict_ratio ≥ 0.6` → `deprecated_by_contradiction`
2. both `support ≥ 2.0` and `contradiction ≥ 2.0` → `contested`
3. else by `net`: `≥4.0` high · `≥1.5` medium · `≥0.3` low · below → `faded`

`durable_fact` protection (no decay) is what makes universal application safe. `classify_kind_heuristic` backfills a kind for rows that lack one (scope=temporary → `episodic_context`; else a key-prefix table, then a domain fallback, then `routine_pattern`). Evidence rows snapshot `half_life_days_snapshot` at insert time so later constant changes don't retroactively reshape history. Base weights per `source_type` (`EVIDENCE_BASE_WEIGHT`) supply a fallback when an evidence row's weight is 0/NULL; `canonicalization`/`deprecation` rows weigh 0 (bookkeeping, ignored).

## 6. Belief schema

**Authoritative schema: `belief_engine/db/models.py`** (SQLAlchemy ORM; tables are created via `Base.metadata.create_all` at app boot). ⚠️ **`belief_engine/db/schema.py` (`ensure_schema`, prints "Migration OK") is a PARTIAL/legacy DDL** — it predates decay v2 and the lock column, so its `user_beliefs`/`belief_evidence` `CREATE TABLE` statements are **missing** `locked`, `kind`, `last_contradicted_at`, the four `current_*` columns, and `valence` / `half_life_days_snapshot` / `extracted_by`. It will create the *new* side tables correctly, but trust `models.py` for the live column set. Tables live in the **main `emi.db`** (`ensure_schema._belief_db_path()` resolves the app DB URI), not a separate file.

### `user_beliefs`

| column | notes |
| --- | --- |
| `id` | TEXT PK (UUID) |
| `domain` | derivation lane (§3) |
| `belief_key` | TEXT UNIQUE — stable dot-slug, e.g. `routine.dog_walk.morning`; updates target it |
| `statement` | prose, agent-ready |
| `confidence` | `high`/`medium`/`low` — **stored at extraction time**; agents read `current_confidence_band` instead |
| `scope` | `chronic` / `temporary` (legacy axis; decay now keys on `kind`) |
| `status` | `active` / `contested` / `deprecated`. Only `active` is exported |
| `locked` | INTEGER, default 0. **1 = owner correction via `/beliefs`**; updater/decay/canonicalize must not modify or deprecate it |
| `kind` | decay v2 — drives half-life (§5) |
| `conditions` | JSON, structured qualifiers (reevaluator writes `{"text": …}`) |
| `observation_count` | incremented per upsert; merges add the absorbed counts |
| `first_observed`, `last_confirmed` | ISO bookends |
| `last_contradicted_at` | ISO — written by recompute when contradiction evidence exists |
| `current_support_weight`, `current_contradiction_weight`, `current_net_weight` | FLOAT — decay-v2 snapshot, written nightly by recompute |
| `current_confidence_band` | `high`/`medium`/`low`/`faded`/`contested`/`deprecated_by_contradiction` — the *effective* confidence |
| `created_at`, `updated_at` | ISO row lifecycle |

### `belief_evidence`

One row per signal touching a belief. `belief_id` is **`ON DELETE CASCADE`** against `user_beliefs(id)`.

| column | notes |
| --- | --- |
| `id`, `belief_id` | PK / FK |
| `source_type` | `daily_insights` · `ticket_rejection` · `ticket_acceptance` · `canonicalization` · `deprecation` · `decay_review` · `manual` (no `kg_edge` emitted anymore) |
| `source_date`, `source_ref` | YYYY-MM-DD / optional back-ref |
| `signal_type` | `confirms` / `qualifies` / `contradicts` / `rejects` |
| `summary`, `raw_text` | digest + verbatim quote |
| `weight` | 0.0–5.0 |
| `valence` | decay v2 — `support` / `contradict` / `qualify`; derived from `signal_type` if not supplied |
| `half_life_days_snapshot` | INTEGER — half-life at creation time (replay reproducibility) |
| `extracted_by` | provenance — which agent/step wrote the row |

### `belief_tags`, `belief_short_id`, `belief_merges`

The three additive side tables (DDL in `schema.py`; also `CREATE … IF NOT EXISTS`'d by their writers):

- **`belief_tags`** `(belief_id, tag, assigned_at, method)` — the standardized retrieval vocabulary (§7). PK `(belief_id, tag)`; cascades on belief delete.
- **`belief_short_id`** `(belief_id PK, short_id UNIQUE, assigned_at)` — monotonic, never-reused `b<n>` handle (§8).
- **`belief_merges`** `(loser_id PK, survivor_id, merged_at, reason)` — merge-provenance redirect (§8).

### Embeddings — `BeliefChroma`

`belief_engine/chroma/belief_chroma.py`, collection `belief_engine_beliefs`, keyed by `belief_id`, sharing the project embedding model/client but managed separately. Used for `find_similar()` (Step 2) and the canonicalization recall channel (`get_all_for_domain` returns `(id, vector)` pairs). Embeddings are upserted on every `upsert_belief` and deleted on merge. Plain `deprecate()` does **not** delete the embedding — so `BeliefStore.find_similar` re-checks `status == "active"` after the Chroma hit to filter deprecated-but-not-merged rows.

## 7. Tagging layer

`belief_engine/tagging.py` + `configs/belief_tags.yaml`. A **24-tag standardized vocabulary** is the controlled set a belief may carry for **retrieval** ("pull beliefs to where they're needed"). This is the anti-proliferation guarantee — `sanitize()` is the single enforcement point; off-vocab tags are dropped. Multi-label: a belief carries every tag that applies.

`tag_beliefs(mode=…)` drives `belief_engine::belief_tagger` (mini tier) over active beliefs in batches of 15. Each belief gets the **union of its `domain`** (itself a valid vocab tag, except `general` which is deliberately not a tag) **and the LLM's cross-cutting tags**. So a belief the LLM leaves empty is still tagged by its domain; the LLM only *adds* reach. `mode="needs"` selects untagged + stale beliefs (statement changed since `assigned_at`) for the nightly pass; `mode="all"` is the one-time backfill.

**Consumer pull-sets** (`pull_sets` in the YAML): `meal_engine`, `health_status`, `entertainment`, `routine_stage`. A consumer pulls a tag *set*; a belief surfaces if it carries *any* tag in that set. **Bridge tags** (`dietary`, `family`, `social`, `meal`) let a consumer reach beliefs filed under a different primary domain.

## 8. Identity layer

`belief_engine/identity.py`. A compact, LLM-citable **short id `b<n>`** assigned once per belief via a monotonic counter, **never reused or changed**. `ensure_short_id` is called at belief creation (so a new belief is immediately citable); `assign_short_ids` is the nightly/backfill sweep. A merged-away belief **keeps its short id** so it stays citable for provenance. `record_merge` writes the `belief_merges` redirect (loser → survivor) on every merge in `BeliefStore.merge_belief`.

## 9. Archive lifecycle

`belief_engine/archive.py`. Deprecation only flips `status='deprecated'` — it never evicts the row, so dead beliefs accumulate in the live tables forever (they reached 94% before the first sweep). `archive_deprecated_beliefs()` atomically moves every `status='deprecated'` belief **and its evidence** OUT of `user_beliefs`/`belief_evidence` into **`user_beliefs_archive` / `belief_evidence_archive`** (created `AS SELECT * … WHERE 0`, schema-drift-tolerant) in the same `emi.db`, and drops the belief's `belief_tags`. So the **live tables hold only `active` + `contested`** beliefs, live-by-construction.

`belief_short_id` (counter integrity) and `belief_merges` (write-only provenance) stay live; their refs into the archive dangle harmlessly (FK enforcement is `OFF` during the move so deletes don't cascade into them). Idempotent — no-op when nothing is deprecated. Driven nightly by the `belief_archive` routine.

## 10. Export + live retrieval

### `export_beliefs` (`belief_engine/export/export_beliefs.py`)

Called inline from `BeliefEngineAdapter.run()` after a successful domain loop (also via `BeliefEngineExportAdapter` for manual re-export). It:

1. Reads **all active** beliefs (`BeliefStore.list_all(status="active")`).
2. Joins `belief_tags` + `belief_short_id` (read-only sqlite connection) so the export shape matches what consumers read.
3. Per belief, serializes a flat dict — **`confidence` is `current_confidence_band or confidence`** (prefer the evidence-weighted snapshot, fall back to the LLM's stored read for a fresh belief), plus `short_id`, `tags`, `kind`, `scope`, `status`, `observation_count`, `first_observed`, `last_confirmed`, optional `conditions`.
4. **Content-guard**: compares the `beliefs` list (sorted, ignoring metadata) byte-for-byte against the existing file; if unchanged, the file is **not rewritten** (avoids polluting mtime watchers).
5. Writes atomically (sibling `.tmp` + rename).

> **v1-vs-v2 gate**: `export_beliefs` **no-ops only when `beliefs_v2_primary` is ON** (then v2 would own the file). The flag is `false`, so v1 writes the file normally.

### Live retrieval — `beliefs_for_context` (`belief_engine/retrieval.py`)

The old "no live DB query path" claim is **only partly true now**. `beliefs_for_context(query=, tags=, k=)` queries the live DB and returns a ranked, optionally tag-scoped candidate set of **active** beliefs, scored by `0.55·relevance (embedding cosine to query) + 0.25·recency (30d half-life) + 0.20·frequency (saturating log of observation_count)`. `status='active'` is the only **hard** filter; the tag scope (a consumer's `pull_set`) is applied **only when the store is actually tagged** (else high-recall: return all, never nothing). Each item carries its `short_id` + tags. **The meal engine uses this live** (`meal_context_builder._build_food_beliefs_v2`, gated by the `meal_beliefs_v2` flag, default on). The dayflow stages below still read the exported JSON.

### Where beliefs surface

`resource_user_beliefs.json` is read by:

- **Dayflow routine stage** — `app/assistant/pipelines/dayflow/steps/dayflow_routine_stage.py`. Feeds the agent that regenerates `resource_dayflow_routine.md`. Admits a belief by routine-shaping `kind` OR a `routine_stage` pull-set tag.
- **Health status stage** — `health_status_stage.py`. Admits `health`/`sleep` domain OR a `health_status` bridge tag.
- **Entertainment advisor stage** — `entertainment_advisor_stage.py`. Uses the `entertainment` pull-set.
- **Dayflow orchestrator room** — `resource_user_beliefs` is an allowed resource, injectable into any agent under that room's scope.

  > These three stages import `pull_set` from `belief_engine_v2.tags` — a **vocab helper that reads `configs/belief_tags.yaml`** (identical to `belief_engine.tagging.pull_set`); it does **not** touch the retired v2 store. Harmless, but a candidate for repointing in cleanup.

- **`/beliefs` owner surface** — `app/routes/beliefs.py` (`beliefs_admin_bp`, gated by `reject_if_not_local`). The **canonical owner surface**. Lists every live belief with `domain`/`kind`/`status`/`band`/`net` + tags and lets the owner **edit the statement, reclassify domain, retag, suppress (deprecate), or LOCK** it. Routes: `/beliefs`, `/api/beliefs/list` (filterable; `include_archived=1` also reads the archive tables, read-only), `/api/beliefs/item` (full state + evidence trail), `/api/beliefs/update` (direct row update — v1 rows are mutable, no override side tables; the **lock** is the durability mechanism), `/api/beliefs/trends`. The old `/api/insights/beliefs` panel is secondary.

## 11. Live-vs-dead traps (do not edit dead code)

The v2 satellites were deleted on 2026-07-07 (`subconscious/belief_tagging.py`, the
`belief_tag_new` routine + handler, `beliefs_shadow` route, `belief_v2_shadow_ingest` step, the
`beliefs_v2_primary` export guard); `belief_engine_v2/` itself has **zero importers** and awaits
final removal. Two legacy pairs remain on disk — edit only the live one:

1. **Decay**: live = `belief_engine/decay/` + `RecomputeBeliefSnapshotStep` (universal, evidence-weighted). Dead = `belief_engine/pipeline/steps/decay_stale_beliefs.py` (`DecayStaleBeliefsStep`) + `BeliefStore.decay_temporary_beliefs` / `flag_stale_chronic_beliefs` — the old time-threshold path, no longer wired into `pipeline.py` (the `decay_enabled` YAML key that gated it is now inert). Sandbox-only.
2. **Canonicalization**: live = `belief_engine::merge_verifier` + the pairwise dedup in `canonicalize_belief_set.py` (durable `belief_distinct_pairs` verdicts, per-domain sweep stamps, per-run call cap). Dead = the `belief_engine::belief_canonicalizer` agent (chunk-based; on disk, referenced only by `scripts/backup_beliefs.py`, never by the pipeline).

## 12. Scheduling

Routines now live in **`configs/routines/public/*.json`** (one file per routine), not the old monolithic `configs/routines.json`. Agents reference a `model_tier` (`strong`/`mini`), not a literal model id.

| Routine file | Time | Runner | What it does |
| --- | --- | --- | --- |
| `belief_engine.json` | 00:30 | pipeline (`belief_engine`) | `BeliefEngineAdapter` — loop enabled domains, 5-step pipeline each, export inline on success |
| `belief_archive.json` | 05:30 | function (`belief_archive`) | Evict deprecated beliefs (+ evidence) to the `*_archive` tables (§9) |
| `belief_tag_v1.json` | 05:35 | function (`belief_tag_v1`) | Tag untagged/stale active beliefs (`mode="needs"`, `max_per_run=60`) (§7) |
| `belief_engine_export.json` | (disabled) | pipeline | Manual re-export only; export is now inline. `/run-routine belief_engine_export` for a manual run |

Routine functions are registered as `@routine_handler`-decorated handlers in `app/assistant/routine_handlers/` (`belief_archive.py`, `belief_tag_v1.py`), not in `routine_functions.py`.

## 13. Agents

| Agent | Tier / engine | Role |
| --- | --- | --- |
| `belief_engine::belief_updater` | strong (`gpt-5.2`) | Step 2 — `create/update/deprecate/no_change` per belief |
| `belief_engine::belief_reevaluator` | strong (`gpt-5.2`) | Step 4 — authoritative rewrite of contested beliefs from full trail |
| `belief_engine::merge_verifier` | strong (`gpt-5.2`) | Step 5 — per-pair same/not-same decision + reconciled `canonical_statement` |
| `belief_engine::belief_tagger` | mini (`gpt-5.6-luna`) | §7 — cross-cutting tags from the standardized vocab |
| `belief_engine::belief_canonicalizer` | mini | **DEAD** (§11 trap 3) — old chunk-based canonicalizer |

## 14. Why the beliefs DB is "fragile / handle with extreme care"

1. **Tables live in `emi.db`, not a separate file.** A schema mistake (or a stray `DELETE FROM user_beliefs`) hits the same file as the unified log, KG, and pod store.
2. **`belief_evidence.belief_id` is `ON DELETE CASCADE`.** A direct `DELETE` cascades the entire evidence trail. Only application-level `deprecate()` (sets `status='deprecated'`) is safe; eviction goes through the archive sweep (which turns FK enforcement off precisely so its deletes don't cascade into the provenance tables).
3. **Two-store consistency.** A belief lives in both SQLite and Chroma. The store syncs on writes; a direct DB edit leaves Chroma orphaned, and deprecated-but-unmerged beliefs keep their embeddings (`find_similar` filters them post-hoc).

Practical implication: any script that prunes/rebalances/re-seeds beliefs should go through `BeliefStore` (`belief_engine/store/belief_store.py`) and never touch the tables directly. Per the standing rule: ask before running heavy DB scripts while the assistant is running.

## 15. Key files

| File | Purpose |
| --- | --- |
| `belief_engine/__init__.py` | Package overview + cross-ref |
| `belief_engine/pipeline/pipeline.py` | `BeliefEnginePipeline` — 5-step orchestrator, one domain per call |
| `belief_engine/pipeline/routine_adapter.py` | `BeliefEngineAdapter` (loops domains, decides sweep mode once, exports inline) / `BeliefEngineExportAdapter` |
| `belief_engine/pipeline/steps/collect_evidence.py` | Tag-filtered scan of 14 days of insights + cross-day ticket aggregates |
| `belief_engine/pipeline/steps/update_beliefs.py` | LLM `belief_updater`; lock honoring; contestation queue |
| `belief_engine/pipeline/steps/recompute_belief_snapshot.py` | Universal evidence-weighted snapshot + fade/contest transitions |
| `belief_engine/pipeline/steps/reevaluate_beliefs.py` | LLM `belief_reevaluator` — full-trail rewrite of contested |
| `belief_engine/pipeline/steps/canonicalize_belief_set.py` | Pairwise merge via `merge_verifier`; full/new_only modes |
| `belief_engine/pipeline/steps/decay_stale_beliefs.py` | **DEAD** — old time-threshold decay (§11 trap 2) |
| `belief_engine/decay/model.py` | Decay v2 math kernel — kinds, half-lives, two-weight, bands |
| `belief_engine/decay/recompute.py` | The snapshot job (walks rows, writes back) |
| `belief_engine/store/belief_store.py` | Sole DB+Chroma access layer (short sessions; merge/deprecate/upsert; legacy decay helpers) |
| `belief_engine/chroma/belief_chroma.py` | Dedicated `belief_engine_beliefs` Chroma collection |
| `belief_engine/retrieval.py` | `beliefs_for_context` — live ranked, tag-scoped read (meal engine) |
| `belief_engine/tagging.py` | v1 tag writer + `sanitize` / `pull_set` |
| `belief_engine/identity.py` | Short ids (`b<n>`) + `belief_merges` provenance |
| `belief_engine/archive.py` | Nightly eviction of deprecated beliefs to `*_archive` |
| `belief_engine/state/sweep_tracker.py` | `decide_mode` (full vs new_only), `FULL_SWEEP_INTERVAL_DAYS=7` |
| `belief_engine/config.py` | `belief_domains.yaml` loader |
| `belief_engine/db/models.py` | **Authoritative** SQLAlchemy schema (all 5 tables) |
| `belief_engine/db/schema.py` | **Partial/legacy** DDL — missing decay-v2 + lock columns (§6) |
| `belief_engine/db/ensure_schema.py` | Idempotent legacy migration entry point (prints "Migration OK") |
| `belief_engine/export/export_beliefs.py` | Content-guarded JSON export (v2-flag-gated no-op) |
| `app/assistant/agents/belief_engine/{belief_updater,belief_reevaluator,merge_verifier,belief_tagger}/` | Live agents |
| `app/assistant/agents/belief_engine/belief_canonicalizer/` | **DEAD** agent (§11 trap 3) |
| `app/assistant/routine_handlers/{belief_archive,belief_tag_v1}.py` | Routine function handlers |
| `app/routes/beliefs.py` | `/beliefs` owner surface (`beliefs_admin_bp`, local-only) |
| `configs/belief_domains.yaml` | Domains (derivation lane); `decay_enabled` is vestigial |
| `configs/belief_tags.yaml` | Standardized 24-tag retrieval vocab + consumer `pull_sets` |
| `configs/routines/public/belief_*.json` | Routine schedules |
| `configs/subsystems.yaml` | `beliefs_v2_primary` flag (false → v1 sole producer) |
| `app/assistant/pipelines/pipeline_registry.py` | Registers `belief_engine` + `belief_engine_export` adapters |
| `resources/kg_derived/resource_user_beliefs.json` | Exported file consumed by dayflow + UI |

## 16. Cookbook

### Add a new belief domain

1. Add a row to `configs/belief_domains.yaml` (`id`, `enabled: true`, `tags`, optional `ticket_types`). `decay_enabled` is ignored — leave it out.
2. The unified `BeliefEngineAdapter` picks it up automatically (no per-domain pipeline registration, no per-domain routine).
3. Ensure upstream daily-insights tagging emits the new domain's tag(s) — otherwise the domain runs nightly with an empty bundle and `UpdateBeliefsStep` skips. Export, recompute, and canonicalize are domain-agnostic.

### Debug a belief

| Symptom | Where to look |
| --- | --- |
| In the DB but missing from consumers | `status` (only `active` exported) and `_metadata.generated_at` (export is content-guarded). For live meal retrieval, check it carries a `meal_engine` pull-set tag. |
| Unexpected confidence | It's the **snapshot band** (`current_confidence_band`), not the stored `confidence`. Check the belief's evidence weights/ages and `kind` (half-life). |
| Faded / auto-deprecated | `RecomputeBeliefSnapshotStep`: net weight fell below the floor. Look at evidence ages vs the kind's half-life. Then the `belief_archive` sweep moved it to `user_beliefs_archive`. |
| Flipped to `contested` | Either Step 2 (confidence drop / explicit contested) or Step 3 (`conflict_ratio ≥ 0.6` or both weights high). `belief_reevaluator` writes the resolution. |
| Unexpectedly merged | `belief_evidence` row `source_type='canonicalization'` on the survivor (verifier reasoning + absorbed statements), and the `belief_merges` redirect. |
| Owner edit didn't stick overnight | Confirm `locked=1` — only locked beliefs are protected from the updater/decay/canonicalize. |
| Force a re-export | `python -m belief_engine.export.export_beliefs` (content-guarded — touch a statement to force a write). |
