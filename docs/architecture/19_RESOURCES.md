# Resources

## 1. What resources are (and why they matter)

Resources are the **shared synchronized context layer** of EmiOS.

The `resources/` directory is a tree of named, file-backed JSON / markdown blobs. Each file has a stable `resource_id` (the filename without extension, prefixed `resource_`). Agents declare which resources they need in their `config.yaml`'s `user_context_items`, and the context injector loads + injects them at prompt-build time.

That sounds boring. The architecturally interesting part is the synchronization story:

- **Truth-finding is expensive** — the KG, the unified log, the calendar, sleep computation, health inference, belief updating. Each requires substantial work to derive a current view.
- **Many agents need the same view, often within seconds of each other.** A master_room agent answering a chat at 14:02 and the dayflow_orchestrator's strategic_planner at 14:03 should both see the *same* calendar, the *same* health status, the *same* belief set.
- **Resources are the denormalized projection.** Pipelines do the expensive work once, write to a stable read-optimized resource, and downstream agents read cheaply and consistently.
- **File freshness is the sync mechanism.** No pub/sub, no message bus. When a pipeline finishes, the file's mtime advances; ResourceManager's cache picks it up on the next read; every consumer sees the new value.

It's the ETL → data-mart pattern, applied to AI agent context. The dayflow pipeline (and friends) is the ETL; `resources/` is the data mart; agents are the analytics consumers.

This is also why agents *don't* directly query the KG, the unified log, or the belief DB for routine context — those queries would be expensive *and* inconsistent across agents. They go through resources.

## 2. The four resource lifecycle classes

Resources fall into four authorship categories with different lifecycles and different hand-edit safety:

| Class | Who writes | Hand-edit safety | Examples |
| --- | --- | --- | --- |
| **User-authored** | Only the user | Safe; expected | `resources/user/`, `resources/instructions/` |
| **Setup-seeded** | Setup wizard once, then user | Safe after seeding | `resources/assistant/resource_assistant_personality_data.json`, `resources/user/resource_user_data.json` |
| **Pipeline-derived** | A scheduled pipeline | Will be clobbered on next run | `resources/dayflow_pipeline_outputs/`, `resources/kg_derived/`, `resources/daily_insights_pipeline_outputs/` |
| **Runtime-snapshots** | Live system state on each tick | Will be overwritten frequently | `resources/status/` |

The directory layout *is* the lifecycle taxonomy. Putting a resource in the wrong directory is a category error.

## 3. The directory taxonomy

