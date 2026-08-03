# KG Pipeline — Architecture

The pipeline ingests chat from `unified_log_2026` and produces nodes + edges in the live KG (`kg_node_metadata`, `kg_edge_metadata`). Conceptually it does 7 things, in order:

1. **Prepare the chat** — filter to chat-eligible messages, resolve entity references ("she" → the user's partner).
2. **Segment into windows** — group messages into coherent conversation units.
3. **Critique + extract claims** — line-item veto on each window, then mine nodes/edges from the kept lines.
4. **Enrich** — attach metadata to each connected component: dates, aliases, importance, hash tags.
5. **Write proposals** — persist as the shadow KG (`claim_proposal*`).
6. **Promote** — review proposals, dedupe/merge against the live KG (LLM arbiter for ambiguous matches), write rows. At promote-time also stamp TTL, canonicalize the claim sentence, and embed the node into ChromaDB. Section tagging is *not* done here — it moved to the importance-rater routine (see Block 6).
7. **Decay** — nightly pass that auto-closes State/Event nodes whose TTL expired.

The first 5 run as daemon threads inside `KGPipeline`. Block 6 is a separate scheduled routine. Block 7 lives in the KG maintenance pipeline.

**Drill into any block below** for what it does, agents involved, schemas, observability, and code pointers.

---

## Design principles

- **Explicit table-per-stage queues.** Each stage has its own queue table; rows move forward one stage at a time. No status enums on shared tables.
- **Simple worker functions.** Each stage is a stateless function: read from input bucket → process → write to output bucket. No bundled responsibilities.
- **Stages don't touch the live KG directly.** The pipeline ends at the proposal layer (`claim_proposal*`). The promoter (Block 6) is the sole writer to `kg_node_metadata` / `kg_edge_metadata`.
- **Canonical identity is `unified_log_id` end-to-end.** Drill from any KG node back to source: `node → proposal → evidence → unified_log_id → unified_log_2026` — single hop to raw text.

> **Agent reads go through the `ask_kg` tool.** This doc is the *write* path. The old `kg_query_manager` / `kg_team_manager` were retired; the agent-facing KG read path is now the `ask_kg` tool (`app/assistant/lib/tools/ask_kg/`, wrapping `KnowledgeGraphSearch`) — a RAG Q&A with a global Chroma-similarity mode and a node-anchored BFS mode, returning a cited answer.

The worker contract is uniform across all 5 in-pipeline stages:

```python
def claim_next() -> Optional[InputUnit]:
    """Atomically claim one input row that has no matching output row. None if bucket is empty."""

def process(input_unit) -> OutputUnit:
    """Stateless work — agents, tools, anything. No DB session held during LLM calls."""

def commit(output_row):
    """Single INSERT into output bucket."""
```

The runner spawns one daemon thread per stage and they advance independently.

---

## Big picture flow

```
unified_log_2026                  ← raw events (immutable source of truth)
   │ Block 1: prepare the chat
   ▼
kg_resolved_message               ← bucket: ready to segment
   │ Block 2: segment
   ▼
kg_window  (+ kg_window_message)  ← bucket: ready to critique+extract
   │ Block 3: critique + extract
   ▼
kg_window_extraction              ← bucket: ready to enrich
   │ Block 4: enrich
   ▼
kg_window_enrichment              ← bucket: ready to write proposals (1:1 with future proposal)
   │ Block 5: write proposals (deterministic)
   ▼
claim_proposal*                   ← shadow KG: ready to promote
   │ Block 6: promote (separate routine; three-phase, no write-lock across LLM)
   ▼
kg_node_metadata + kg_edge_metadata  ← live KG (+ Chroma embed-at-write)
   │ Block 7: decay (nightly maintenance)
   ▼
                                  ← auto-closed State/Event nodes whose TTL expired
```

---

## Block 1: Prepare the chat

**Why it exists.** Raw `unified_log_2026` carries everything (chat, system events, dayflow items). The pipeline only cares about chat between user and assistant. Plus, raw chat is full of pronouns and shorthand — "she texted me" is uninterpretable without entity binding. This block does both jobs.

**What it does.**

1. **Eligibility filter** (built into the resolver's input query — no separate filter stage):
   - `source` ∈ {`chat`, `room_slack`, `room_sms`, `room_ui`, `kg_maintenance_resolution`}
   - `role` ∈ {`user`, `assistant`}, non-empty message
   - `room_id` ∈ {`master_room`} or null

2. **Entity resolution.** For each eligible message, the `entity_resolver` LLM agent (gpt-5.6-luna) rewrites pronouns and references against the day's already-resolved context. "I" → "(the user)", "she" → "(the user's partner)", "the kids" → "(a family member, a family member)". Original message stays unchanged in `unified_log_2026.message`; the resolved version is a separate row in `kg_resolved_message`.

3. **Per-day chronological batching.** Each batch is up to 10 unresolved messages plus up to 10 already-resolved messages from the same day for context.

**Input → Output.** `unified_log_2026` (filtered) → `kg_resolved_message` (keyed by `unified_log_id`).

**Code.**
- Step: `app/assistant/pipelines/kg_pipeline/steps/resolve_messages.py` (`ResolveMessagesStep`)
- Agent: `app/assistant/agents/knowledge_graph_add/entity_resolver/`

**Observability.** Queue depth: `SELECT COUNT(*) FROM unified_log_2026 u LEFT JOIN kg_resolved_message rm ON rm.unified_log_id = u.id WHERE rm.id IS NULL AND <eligibility>;`

**Schema highlights.** `kg_resolved_message`: `id`, `unified_log_id` (unique FK), `unified_timestamp`, `resolved_text`, `resolved_entities` (JSON), `resolver_version`, `resolved_at`.

---

## Block 2: Segment into windows

**Why it exists.** A "window" is the atomic unit of work for everything downstream. Without segmentation, the extractor would either chew on one message at a time (no anaphora context) or the whole day (way too much). The segmenter finds *coherent topic boundaries* so each window is one conversation about one thing.

**What it does.**

1. Reads up to one day's worth of new resolved messages.
2. Calls the `conversation_boundary` agent (gpt-5.6-luna) with the full span.
3. Agent emits windows — each one spanning N consecutive messages — with a summary and a reason for each boundary.
4. **Gating rule** prevents fragmenting live conversations: a day's tail won't be segmented until either (a) at least 6 contiguous resolved-but-unsegmented messages have accumulated, OR (b) the resolver has caught up to the most recent eligible message for the day.
5. **Cross-window links.** When a new window continues a topic from an older window (the A-B-A pattern), the agent emits `related_previous_window_ids` so downstream consumers can rejoin them on demand. The worker never modifies older windows.

**Input → Output.** `kg_resolved_message` → `kg_window` + `kg_window_message` (join table).

**Code.**
- Step: `app/assistant/pipelines/kg_pipeline/steps/segment_messages.py` (`SegmentMessagesStep`)
- Agent: `app/assistant/agents/knowledge_graph_add/conversation_boundary/`

**Schema highlights.** `kg_window` carries `start_unified_log_id`, `end_unified_log_id`, timestamps, `summary`, `standalone`, `related_previous_window_ids`. `kg_window_message` is `(window_id, unified_log_id, item_order)` with `unified_log_id` unique (one window per message).

---

## Block 3: Critique + extract claims

**Why it exists.** Most conversation isn't claim-bearing — jokes, banter, status updates, vague gestures. Sending it all to the (expensive) extractor wastes tokens and pollutes the KG with junk like "the user's grandpa's passengers screamed in terror" (a Bill Hicks joke that v1's whole-window critic accepted as factual). The critic is the cheap pre-filter; the extractor is the expensive mining step.

