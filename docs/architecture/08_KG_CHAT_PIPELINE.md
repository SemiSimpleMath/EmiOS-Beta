# KG Chat Pipeline — Architecture

Audit date: 2026-04-25. This document is the authoritative description of the live ingest path that turns chat in `unified_log_2026` into rows in the live knowledge graph (`kg_node_metadata`, `kg_edge_metadata`).

It is grounded in the code at `app/assistant/pipelines/kg_chat_pipeline_parallel/` and the proposal layer at `app/assistant/kg/`. If anything in this doc disagrees with that code, the code wins — update this doc rather than the other way around.

## TL;DR

```
unified_log_2026                     ← raw chat lives here
   │ (1) Filter chat rows
   ▼
kg_chat_projection                   ← chat-only row; gains resolved_text in step 2
   │ (2) Resolve entities (per message — UPDATEs same row)
   ▼
kg_chat_projection.resolved_text     ← LLM-resolved text on the same row
   │ (3) Build windows over rows where resolved_text IS NOT NULL
   ▼
kg_chat_conversation_window          ← grouped batches of resolved messages
   │ (4) Extract facts
   ▼
kg_chat_extracted_node/_edge         ← in-window staging
   │ (4b, same step) Write proposals
   ▼
claim_proposals (+ children)         ← shadow KG queue
   │ (5) Promote (separate routine)
   ▼
kg_node_metadata, kg_edge_metadata   ← live graph
```

Steps 1–4 run nightly inside `KGChatPipelineParallel` (one daemon thread per step, status-coordinated). Step 5 is a separate scheduled routine (`kg_proposal_promoter`).

**Conceptual ordering vs. actual concurrency:** the diagram is a logical dataflow. In practice all four steps run as concurrent daemon threads — each loops continuously, claiming whatever work is available at its input. With the 2026-04-25 reorder, Build now naturally waits on Resolve because Build's input table (`kg_chat_resolved_message`) is fed by Resolve.

## Code map

| Concern | Path |
|---|---|
| Pipeline orchestrator | `app/assistant/pipelines/kg_chat_pipeline_parallel/pipeline.py` |
| Parallel runner | `app/assistant/pipelines/kg_chat_pipeline_parallel/parallel_runner.py` |
| Step 1 (Filter chat rows) | `steps/project_chat_from_unified_log.py` |
| Step 2 (Resolve entities) | `steps/resolve_messages_batched.py` |
| Step 3 (Build windows) | `steps/build_conversation_windows.py` |
| Step 4 (Extract facts) | `steps/extract_facts_from_windows.py` |
| Proposal writer (called inside step 4) | `app/assistant/kg/proposal_writer.py` |
| Proposal promoter (separate routine) | `app/assistant/kg/proposal_promoter.py` |
| Shared utility code | `app/assistant/pipelines/kg_shared/` (extraction_utils + merge_utils) |
| Subsystem flag (UI toggle) | gates `is_subsystem_enabled("kg_chat_pipeline")` in `pipeline.py:run()` |

## Window status state machine

The window's `status` column is the coordination primitive between Build (step 3) and Extract (step 4). Earlier steps don't touch window status — they operate on projections / resolved messages directly.

```
   pending                  ← created by Build (step 3) over already-resolved messages
       │
       ▼
   extracting (claim verb)  ← Extract claims pending windows
       │
       ├─────────────────┬─────────────────────────┬───────────────────┐
       ▼                 ▼                         ▼                   ▼
   extracted   skipped_unknown_referent     critic_rejected      extract_empty
       │       extract_error
       ▼
   (pipeline ends; proposals written; await promoter)
```

Notes:
- `pending` is set when `BuildConversationWindowsStep` creates a window (step 3). Because Build now sources from `kg_chat_resolved_message`, every projection in a `pending` window has already been resolved.
- `extracting` is the only in-progress verb in the live path. Stuck `extracting` = process killed mid-extract; reset to `pending` to retry.
- `parsing` and `enriching_metadata` are **historical fossils** from removed steps (the old per-window resolver and the legacy enrich/standardize/merge chain). Any window in those statuses today is debris from prior pipeline crashes; safe to reset to `pending`.