```
resources/
  user/                              [user-authored / setup-seeded]
    resource_user_data.json          — name, pronouns, timezone, family
    user_bio.json                    — style/background/projects/values/preferences;
                                       seeded by setup wizard, edited via
                                       /settings/user-bio, injected into chat_gate
                                       prompts via UserBioContextService
    resource_kg_interests.json       — what the user wants the assistant to track
    resource_wiki_sections.json      — biographical taxonomy

  assistant/                         [setup-seeded, then user-tunable]
    assistant_core.json              — the assistant's identity (name, voice, tone)
    resource_assistant_personality_data.json
    resource_assistant_limits_data.json

  instructions/                      [user-authored, behavior-shaping]
    resource_assistant_guidelines.md  — house rules
    resource_assistant_limits.md      — hard constraints (privacy, scope)
    resource_assistant_personality.md — speaking style
    resource_action_decider_instructions.md
    resource_activity_log.md

  kg_derived/                        [pipeline-derived: belief engine, KG]
    resource_user_beliefs.json       — exported by belief_engine (inline at run end)
    resource_user_beliefs_v2.json    — v2 export (same shape; cutover-gated)
    resource_user_beliefs_communication_canonicalized.json — on disk, no current reader
    timelines/<entity>.md            — per-entity narrative timelines

  daily_insights_pipeline_outputs/   [pipeline-derived: daily_insights]
    resource_daily_insights.json     — per-day actionable info
    resource_daily_assessment_summary.json
    resource_daily_assessment_summary_text.md
    resource_daily_context.md

  dayflow_pipeline_outputs/          [pipeline-derived: dayflow pipeline]
    resource_expected_calendar.json  — canonical schedule for today
    resource_user_health_status.md   — current health snapshot
    resource_user_calendar.json
    resource_dayflow_routine.md      — generated daily routine
    resource_sleep_summary.json
    resource_diet_log_today.json
    resource_activity_tracker_output.json
    resource_afk_statistics_output.json
    resource_entertainment_advisor_output.json
    resource_desktop_activity_recent.md
    resource_health_inference_output.json
    resource_conversation_starters_latest.json
    ... and contracts in RESOURCE_CONTRACTS.md

  day_context/                       [pipeline-derived: per-day archive]
    <YYYY>/<MM>/<YYYY-MM-DD>/       — per-day folders archived by date

  subconscious/                      [pipeline-derived: subconscious + meal/grocery loops]
    resource_concerns_register.json  — open concerns the mind is tracking
    resource_digest_state.json
    resource_grocery_inventory.json, resource_meal_feedback_state.json, ...

  weekly_insights_pipeline_outputs/  [pipeline-derived: weekly_insights]
    resource_weekly_insights_latest.json

  status/                            [runtime snapshots]
    resource_dayflow_orchestrator_status.json
    resource_dayflow_status.json
    resource_manager_invocation_status.json
    resource_routine_status.json
    resource_runtime_concurrency_status.json
    planner_watermark.json
    apple_music_related_artists_cache.json

  pointers/                          [pipeline-derived: indirection layer]
    resource_*_latest.json           — pointers to the most recent snapshot

  templates/                         [special: pre-compilation]
    instructions/                    — Jinja templates compiled to instructions/

  context/                           [pipeline-derived: contextual]
    global/, user/
  context_engine/                    [pipeline-derived: per-room context engine]
    master_room/

  resource_current_location.json     [flat at root: runtime snapshot]
  resource_routine_status.json       [flat at root: runtime snapshot]
  resource_emi_accounts.json         [flat at root: account config; gitignored]
  env_registry_user.json             [flat at root: account/secret registry; gitignored]
```

The loader (`load_all_from_directory`) skips a fixed set of directory names anywhere in the path — `skip_dir_names = {tmp, tests, __pycache__, templates, day_context, pointers}` — plus dotfiles and `README*`. `templates/` is excluded here because it's compiled in a separate phase; `day_context/`/`pointers/` because they're indirection, not directly-injectable values.

**Special cases:**
- **`templates/`** is not a normal resource directory. Files there contain Jinja template tokens (`{{ ... }}` / `{% ... %}`) and **must be compiled to concrete values before injection** — `ResourceManager._assert_concrete_resource()` raises `ValueError` if a resource with template tokens is offered for injection. Compiled outputs land in `instructions/` or other consumer directories.
- **`pointers/`** is an indirection layer: `resource_X_latest.json` typically contains a path or id pointing at a versioned artifact elsewhere. Useful when the latest version's filename changes but consumers want a stable pointer.
- **`memory/`** previously held the output of the dead `memory_apply_fact` subsystem; removed 2026-04-28. The one surviving live file (`user_bio.json`) migrated to `resources/user/`.
- **Personal overlays.** For any text resource `<stem><suffix>`, a sibling `<stem>_personal<suffix>` (e.g. `resource_assistant_guidelines_personal.md`) is auto-appended onto the public base at load time (PHASE 3 of `load_all_from_directory`). The personal content goes *after* the base, so later instructions win at LLM read time and personal directives override the template defaults without editing the public file. `_personal` siblings are not standalone resources (the scan skips any stem ending in `_personal`) and are gitignored — the convention keeps personal preferences out of the public repo while shipping a shared template as the baseline.

## 4. The metadata envelope (emerging convention, not universal)

Larger pipeline-derived resources tend to wrap their content in an `_metadata` envelope:

```json
{
  "_metadata": {
    "resource_id": "resource_user_beliefs",
    "schema_version": "1.0",
    "generated_at": "2026-04-28T10:30:00Z",
    "entry_count": 183,
    "description": "Living belief set about the user..."
  },
  "beliefs": [ ... ]
}
```

Smaller resources (calendar, status snapshots, runtime caches) often skip the envelope and put fields at the top level (e.g. `resource_expected_calendar.json` has flat `date`, `expected_schedule`, etc.).