**What it does.**

1. **`window_critic_v2`** (gpt-5.4 smart, see `app/assistant/agents/knowledge_graph_add/window_critic_v2/`) runs on the window's user messages with the assistant context visible-but-not-extractable. For each user message it decides claim-bearing or not. Output: `extractable_lines` — the indices of user messages worth mining. Empty list = reject the window.
2. If the critic returns no extractable lines → write a `kg_window_extraction` row with `verdict='rejected'`, done.
3. Otherwise → `fact_extractor` (powerful tier) runs over the SAME window, but `user_text` is narrowed to only the kept lines. The `full_conversation` (all messages including assistant) stays visible for anaphora / discourse coherence. Output: structured nodes + edges with original_sentence + temporal context.
4. Write `kg_window_extraction` row with `verdict='extracted'` + nodes/edges JSON.

The critic is calibrated by a single resource file (`resources/user/resource_kg_interests.json`) — the description + categories there are the one knob that decides what counts as "claim-bearing." See `feedback_no_truncated_tool_results.md` etc. for why we keep all critic data flowing into the extractor without slicing.

**Input → Output.** `kg_window` → `kg_window_extraction` (one row per window; `verdict` ∈ {extracted, rejected, skipped, error}).

**Flushing.** Need to re-extract everything with a tuned critic / extractor? `TRUNCATE kg_window_extraction` and the next worker run reprocesses all windows.