## Step 1 — Filter chat rows from unified log

**Class:** `ProjectChatFromUnifiedLogStep` (`project_chat_from_unified_log.py`)

**Reads:** `unified_log_2026`
**Writes:** `kg_chat_projection`, `kg_chat_projection_state` (watermark)

**What it does:** Filters `unified_log_2026` to chat-only rows, copies the relevant fields to `kg_chat_projection` so downstream stages have a clean, indexed, chat-only chronological table to walk.

**Inclusion rules** (from `_is_chat_eligible`):
- `source` ∈ {`chat`, `room_slack`, `room_sms`, `room_ui`}
- `role` ∈ {`user`, `assistant`}
- non-empty `message`
- `room_id` ∈ {`master_room`} (or empty)

**Mechanics:**
- Walks unified_log forward from the watermark stored in `kg_chat_projection_state`.
- `BATCH_SIZE = 500`, `MAX_BATCHES_PER_RUN = 50` (caps at 25,000 rows per run).
- Idempotent: skips projection_ids that already exist.

**No LLM calls.** Pure filter + copy.

## Step 2 — Resolve entities (per message, chronological)

**Class:** `ResolveMessagesBatchedStep` (`resolve_messages_batched.py`)

**Reads:** `kg_chat_projection` (rows where `resolved_text IS NULL`)
**Writes:** UPDATEs the same `kg_chat_projection` row in place — sets `resolved_text`, `resolver_version`, `resolved_at`. The original `message` column is **never touched**.

**What it does:** Walks projection rows chronologically, day-bounded, calling the `entity_resolver` agent in batches of 10 new messages with up to 10 already-resolved prior messages in the same day as read-only context. Each message is resolved exactly once and the result is stored on the same row. Downstream consumers (Build and Extract) read `resolved_text` from `kg_chat_projection` directly — no JOIN, no second table.

**Why per-message and not per-window:**
- Idempotent and persistent. A message resolved once never needs re-resolution, even if windowing changes.
- No double-cost when the same message lives in two overlapping windows.
- No drift — same message can't have two different `resolved_text` values from different window contexts.
- The `entity_resolver` agent's `previous_context` is supplied from prior same-day messages, not from window membership, giving better cross-conversation continuity.

**Batching contract** (from `_plan_next_batch`):
- `BATCH_NEW_SIZE = 10` new (unresolved) messages per LLM call.
- `BATCH_CONTEXT_SIZE = 10` already-resolved messages from earlier the same day, prepended as read-only context.
- A pre-resolved gap inside what would be a `new` batch terminates the batch early; next call resumes after the gap.
- Day boundary = local calendar day in the user's configured timezone. Context never bleeds across days.

**Per-message storage** (columns on `kg_chat_projection`):
- `id` (projection_id; FK target for window_item)
- `unified_log_id`, `unified_timestamp`, `role`, `speaker_name`
- `message` (verbatim original; **never modified**)
- `resolved_text` (entity-substituted text from the resolver; NULL until resolved)
- `resolved_entities` (reserved; unused today)
- `resolver_version` = `chronological_batched_v1`
- `resolved_at`

**Agent:** `knowledge_graph_add::entity_resolver` (gpt-5.4-mini). Same agent the old per-window resolver called — only the calling pattern changed.

**Auto-injected context:** the resolver agent's config has `entity_scan_keys: [text]` so the framework scans the input text for known entity labels and injects matching KG entity-card snippets via `entity_level_0`. This is how the resolver "knows" Morgan is Alex's father even when only a kinship word like "dad" appears in the text.

**Known prompt issue (audit finding):** the agent's `system.j2` contains real-data examples and counter-examples that the user has explicitly requested be removed. See `entity_resolver/prompts/system.j2` lines 22, 27, 48–50, 56–66, 71, 75. Pending rewrite.

## Step 3 — Build conversation windows

**Class:** `BuildConversationWindowsStep` (`build_conversation_windows.py`)

**Reads:** `kg_chat_projection` rows where `resolved_text IS NOT NULL` (i.e. messages already resolved by step 2)
**Writes:** `kg_chat_conversation_window`, `kg_chat_conversation_window_item`, `kg_chat_conversation_window_state` (watermark)