The convention isn't enforced — readers are expected to know the shape of each resource they consume. Per-resource shape contracts for the dayflow pipeline outputs live in `app/assistant/pipelines/dayflow/RESOURCE_CONTRACTS.md`. Other pipelines' resources don't yet have a centralized contracts file.

## 5. ResourceManager — the access layer

`app/resource_manager/resource_manager.py` (~600 lines).

**Responsibilities:**
- Load resources from disk on startup or on first read.
- Maintain an in-process cache (`_resource_values`) keyed by `resource_id`.
- Track each cached resource's source-file mtime (`_cached_mtimes`) for staleness detection.
- Auto-reload on read when the file mtime advances — pipelines write, consumers see updates without explicit invalidation.
- Per-file locks (`_file_locks`) for concurrent-write safety.
- Refuse to inject resources containing template tokens — those must be compiled first (`_assert_concrete_resource`).
- Mirror values to `DI.global_blackboard` so callers reading directly from the blackboard still see fresh values (best-effort; the ResourceManager cache is the source of truth).

**Read API:** `get_resource(*, scope_context, resource_id, required=False)` — `scope_context` is **mandatory** (`ResourceResolver._resolve` raises `ValueError` if it's `None`); the call returns the resolved value, auto-refreshing if the backing file is newer (or computing it, for dynamic resources — see below). `required=True` raises `ValueError` when the value is missing. `get_resources(*, scope_context, resource_ids, ...)` is the batch form.

**Write API:** persistent updates from agents (e.g., learned preferences) go through `update_resource(resource_id, value, persist=True)` so the cache and the file stay in sync (and `refresh_resource(resource_id)` force-reloads one from disk).

**Single instance:** `DI.resource_manager` is the singleton; agents resolve resources through it, never by reading the file directly.

### Scope-gated access

Every read flows through a scope gate before any value is produced. `get_resource` reads `scope_context.resources` and enforces:

- **Denylist:** if `resource_id` ∈ `denied_resources` → `PermissionError`.
- **Allowlist:** if `allowed_global_resources` is not `"all"` and doesn't contain `resource_id` → `PermissionError` (logged at WARNING with the offending id and the scope's allow set).
- **Lock (optional, thing-side):** if the resource carries a `requires_scope` lock, the caller's scope must satisfy it via `scope_gate.scope_gate_passes(...)` (the same lock mechanism skills use) → `PermissionError` on mismatch. No lock = free.

`ResourceResolver` (`agent_runtime/services/resource_resolver.py`) is the thin runtime shim agents go through; it re-raises `PermissionError` (logged at DEBUG) and any read error. This is the resource-layer half of the four-layer scope model (`docs/architecture/SCOPE.md`): the scope key on one side, the resource's allow/deny/lock on the other.

### Dynamic / computed resources

Not every resource is file-backed. A resource can be **computed per read** from the caller's scope:

- `register_provider(resource_id, provider, *, requires_scope=None)` registers `resource_id` so that `get_resource` returns `provider(scope_dict)` instead of reading a file. The provider receives the normalized scope dict, so the *same* resource id can yield different values for different callers.
- `register_lock(resource_id, requires_scope)` attaches a `requires_scope` lock to an existing (file-backed) resource without registering a provider.

Both ride the standard scope-gated `get_resource` path (allow/deny still apply first). Wired in `app/bootstrap.py` — the **only** `register_provider`/`register_lock` call sites today:

| Resource | How it's produced | Lock |
| --- | --- | --- |
| `resource_accounts` | `DI.env_registry.render_accounts_for_scope(scope)` — principal- and per-account-authority-filtered account text (NOT file-backed) | — |
| `resource_email_accounts` | same provider, `categories={"email"}` — the email/calendar/todos domain view, so social accounts never enter that prompt (relevance, not permission) | — |
| `resource_user_email` | file-backed | `{"acting_as": "user"}` — withheld in `/actas self` mode, where email-as-self governs instead |

These replace the old auto-injected `available_accounts`; see `docs/architecture/SECRETS_ACCOUNTS.md` for the account/secret model.