**Code.**
- Step: `app/assistant/pipelines/kg_pipeline/steps/critique_and_extract.py` (`CritiqueAndExtractStep`)
- Agents: `app/assistant/agents/knowledge_graph_add/window_critic_v2/`, `.../fact_extractor/`

**Schema highlights.** `kg_window_extraction`: `verdict`, `verdict_reason`, `nodes` (JSON), `edges` (JSON), `extractor_version`, `critic_version`.

---

## Block 4: Enrich

**Why it exists.** The extractor produces minimal nodes (label, type, sentence). Downstream consumers need more: when did this happen, what category, who else cares about it, alternate names. Enrichment is one LLM pass that fills all of those in at once, per connected component.

**What it does.**

1. Split the extraction's nodes/edges into **connected components** (deterministic — disjoint subgraphs become separate proposals).
2. For each component, call `meta_data_add` (gpt-5.6-luna) to fill in: aliases, hash_tags, `start_date` / `end_date` (ISO + confidence + prose anchor), `valid_during`, `category`, `semantic_label`, `goal_status`, `confidence`, `importance`.
3. Write one `kg_window_enrichment` row per component, carrying the enriched nodes + edges.

After this block, each enrichment row maps **1:1 to a future claim_proposal**.

**Input → Output.** `kg_window_extraction` (extracted only) → `kg_window_enrichment` (one row per connected component).

**Code.**
- Step: `app/assistant/pipelines/kg_pipeline/steps/enrich_extraction.py` (`EnrichExtractionStep`)
- Agent: `app/assistant/agents/knowledge_graph_add/meta_data_add/`

**Schema highlights.** `kg_window_enrichment`: `(window_extraction_id, component_index)` unique, `enriched_nodes` JSON, `enriched_edges` JSON, `enricher_version`.

---

## Block 5: Write proposals (the shadow KG)

**Why it exists.** Promoting straight into the live KG would mean every extraction commits immediately, with no review, no dedup pass, and no place for the operator to intervene. The shadow KG (`claim_proposal*`) is a parallel staging area where every proposed change waits until the promoter (Block 6) evaluates it.

**What it does.** Pure deterministic DB writes — no LLM calls. For each enrichment row:

1. Insert one `claim_proposal` row (the group header — status `pending` by default).
2. Insert one `claim_proposal_node` per node in the component.
3. Insert one `claim_proposal_edge` per edge.
4. Insert one `claim_proposal_evidence` row pointing back to the enrichment + the source window (`window_id`, `enrichment_id`, `unified_log_id`, raw text).

**Input → Output.** `kg_window_enrichment` → `claim_proposal` + `claim_proposal_node` + `claim_proposal_edge` + `claim_proposal_evidence`.

**Code.** Step: `app/assistant/pipelines/kg_pipeline/steps/write_proposals.py` (`WriteProposalsStep`).

**Admin review surface.** See "Manual review" in the appendix — most proposals get auto-promoted, but the `/kg-proposals/` UI lets you triage any that the promoter leaves `pending`.

---

## Block 6: Promote (separate routine)

**Why it exists.** Promotion is the one place that decides whether a proposed node matches an existing node (dedupe/merge), creates a new one (mint), or has to be held back because it conflicts with an existing claim. Same for edges. It's also where per-node annotations land that are too expensive for the in-pipeline stages — TTL estimation and sentence canonicalization.

