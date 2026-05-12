# KG Pipeline — Architecture

The pipeline ingests chat from `unified_log_2026` and produces nodes + edges in the live KG (`kg_node_metadata`, `kg_edge_metadata`). Conceptually it does 7 things, in order:

1. **Prepare the chat** — filter to chat-eligible messages, resolve entity references ("she" → Katy).
2. **Segment into windows** — group messages into coherent conversation units.
3. **Critique + extract claims** — line-item veto on each window, then mine nodes/edges from the kept lines.
4. **Enrich** — attach metadata to each connected component: dates, aliases, importance, hash tags.
5. **Write proposals** — persist as the shadow KG (`claim_proposal*`).
6. **Promote + annotate** — review proposals, dedupe against the live KG, write rows. At promote-time also stamp TTL, canonicalize the claim sentence, and persist section tags for downstream projections (entity cards, wiki).
7. **Decay** — nightly pass that auto-closes State/Event nodes whose TTL expired.

The first 5 run as daemon threads inside `KGPipeline`. Block 6 is a separate scheduled routine. Block 7 lives in the KG maintenance pipeline.

**Drill into any block below** for what it does, agents involved, schemas, observability, and code pointers.

---

## Design principles

- **Explicit table-per-stage queues.** Each stage has its own queue table; rows move forward one stage at a time. No status enums on shared tables.
- **Simple worker functions.** Each stage is a stateless function: read from input bucket → process → write to output bucket. No bundled responsibilities.
- **Stages don't touch the live KG directly.** The pipeline ends at the proposal layer (`claim_proposal*`). The promoter (Block 6) is the sole writer to `kg_node_metadata` / `kg_edge_metadata`.
- **Canonical identity is `unified_log_id` end-to-end.** Drill from any KG node back to source: `node → proposal → evidence → unified_log_id → unified_log_2026` — single hop to raw text.

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
   │ Block 6: promote + annotate (separate routine)
   ▼
kg_node_metadata + kg_edge_metadata + kg_node_section_tag  ← live KG
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

2. **Entity resolution.** For each eligible message, the `entity_resolver` LLM agent (gpt-5.4-mini) rewrites pronouns and references against the day's already-resolved context. "I" → "(Jukka)", "she" → "(Katy)", "the kids" → "(Peter, Annika)". Original message stays unchanged in `unified_log_2026.message`; the resolved version is a separate row in `kg_resolved_message`.

3. **Per-day chronological batching.** Each batch is up to 10 unresolved messages plus up to 10 already-resolved messages from the same day for context.

**Input → Output.** `unified_log_2026` (filtered) → `kg_resolved_message` (keyed by `unified_log_id`).

**Code.**
- Step: `app/assistant/pipelines/kg_pipeline/steps/resolve_messages.py` (`ResolveMessagesStep`)
- Agent: `app/assistant/agents/entity_resolver/`

**Observability.** Queue depth: `SELECT COUNT(*) FROM unified_log_2026 u LEFT JOIN kg_resolved_message rm ON rm.unified_log_id = u.id WHERE rm.id IS NULL AND <eligibility>;`

**Schema highlights.** `kg_resolved_message`: `id`, `unified_log_id` (unique FK), `unified_timestamp`, `resolved_text`, `resolved_entities` (JSON), `resolver_version`, `resolved_at`.

---

## Block 2: Segment into windows

**Why it exists.** A "window" is the atomic unit of work for everything downstream. Without segmentation, the extractor would either chew on one message at a time (no anaphora context) or the whole day (way too much). The segmenter finds *coherent topic boundaries* so each window is one conversation about one thing.

**What it does.**

1. Reads up to one day's worth of new resolved messages.
2. Calls the `conversation_boundary` agent (gpt-5.4-mini) with the full span.
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

**Why it exists.** Most conversation isn't claim-bearing — jokes, banter, status updates, vague gestures. Sending it all to the (expensive) extractor wastes tokens and pollutes the KG with junk like "Jukka's grandpa's passengers screamed in terror" (a Bill Hicks joke that v1's whole-window critic accepted as factual). The critic is the cheap pre-filter; the extractor is the expensive mining step.

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
2. For each component, call `meta_data_add` (gpt-5.4-mini) to fill in: aliases, hash_tags, `start_date` / `end_date` (ISO + confidence + prose anchor), `valid_during`, `category`, `semantic_label`, `goal_status`, `confidence`, `importance`.
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

## Block 6: Promote + annotate (separate routine)

**Why it exists.** Promotion is the one place that decides whether a proposed node matches an existing node (dedupe), creates a new one (mint), or contradicts an existing claim (conflict). Same thing for edges. It's also where per-node annotations land that are too expensive for the in-pipeline stages — TTL estimation, sentence canonicalization, section tagging.

**Runs as a separate routine** (`proposal_promoter`), not as a `KGPipeline` daemon thread. Triggered by schedule and the `/kg-proposals/run-promoter?commit=1` UI button.

**What it does, per pending proposal:**