## 6. How agents declare resource needs

In `config.yaml` for any agent:

```yaml
user_context_items:
  - resource_user_data
  - resource_user_beliefs
  - resource_expected_calendar
  - active_dayflow_items     # blackboard keys also live here
  - agent_input              # the inbound Message
```

Per CLAUDE.md, the **context injector** (`app/assistant/agent_runtime/services/context_injector.py`) walks `user_context_items` and resolves each:

- `resource_*` prefix → `ResourceResolver.get_global_resource(resource_id, scope_context=...)` → `ResourceManager.get_resource(...)`. The injector pulls `scope_context` off the agent's blackboard and raises `ValueError` if it's absent — the scope gate (§5) runs on every injected resource, so an agent's declared item still has to clear allow/deny/lock.
- Special blackboard keys (`active_dayflow_items`, `planned_tasks`, etc.) → resolve from blackboard.
- `agent_input` → comes from the inbound Message.

Each agent gets *only* the resources it declares — no global "everything injected" scope. This is the **explicit context principle**: agents pick their inputs.

There's also a **keyword-driven** path: `task_keyword_resources` in agent config maps user-message keywords to resources. E.g., a chat with "doordash" in it can trigger injection of `resource_doordash_guidelines`. Implemented in `keyword_resource_index.py`.

## 7. Pipelines that own resources (the writers)

Different pipelines own different resource subdirectories:

| Pipeline | Owns | Cadence |
| --- | --- | --- |
| `dayflow_pipeline` (NOT the orchestrator) | `dayflow_pipeline_outputs/` (calendar, health, sleep, diet, activity, daily routine) | Multi-stage, throughout the day |
| `daily_insights_pipeline` | `daily_insights_pipeline_outputs/` (per-day insights, themes, assessments) | Once per day, midnight-ish |
| `belief_engine` | `kg_derived/resource_user_beliefs.json` | 00:30 daily (when domain runs); inline export at end of run |
| `entity_card_refresh` (function routine) | entity card v2 *tables* (DB-backed, not resource files); see `12_ENTITY_CARDS.md` | Daily 04:30 |
| `wiki_generator` | (wiki output; see `11_WIKI_GENERATOR.md`) | Daily |
| KG pipeline | (KG-derived resources e.g. `resource_kg_interests`) | Nightly |
| Setup wizard | `user/`, `assistant/` (one-time seeding) | Once, on first install |
| Routine manager | `status/resource_routine_status.json` | Each routine tick |
| Manager invoker | `status/resource_manager_invocation_status.json` | Each manager invocation |

**The dayflow pipeline is *not* the dayflow orchestrator.** They share a name but play very different roles:

- **dayflow pipeline** (`app/assistant/pipelines/dayflow/`) — *senses + projects*. Computes user state and writes resources.
- **dayflow orchestrator** (`app/assistant/dayflow_orchestrator/`) — *acts*. Reads the resources and the items table, picks tasks, dispatches actions.

The orchestrator is a consumer of the pipeline's outputs.

## 8. Atomicity and freshness guarantees

**Atomicity at write time** is per-writer; not yet a global guarantee:
- `belief_engine/export/export_beliefs.py` writes atomically (temp + rename) as of 2026-04-28.
- ResourceManager's own update path uses `tempfile` + atomic move when persisting agent updates.
- Older pipeline writers may use `path.write_text(...)` directly — a crash mid-write can leave a corrupt file. Worth auditing per writer when consumers depend on never seeing a partial.

**Cache freshness** is mtime-driven. ResourceManager compares the current file mtime against the cached mtime on every read; auto-reloads if the file is newer. Two consequences:
- A pipeline that writes a resource between two consumer reads will produce different values for those reads. Eventually-consistent, but only at write granularity (atomic write means no torn reads).
- A consumer that reads a resource twice during the same agent invocation may see different values if a pipeline finished in between. Generally fine because reads are at agent boundaries, but agents that need a snapshot should capture once and reuse.

**No transactional cross-resource consistency.** If a pipeline writes `resource_A` and then `resource_B`, a consumer reading both might see new A and old B. Pipelines that need atomic multi-resource writes have to design around this.

## 9. Hand-edit etiquette