**Runs as a separate routine.** `proposal_promoter` is the *actor name*; the entry point is `run_promoter(*, limit=100, commit=False)` — **dry-run by default**, `commit=True` writes. Triggered by schedule and the `/kg-proposals/run-promoter?commit=1` UI button. Gated by the `kg_proposal_promoter` subsystem flag (toggle at `/dev/subsystems`); disabled → early-return `{status: "skipped"}`.

### Three-phase, no write lock held across an LLM call

This is the structural fix for the 2026-05-02 "database is locked" cascade. Each proposal runs three phases (`_prepare_proposal_plan` → `_evaluate_and_apply`):

1. **READ** (`read_session`) — snapshot the proposal + its nodes/edges + all candidate context (state/event candidates, entity confirm-lists, participant fingerprints, window text). Session closes before any LLM call. Reads don't block writers under WAL.
2. **LLM** (no session) — run `state_ttl_estimator`, `fact_canonicalizer`, and `node_merger` (only when there are candidates). Results land in a `_PromoterPlan` keyed by proposal-node id.
3. **APPLY** (`db_manager.transaction`, ≈ sub-millisecond) — `_evaluate_and_apply` makes **zero** LLM calls; every match/create decision is read from the plan. The writer slot is held only for deterministic SQL.

### Node resolution tiers (READ + LLM phases)

For entity-like nodes (Entity/Concept/Goal), `_resolve_entity_like` returns a *tier*, and each tier is treated differently:

- **`disambiguation`** — a `Disambiguation` node exists at this label (known-ambiguous referent). Bind to the Disambiguation node itself, beating any exact hit; edges land there until the maintenance investigator re-points them. See `app/assistant/kg/disambiguation.py`.
- **`label`** — exact case-insensitive label match. Closed-form identity, *except* a same-label bind to a well-connected person (`category == person`, ≥ `_PERSON_CONFIRM_MIN_EDGES = 5` edges) is the "two-namesakes" trap → routed through `node_merger` confirmation instead of binding silently.
- **`mention_map`** — a form `node_merger` previously CONFIRMED, recorded durably and self-revoking on ambiguity (`app/assistant/kg/mention_map.py`). Closed-form.
- **`alias`** — NOT closed-form (aliases accrete from bound mention labels, so a generic label like "House" can capture every house). Routed through `node_merger` with the alias hit + semantic near-matches as candidates.
- **Semantic tier** — no exact hit → `_semantic_entity_candidates` queries Chroma (label, context, and identity-sentence collections; cosine ≥ `_ENTITY_SEMANTIC_SIM_THRESHOLD = 0.88`) for near-matches and hands them to `node_merger`. Catches "the user's mom" vs "…mother" before minting a twin.

`Property` nodes resolve via `_resolve_property` (subject-scoped: label AND a shared subject edge — generic labels like "Date of Birth" otherwise become global magnet nodes).

State/Event nodes resolve by **participant fingerprint**: candidates are scored by Jaccard participant overlap, then filtered (`_filter_candidates_by_min_jaccard ≥ 0.5`, hub-weighted inverse-degree overlap, universal time-frame exclusion, Event start-date tolerance, label-or-embedding-similarity), and the top ≤5 go to `node_merger` (`knowledge_graph_add::node_merger`) as the LLM arbiter — deterministic proposes, LLM decides.

**Verdict-distinct injection (now implemented; formerly "planned").** Before the merger call, `load_distinct_verdicts_among` (`kg_maintenance.verdict_store`) is consulted: when the maintenance loop has already ruled two candidates DISTINCT from each other, that memo is injected into the candidates so the merger can't silently re-bind a new observation into the wrong twin and undo maintenance work.

### APPLY phase

`_evaluate_and_apply` writes nodes (entity-like first, then relationship-like), then edges:

- **Node match** → `matched_existing`: `_refresh_on_reobservation` bumps observation columns (monotonic `last_observed`, `observation_count`, gentle confidence, Goal `last_pursued_at`/revive) **without bumping `updated_at`** for pure bookkeeping (an explicit self-ref UPDATE avoids cascading wiki/card refreshes); writes a `kg_node_evidence` row (`merge_action="confirmed"`). Confirmed binds from a merger call mint a mention-map entry.
- **Node create** → `created_new`: a **mint/match race guard** re-checks `_resolve_entity_like` inside the write txn (a concurrent run may have minted it); on hit it converts to match. Otherwise `_create_kg_node_from_proposal` mints the row, promotes first-class columns out of `attributes_json` (`semantic_label`, `confidence`, `valid_during`, `goal_status`, observation fields), embeds the node into Chroma **at write** (`_embed_and_store_node` — context vector; the label/identity vectors are owned by the ORM sync chokepoint), then `_mint_disambiguation_on_label_collision` mints a Disambiguation marker if the new label now collides with a same-type node.
- **Edge** → match an existing edge via `_existing_kg_edge` (predicate *spelling class* + symmetric/`bidirectional` reverse-triple dedup) and append evidence, or create a fresh `Edge`. **Pod-URI endpoints** (`datapod:<kind>:<id>`) bypass node resolution and are gated by `is_kg_admissible` (`pod_kind_registry`) — there is **no `kg_node` mirror** for pods; the FK was dropped, this admission check is the replacement.

`_NodeOutcome.action` vocabulary: **`matched_existing` | `created_new` | `skipped_locked` | `skipped_conflict`** (there is no `held_needs_existing`).

### Durable single-target conflict handling

A single-target predicate (`is_spouse_in`, `works_for`, `born_in`, `has_birthday`, `has_nationality`) already pointing from a source to a *different* target is a durable conflict (`_is_durable_conflict`). The promoter **no longer auto-`contradicted`s the group**. `_classify_durable_conflict` does temporal triage on the era node (the State/Event endpoint of the conflicting edge):

- **`closed_era`** — the existing era has an `end_date`. Sequential facts coexist (remarriage, new job) → fall through and **create the successor edge**.
- **`succession`** — the existing era is open but the proposal's dates postdate its start. Conservative path: set `final_status = "held"`, **roll back this proposal's writes, leave it `pending`** (re-evaluates every run, promotes once the era is closed), and raise a `single_target_succession` finding routing the close-the-old-era decision to the user.
- **`same_era`** — undatable/overlapping double assertion. **Skip just the conflicting edge** (`skipped_conflict`, or `skipped_locked` if the source node is user-locked), let the rest of the group apply, and raise a `single_target_conflict` finding.

`SAVEPOINT` + `final_status = "contradicted"` is now reserved for the **placeholder-label rejection** (the extractor fabricated an "Unknown X"/"(unknown)" node — `_is_placeholder_label`); that path rolls the whole apply back and sets `contradicted`.

### Status the promoter writes

```
        ┌─→ promoted     (apply succeeded; commit=True)
pending ┼─→ contradicted (placeholder-label group rejection; SAVEPOINT rollback)
        ├─→ held → stays pending  (succession candidate — rolled back, re-tried next run)
        └─→ pending (unchanged)   (dry-run, or same_era edge skip)
```

The promoter writes only `promoted` and `contradicted`, and otherwise leaves the proposal `pending`. `retracted` (user Reject) and `abandoned` (writer-stage placeholder reject) are **written elsewhere**, not by the promoter. Findings (`single_target_succession`, `single_target_conflict`) are routed via `kg_maintenance.store.upsert_finding` **after** the write transaction commits (SQLite is single-writer) and only when `commit=True`.

**Provenance is written to evidence, not denormalized on the node row.** Every node create/match writes one `kg_node_evidence` row; every edge create/match writes one `kg_edge_evidence` row. Carries `(window_id, source_text, derived_sentence, message_timestamp, merge_action)` (the legacy `source_table`/`source_id` pair is now NULL — it misattributed in multi-topic windows; source context comes from walking `window_id → kg_window_message → unified_log_2026`). `merge_action` is `created` for fresh rows or `confirmed` for reinforcements. This is the canonical provenance store for the kg_node_viewer's "Evidence" panel, the `node_merger`'s match context, and forensics.

### Section tagging is no longer here

The promoter does **not** tag nodes. Per the deferred-dependency chain `promote (NULL importance) → rate → tag → card/wiki dirty-sweep`, section tagging moved into the importance-rater routine: `_lazy_kg_importance_rater` (`app/assistant/routine_manager/routine_functions.py`, registered as `kg_importance_rater`) calls `section_tagging.backfill_untagged_nodes` **after** rating, so the tagger sees real importance values. `kg_node_section_tagger` (gpt-5.6-luna) classifies each untagged State/Event/Goal/Property into the `card` and `wiki` namespaces in one call; tags persist in `kg_node_section_tag` and the projection builders read them rather than re-tagging. Section vocabularies live in `app/assistant/kg/section_tagging.py` (`CARD_SECTION_VOCAB`, `WIKI_SECTION_VOCAB`).