1. **Read snapshot + LLM canonicalize** (no write lock held). `_prepare_proposal_plan` figures out, for each proposed node, whether it matches an existing KG node (via semantic similarity + heuristics) or needs to be newly minted. Returns a structured plan.
2. **Short write transaction** (no LLM calls). `_evaluate_and_apply`:
   - For each node: `matched_existing` (reinforce the existing node + write evidence), `created_new` (mint a fresh `kg_node_metadata` row), or `held_needs_existing` / `skipped_locked`.
   - For each edge: same shape — match against an existing edge with the same `(source, target, predicate)` or mint a new one. Edge evidence row written per observation.
   - On any **durable contradiction** (single-target conflict on a `chronic` State, locked node mismatch, etc.) → rollback the whole proposal's apply within a SAVEPOINT and set status `contradicted`.
3. **Per-node enrichments at create time** (only when a fresh `kg_node_metadata` row is born):
   - **TTL estimation** for State/Event nodes via `state_ttl_estimator` agent. Stashes `{duration_class, estimated_duration_days, confidence, reasoning}` in `node.attributes["ttl"]`. Classes: ephemeral (≤1d), short_term (2–30d), medium_term (30–180d), long_term (180–730d), durable (null = never expires).
   - **Sentence canonicalization** for State/Event/Goal via `fact_canonicalizer`. Rewrites the extractor's sentence into present-tense canonical form before storing as `Node.original_sentence`.
4. **Status update.** `promoted` (success), `contradicted` (rollback), or stays `pending` if the apply held the proposal back. Status update commits with the transaction.

**Provenance is written to evidence, not denormalized on the node row.** Every node create/match writes one `kg_node_evidence` row; every edge create/match writes one `kg_edge_evidence` row. Carries `(window_id, source_table='unified_log_2026', source_id=unified_log_id, raw_text, derived_sentence, message_timestamp, merge_action)`. `merge_action` is `created` for fresh rows or `confirmed` for reinforcements. This is the canonical provenance store for the kg_node_viewer's "Evidence" panel, the `node_merger`'s context for match decisions, and forensics.

### After all proposals processed: batched section tagging

When `run_promoter` finishes its loop with `commit=True`, it collects the `resolved_node_id` of every newly-created node (State, Event, Goal, Property) and sends them to `section_tagging.tag_nodes_by_id` in batches of 20:

- `kg_node_section_tagger` (gpt-5.4-mini) classifies each node into **two namespaces in one call**: `card` (for entity cards) and `wiki` (for the wiki). Multi-tag allowed per namespace.
- Tags persist in `kg_node_section_tag` (one row per `(node_id, namespace, section_name)`).
- The downstream projection builders (`entity_cards_v2.builder`, `wiki_generator`) read tags from this table — they don't re-tag on every rebuild.
- Tagging failures are non-fatal — the promotion already committed; only the tag write would fail.

**Card sections:** `contact`, `connection_to_user`, `where_they_are`, `what_they_do`, `notes`, `current_connections`.

**Wiki sections:** `identity_and_background`, `education`, `career`, `marriage_and_family`, `residence`, `health`, `interests_and_hobbies`, `contact`, `relationships`.

Section vocabularies live in `app/assistant/kg/section_tagging.py` (`CARD_SECTION_VOCAB`, `WIKI_SECTION_VOCAB`).

**Code.**
- Promoter: `app/assistant/kg/proposal_promoter.py`
- Per-node enrichment agents: `app/assistant/agents/knowledge_graph_add/state_ttl_estimator/`, `.../fact_canonicalizer/`
- Section tagger: `app/assistant/agents/kg_node_section_tagger/`
- Tagging orchestration + persistence: `app/assistant/kg/section_tagging.py`
- Tag ORM model: `app/assistant/kg/db/knowledge_graph_db_sqlite.py` (`NodeSectionTag`)

### Proposal status lifecycle

```
        ┌─→ promoted     (auto-promoter success OR manual approve)
pending ┼─→ contradicted (durable single-target dup, locked node, etc.)
        ├─→ retracted    (user clicked Reject)
        └─→ abandoned    (writer stage hit a placeholder label that auto-rejects the group)
```

Only `pending` proposals are still movable; the other three are terminal.

---

## Block 7: Decay (separate maintenance routine)

**Why it exists.** Cards and the wiki project the NOW snapshot of the KG. Without decay, every State/Event ever observed stays open forever — "Jukka is taking the dogs out" from a year ago would still appear current. The TTL stamped at promotion gives each State/Event an expected lifetime; decay enforces it.

`step_state_decay.py` in `kg_maintenance_pipeline` runs nightly. For each active State/Event node:

- `expected_end = start_date + estimated_duration_days + GRACE_DAYS`
- If exceeded with no fresh re-observation → auto-close (`valid_to = expected_end`)
- Skips nodes with `confidence < LOW_CONFIDENCE_FLOOR` (stays open)
- Re-observation reopens / refreshes the era

Not part of `KGPipeline`. Listed here because TTL would be hanging metadata without it.

**Code.** `app/assistant/pipelines/kg_maintenance_pipeline/steps/step_state_decay.py`.

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
- **Promotion** — moving a proposal's nodes/edges into the live KG. Done by the promoter routine. State/Event nodes get TTL stamped at this moment, sentences canonicalized, and section tags persisted for downstream projections.
- **Section tag** — one row in `kg_node_section_tag`. Per-node membership in a card or wiki section, set once at promote-time and read by the projection builders.
- **Decay** — the maintenance pass that auto-closes State/Event eras when their TTL expires without re-observation.