| Directory | Hand-edit? | Notes |
| --- | --- | --- |
| `user/`, `assistant/`, `instructions/` | Yes | Designed for direct editing. Edits persist. |
| `kg_derived/`, `dayflow_pipeline_outputs/`, `daily_insights_pipeline_outputs/`, `day_context/` | No | Will be clobbered on the next pipeline run. Edits to fix bad pipeline output don't survive. |
| `status/`, flat `resource_*.json` at root | No | Runtime overwrite every tick. |
| `templates/` | Yes (with care) | These are source files that get compiled. Edit the template, not the compiled output. |
| `pointers/`, `context/` | No | Indirection / pipeline-managed. |

If a pipeline-output looks wrong, the fix is upstream (in the pipeline or its prompts), not the file.

## 10. Why this design works

A few things that the resource layer enables, and that other AI architectures often don't have:

- **Determinism + reproducibility.** Agent input is a function of file contents. The same files plus the same prompt give the same output. Trivial to replay debug runs.
- **Inspectability.** Every piece of context an agent will see is `cat`-able. No hidden state.
- **User legibility.** Resources can be opened in a text editor and understood. The user can audit what the assistant believes about them.
- **User mutability where it makes sense.** `instructions/` is the user's tuning knob; the pipelines respect what's there.
- **Decoupling.** Pipelines and agents talk via stable resource names, not direct calls. Either side can be refactored as long as the resource contract holds.
- **Cheap consumption.** Agents don't pay to re-derive context. The pipeline pays once; everyone reads cheaply.
- **Diff-able evolution.** Resource files are git-trackable. You can see how the system's view of the user changes over time.

## 11. Known weak spots

- **Cross-resource consistency** is not guaranteed (§8). A multi-resource update is two write events, not one.
- **Atomic writes** are not universal across writers; some still use `write_text` directly.
- **The metadata envelope is inconsistent** (§4) — readers must know per-resource shape.
- **Per-resource contracts are scattered.** `RESOURCE_CONTRACTS.md` exists for dayflow pipeline outputs only; other categories rely on prose docs or grepping.
- **`templates/instructions/`** is a real subdirectory but its compilation flow isn't documented here — see ResourceManager and the agent instruction-loading paths if you need to extend it.

## 12. Cross-references

- `00_OVERVIEW.md` — top-level architecture.
- `01_AGENTS.md` — how `user_context_items` is declared per agent.
- `05_DAYFLOW.md` — the dayflow orchestrator (which *consumes* dayflow pipeline resources).
- `06_PIPELINES_AND_ROUTINES.md` — the pipeline runner that powers most resource writers.
- `16_BELIEF_ENGINE.md` — `resource_user_beliefs.json` lifecycle.
- `SECRETS_ACCOUNTS.md` — the account/secret model behind the `resource_accounts` dynamic-resource family.
- `SCOPE.md` — the four-layer scope model; resources are the allow/deny/lock half.
- `app/assistant/pipelines/dayflow/RESOURCE_CONTRACTS.md` — per-resource shape contracts for dayflow pipeline outputs.

## 13. Key files

| File | Purpose |
| --- | --- |
| `app/resource_manager/resource_manager.py` | The cache + load + reload + concurrent-write layer |
| `app/assistant/agent_runtime/services/resource_resolver.py` | Thin shim agents use to resolve a resource via DI |
| `app/assistant/agent_runtime/services/context_injector.py` | Walks `user_context_items` and resolves each |
| `app/assistant/agent_runtime/services/keyword_resource_index.py` | Keyword-triggered resource injection (`task_keyword_resources`) |
| `app/bootstrap.py` | Registers the dynamic-resource providers + locks (`resource_accounts`, `resource_email_accounts`, `resource_user_email`) |
| `app/assistant/env_registry/env_registry_service.py` | `render_accounts_for_scope` — provider behind the `resource_accounts` family |
| `app/assistant/utils/scope_gate.py` | The lock-side gate shared by resources and skills |
| `app/assistant/pipelines/dayflow/RESOURCE_CONTRACTS.md` | Shape contracts for dayflow pipeline outputs |
| `resources/` | The resource tree itself |