**Code.**
- Promoter: `app/assistant/kg/proposal_promoter.py` (`run_promoter`, `_prepare_proposal_plan`, `_evaluate_and_apply`, `_classify_durable_conflict`)
- Per-node enrichment agents: `app/assistant/agents/knowledge_graph_add/state_ttl_estimator/`, `.../fact_canonicalizer/`, `.../node_merger/`
- Disambiguation / mention-map: `app/assistant/kg/disambiguation.py`, `app/assistant/kg/mention_map.py`
- Section tagging (now driven by the rater routine): `app/assistant/kg/section_tagging.py`, `app/assistant/agents/kg_node_section_tagger/`, tag ORM `NodeSectionTag` in `app/assistant/kg/db/knowledge_graph_db_sqlite.py`

---

## Block 7: Decay (separate maintenance routine)

**Why it exists.** Cards and the wiki project the NOW snapshot of the KG. Without decay, every State/Event ever observed stays open forever — "the user is taking the dogs out" from a year ago would still appear current. The TTL stamped at promotion gives each State/Event an expected lifetime; decay enforces it.

`step_state_decay.py` in `kg_maintenance_pipeline` runs nightly. For each active State/Event node:

- `expected_end = start_date + estimated_duration_days + GRACE_DAYS`
- If exceeded with no fresh re-observation → auto-close (`valid_to = expected_end`)
- Skips nodes with `confidence < LOW_CONFIDENCE_FLOOR` (stays open)
- Re-observation reopens / refreshes the era

Not part of `KGPipeline`. Listed here because TTL would be hanging metadata without it.

**Code.** `app/assistant/pipelines/kg_maintenance_pipeline/step_state_decay.py` (no `steps/` subdir).

---

## Pipeline class + step contracts

```
app/assistant/pipelines/kg_pipeline/
├── pipeline.py              # KGPipeline class
├── runner.py                # bucket-aware runner (5 daemon threads)
├── steps/
│   ├── __init__.py
│   ├── resolve_messages.py       # Block 1
│   ├── segment_messages.py       # Block 2
│   ├── critique_and_extract.py   # Block 3
│   ├── enrich_extraction.py      # Block 4
│   └── write_proposals.py        # Block 5
└── README.md
```

Each step exports a class with two methods:
- `claim_next() -> Optional[InputUnit]`
- `process(input_unit) -> OutputUnit`

The runner spawns one daemon thread per step; each thread loops `claim_next → process → commit` on its bucket.

---

## Manual review (the `/kg-proposals/` admin UI)

Most proposals get promoted (or marked contradicted) automatically by Block 6. A few don't — typically when the promoter can't decide whether a proposed node should match an existing KG node (low-confidence semantic match, missing participants on a State/Event, etc.). Those stay `pending` and accumulate. The admin UI is how you triage them.

**Routes** (`app/routes/kg_proposals.py`):

| Method + path | What it does |
|---|---|
| `GET  /kg-proposals/` | List view, filterable by status. Trigger buttons for run-pipeline and run-promoter. |
| `GET  /kg-proposals/<id>` | Detail page: graph viz of the proposed group, evidence trail, action buttons. |
| `POST /kg-proposals/run-pipeline` | Synchronous trigger of the `kg_pipeline` routine. Capped per cycle. |
| `POST /kg-proposals/run-promoter` | Run the promoter (Block 6). Dry-run by default; `?commit=1` applies. |
| `POST /kg-proposals/<id>/approve` | Manual force-promote. Runs the same `_evaluate_and_apply` evaluator on one proposal in a savepoint. |
| `POST /kg-proposals/<id>/reject` | Retract — sets `status='retracted'`. Use when you can see a proposal is wrong. |
| `GET  /kg-proposals/<id>.md` | Markdown export — useful for sharing or archiving. |