**What it does:** Groups consecutive resolved messages into conversation windows for the extractor to operate on. Each window is one batch of related messages, all of which have already been entity-resolved.

**Why Build's input is filtered by `resolved_text IS NOT NULL`:**
- Conceptually correct ordering — messages are resolved before they are grouped for extraction.
- Build naturally waits for Resolve to populate `resolved_text` before it can window the row; no need to enforce ordering with status flags.

**Splitting rules** (from `_should_split`):
- `MAX_GAP_MINUTES = 20` — hard split when consecutive messages are >20 minutes apart.
- `MAX_WINDOW_MESSAGES = 18` — hard cap.
- `MIN_WINDOW_MESSAGES = 12` — soft split allowed only after this floor.
- Soft split fires on `assistant → user` role transitions, **except** when the assistant's last line ended with `?` (Q/A glue: don't sever a question from its answer) or when the user's reply opens with a short agreement / pronoun phrase.

**Boundary reason** is recorded on each window: `time_gap`, `max_window_messages`, `assistant_turn_boundary`, `end_of_batch`.

**Window status starts at `pending`.**

**Historical fragmentation hazard:** prior to 2026-04-21 a different copy of this code (in the now-archived `kg_chat_pipeline/`) used `MIN_WINDOW_MESSAGES = 4`. Those old fragmented windows persist in the DB — re-windowing them requires deleting and rebuilding, not just re-running this step.

**No LLM calls.**

## Step 4 — Extract facts from windows



**Class:** `ExtractFactsFromWindowsStep` (`extract_facts_from_windows.py`)

**Reads:**
- `kg_chat_conversation_window` (claim by status)
- `kg_chat_conversation_window_item` (window membership)
- `kg_chat_projection` (joined by projection_id for `resolved_text`)

**Writes:**
- `kg_chat_extracted_node`, `kg_chat_extracted_edge` (in-window staging)
- `claim_proposals` + child rows via `proposal_writer` (the durable handoff)
- `kg_chat_conversation_window.status` (transition to terminal state)

**Claim function** (post-2026-04-25):
- Claim windows where `status = 'pending'` AND every projection in the window has `resolved_text IS NOT NULL`.
- Set claimed windows to `status = 'extracting'`.

**Per-window flow:**

1. **Load resolved text from `kg_chat_projection`** (helper: `_load_resolved_messages_for_window` joins window_item to projection by projection_id).
2. **`(unknown)` referent gate.** If any line contains the literal `(unknown)`, mark the window `skipped_unknown_referent` and continue. The resolver flags ambiguous references with `(unknown)`; trying to extract from those yields fabrication.
3. **Window critic prefilter.** Calls the `knowledge_graph_add::window_critic` agent. If the critic decides this window has nothing extractable (operational chatter, system messages, etc.), mark `critic_rejected` and continue.
4. **Fact extraction.** Calls `knowledge_graph_add::fact_extractor` (gpt-5.4) with the resolved user-side text. Returns nodes + edges as a graph plan.
5. **Per-node post-extract critic** (`NODE_CRITIC_*`). One LLM call reviews all extracted nodes against the original window text. Currently in **shadow mode** (`NODE_CRITIC_ENFORCE` defaults to `0`) — logs what it would drop but doesn't actually filter. Enabled by env var.
6. **Persist extracted_node/edge rows** to in-window staging tables (used by debugging/UI; not the canonical KG).
7. **Write `claim_proposals`** via `proposal_writer.write_proposals_for_window()`. This is the durable handoff to the live-KG path.
8. **Transition window status** to `extracted` (or empty/error).

**Terminal statuses** the extractor sets:
- `extracted` — successful extraction, proposals written
- `extract_empty` — no nodes/edges produced
- `extract_error` — exception during extraction
- `skipped_unknown_referent` — gate above
- `critic_rejected` — window critic rejected the whole window

**Agents called per window:** up to 3 LLM calls — window_critic, fact_extractor, node_critic_batch.

## Proposal write (sub-step inside step 4)

**Module:** `app/assistant/kg/proposal_writer.py`