The "shadow KG" metaphor: `kg_node_metadata` / `kg_edge_metadata` are the live graph; `claim_proposal*` is a parallel staging area where every proposed change waits for evaluation. The promoter is the bridge.

---

## Properties this architecture gives us

- **Observable.** `SELECT COUNT(*) FROM kg_window_extraction WHERE verdict = 'rejected'` is a real metric. So is queue depth at every block.
- **Replayable.** Flush block N's output table → next pipeline run reprocesses everything from block N onward. No surgery needed.
- **Debuggable.** "Where is window X stuck?" is one row's existence across N tables.
- **Decoupled.** Block 4 doesn't know that block 2 is still working. Each block runs at its own pace.
- **Easy to add new sources.** Email, docs, etc. enter at Block 1 by extending the eligibility filter. Everything downstream is source-agnostic.

---

## Schema migration history

The pre-2026-04-22 chain stored chat windows under `kg_chat_projection` + `kg_chat_conversation_window` + `kg_chat_conversation_window_item` + `kg_chat_parsed_sentence`. Those four tables were retired and migrated into the unified `kg_window` + `kg_window_message` + `kg_resolved_message` schema (see `scratch_legacy_window_migration_*.py` for the conversion rules — pure SQL, no LLM cost). After the migration all `kg_edge_metadata.window_id` and `kg_node_evidence.window_id` references resolve through the current schema; the legacy tables and their fallback lookup paths in `page_writer`, `kg_node_viewer`, `kg_proposals`, `proposal_writer`, and `proposal_promoter` were all removed in commit `80a8b0d2`. There is no longer a "provenance archive" — there's just one schema.

Denormalized provenance columns on `kg_node_metadata` (`window_id`, `original_message_id`, `sentence_id`) were dropped 2026-05-04. Provenance lives entirely in `kg_node_evidence` / `kg_edge_evidence` (per-observation). `original_sentence` stays on the node row as the canonical claim sentence.

`kg_node_section_tag` was added 2026-05-11 to persist section memberships once per node (at promote-time) instead of recomputing them on every card / wiki rebuild.

---

## Open questions / future work

- **Multi-worker-per-stage** when the extractor becomes the throughput bottleneck.
- **Cross-window link consumption** — proposal_writer or wiki layer could use `related_previous_window_ids` to merge re-joined topics.
- **Currency validation** at promote-time for card-bound facts — distinguish "I owned a red Corvette in 1995" from "I own a red Corvette now" before card sections accept the fact.
- **Auto-rewrite of dirty card sections** — section-scoped refresh triggered by a tag-set-hash diff during the nightly sweep (and on manual regenerate). Currency check is part of this path.
- **Resolver `(unknown)` hallucination** — root cause prompt fix for cases like `Uni (university highschool) (unknown)`.
- **Subsystem flag rename** (`kg_chat_pipeline` → `kg_pipeline`) — UI surface, deferred.

---

## Glossary

- **Message** — one row in `unified_log_2026`. The immutable source-of-truth log (also carries non-chat events; the resolver filters to chat-eligible ones).
- **Resolved message** — one row in `kg_resolved_message`. The entity-substituted version of one chat-eligible message, keyed by `unified_log_id`.
- **Window** — one row in `kg_window`. A coherent conversation unit (one topic). The atomic unit of work for blocks 2 onward.
- **Extraction** — one row in `kg_window_extraction`. The result of running critic+extractor on one window. Holds raw nodes/edges before component-split.
- **Enrichment** — one row in `kg_window_enrichment`. One connected component from an extraction, with metadata (dates, aliases, importance) applied. Maps 1:1 to a future proposal.
- **Proposal** — one row in `claim_proposal`. One connected subgraph ready to be evaluated for KG promotion.
- **Promotion** — moving a proposal's nodes/edges into the live KG. Done by the promoter routine (`run_promoter`). State/Event nodes get TTL stamped at this moment, sentences canonicalized, and the node embedded into Chroma. (Section tagging is NOT part of promotion — it runs later in the importance-rater routine.)
- **Section tag** — one row in `kg_node_section_tag`. Per-node membership in a card or wiki section, written by the importance-rater routine (`backfill_untagged_nodes`) after rating, and read by the projection builders.
- **Decay** — the maintenance pass that auto-closes State/Event eras when their TTL expires without re-observation.