Called from `ExtractFactsFromWindowsStep._persist_extraction_result`. Walks the extractor's output and writes a `claim_proposal` per connected subgraph:

1. **Filter placeholders.** Reject nodes whose label matches `unknown`, `unspecified`, `unnamed`, `unidentified`, or contains `(unknown)`.
2. **Enrich nodes.** Calls `knowledge_graph_add::meta_data_add` to fill in dates, aliases, confidence on the bare extractor output. Error-safe (returns nodes unchanged if it fails).
3. **Split into connected components.** Each subgraph becomes one `claim_proposal`.
4. **Persist** with proper UUID FKs:
   - `claim_proposals` (one per subgraph)
   - `claim_proposal_nodes` (children)
   - `claim_proposal_edges` (children)
   - `claim_proposal_evidence` (one row per component, links back to source window)
5. **Normalize predicates.** Each edge's predicate is run through `predicate_vocabulary.normalize_predicate()` to map aliases to canonical forms.

This is a write, not a promotion. Proposals sit in the queue waiting for the promoter routine to evaluate them.

## Step 5 — Promote proposals to live KG

**Module:** `app/assistant/kg/proposal_promoter.py`
**Routine:** `kg_proposal_promoter` in `configs/routines.json` (separate scheduled routine, NOT part of `KGChatPipelineParallel`)

**What it does:** Walks pending proposals one at a time. For each proposal, in its own transaction:

1. **Resolve or create each node.**
   - Entity-like (`Entity`, `Concept`, `Goal`, `Property`): match by `label + aliases`. No match → create new node.
   - Relationship-like (`State`, `Event`): match by `(participant_subset, valid_from, label)` after first resolving participants. No match → create new node.
2. **Reconcile each edge.**
   - Endpoints resolved via `ClaimProposalNode.resolved_node_id`.
   - De-duplicate against existing `kg_edge_metadata` on `(source, predicate, target)`.
   - **Lock check.** If source or target node has `locked_by_user_at` set AND the new edge would topologically conflict (e.g. same-predicate different-target on a unique relation), the proposal is held, not committed. A `kg_maintenance_finding` row is written for human review.
   - Otherwise: create edge with `created_from_proposal_id` linking back.
3. **Commit** the proposal as `promoted` (or `held` / `rejected`).

This is the only path that writes to `kg_node_metadata` and `kg_edge_metadata` with `source = "proposal_promoter"`.

**Authoritative claim:** as of the 2026-04-22 sunset, no other code path writes to the live KG with `source = "chat"`. The legacy enrich/standardize/merge chain that did so has been archived.

## Parallel runner mechanics

**Class:** `ParallelPipelineRunner` (`parallel_runner.py`)

- One daemon thread per step. Each thread loops: `step.run(ctx)` → sleep `poll_interval` → repeat.
- Coordination is entirely through window status. No inter-thread messaging.
- SQLite WAL + `busy_timeout` set in `app.models.base`.
- Each step uses fresh `get_session()` calls, never holds a session across LLM work.
- Worker loop catches `OperationalError("database is locked")` and backs off.
- Pipeline terminates when all steps report `max_idle_rounds` consecutive idle iterations.

The runner is invoked nightly via routine `kg_chat_pipeline_parallel` (in `configs/routines.json`) during the configured KG quiet hours window.

## Subsystem gating

`pipeline.py:run()` checks `is_subsystem_enabled("kg_chat_pipeline")` (note the legacy flag name — it gates the live parallel pipeline). Toggleable via `/dev/subsystems` UI.

## Maintenance pipeline (separate)

`app/assistant/pipelines/kg_maintenance_pipeline/` is a separate pipeline that runs roaming maintenance scans over the live KG (orphan detection, contradiction sweeps, lock-violation detection). It does not feed off the chat pipeline; it runs independently on its own schedule.

It still imports utility code from `app/assistant/pipelines/kg_shared/` (`apply_node_data_merger_result`, `merge_node_fields_into_existing`).

## Audit findings (2026-04-25 session)

These were uncovered while writing this doc and remain partially open:

| # | Finding | Status |
|---|---|---|
| 1 | Old `kg_chat_pipeline/` directory + 2 step copies + 2 routines registry entries → all referencing dead code | **FIXED** (archived; old routine removed; registry cleaned; utils relocated to `kg_shared/`) |
| 2 | `ResolveMessagesBatchedStep` built on 2026-04-20 but never wired into pipeline | **FIXED** (2026-04-25; wired in, replacing old per-window resolver) |
| 3 | Extractor read `window.resolved_text` (per-window resolver output) instead of canonical per-message rows | **FIXED** (2026-04-25; rewritten to read from `kg_chat_resolved_message`) |
| 4 | Six legacy step files (Parser, Critic, Enrich, Standardize, Merge, old Resolve) still on disk after 04-22 sunset | **FIXED** (2026-04-25; archived) |
| 4b | Build sourced from `kg_chat_projection`, ran in parallel with Resolve. Conceptually wrong ordering (windowing before resolution) and Build couldn't see resolved text. | **FIXED** (2026-04-25; Build now sources from `kg_chat_resolved_message`; pipeline step list reordered to Filter → Resolve → Build → Extract). |
| 5 | Old fragmented windows (4-msg) from pre-2026-04-21 code persist in DB | OPEN. Re-windowing requires per-day surgery (delete + rebuild). Tooling exists (`scratch_rewindow_aiti_isa.py` is one example) — needs a generalized version. |
| 6 | `entity_resolver` system.j2 has real-data examples and counter-examples | OPEN. Pending rewrite to rules-only style per user's prompt-design rules. |
| 7 | `extract_facts_from_windows.py` has long inline NODE_CRITIC prompt with hardcoded "Alex" name and many counter-examples | **RESOLVED** (2026-04-25; removed entirely. Was running in shadow mode anyway. The `window_critic` agent + user-editable `resource_kg_interests.json` provides sufficient gating; some residual noise leaks through, accepted per design call. If a sentence- or node-level filter is needed later, build a proper `triager` agent — see triager design discussion in session log). |
| 8 | `kg_chat_resolved_message_state` watermark table exists but has 0 rows; the resolver currently uses `_find_next_unresolved_day()` query instead. Cheap query, but inconsistent with the table's existence. | OPEN. Either wire watermark tracking or drop the unused table. |
| 9 | Stuck windows in `-ing` statuses from prior crashes (354 reset on 2026-04-25) | RESOLVED for current backlog. Long-term: add a periodic reclaim sweep that resets in-progress statuses older than ~10 minutes. |
| 10 | `subsystems.yaml` flag is named `kg_chat_pipeline` (legacy name); the new code is `kg_chat_pipeline_parallel`. | COSMETIC. Renaming the flag would touch the UI; deferred. |
| 11 | `kg_chat_conversation_window.resolved_text` column was write-dead but still on schema | **FIXED** (2026-04-25; column dropped from model and DB; 1,938 rows of stale JSON cleared). |
| 12 | Two 1:1 tables: `kg_chat_projection` and `kg_chat_resolved_message`. Projection is the chat-only filter; resolved_message is the substantive output. | **FIXED** (2026-04-25; collapsed: added `resolved_text`/`resolver_version`/`resolved_at`/`resolved_entities` columns to `kg_chat_projection`, migrated 4,121 rows in place, dropped `kg_chat_resolved_message` + `_state` tables. Resolver UPDATEs the projection row; `message` column is never touched, preserving provenance via `unified_log_id` + `message`. JSON snapshot at `scratch_backup_kg_chat_resolved_message.json` for rollback safety). |

## Glossary

- **Projection** — a row in `kg_chat_projection`. One per chat message, copied verbatim from `unified_log_2026` (chat-only filter). Holds both the original `message` (immutable) and the resolved version `resolved_text` (NULL until step 2 runs). The canonical "what was said, optionally with entities named."
- **Window** — a row in `kg_chat_conversation_window`, grouping consecutive resolved projections. The unit of extraction.
- **Proposal** — a row in `claim_proposals`, the shadow-KG queue item awaiting promotion. One per connected subgraph from one window's extraction.
- **Promotion** — the act of moving a proposal's nodes/edges into the live KG (`kg_node_metadata`/`kg_edge_metadata`). Done by the promoter routine, not the pipeline.
