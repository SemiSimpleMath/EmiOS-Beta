# KG Pipeline — Architecture

This is the architecture for the live KG ingest pipeline that turns chat in `unified_log_2026` into rows in the live knowledge graph (`kg_node_metadata`, `kg_edge_metadata`) via the proposal layer (`claim_proposal*`).

## Design principles

- **Explicit table-per-stage queues.** Each stage has its own queue table; rows move forward one stage at a time. No status enums on shared tables, no implicit coordination.
- **Simple worker functions.** Each stage is a stateless function that reads from its input queue, processes, writes to its output queue. No bundled responsibilities.
- **Bucket-per-stage windowing.** Conversation windows are formed by an explicit segmenter, not by time-based heuristics, so windows are topic-coherent.
- **Stages don't touch the live KG directly.** The pipeline ends at the proposal layer (`claim_proposal*`); the proposal promoter is the sole writer to `kg_node_metadata` / `kg_edge_metadata`.

## Architectural principle

> **Each stage = one worker, one input bucket, one output bucket.**
> Worker reads from its input bucket, processes one unit, writes to its output bucket. Never reaches back, never modifies prior stages, never infers state from elsewhere.

Implications:
- The output table of stage N IS the input bucket of stage N+1.
- "What's ready for me?" is always the same query: items in my input bucket with no corresponding row in my output bucket.
- Any stage can be flushed (`TRUNCATE my_output_table`) and rebuilt — the next worker run picks up everything as "not yet processed."
- Per-stage queue depth is `SELECT COUNT(*)` on a single table.
- Workers are stateless. Each is a function: `process_one_unit(unit_id)`.

## TL;DR

```
unified_log_2026                           ← raw events (immutable, source of truth)
   │ Stage 1: resolve entities (chat-eligibility filter built in, per day)
   ▼
kg_resolved_message                        ← bucket: ready to segment (keyed by unified_log_id)
   │ Stage 2: segment into coherent conversation units
   ▼
kg_window (+ kg_window_message)          ← bucket: ready to critique+extract
   │ Stage 3: window_critic → fact_extractor (combined)
   ▼
kg_window_extraction                      ← bucket: ready to enrich
   │ Stage 3.5: split into components + meta_data_add per component
   ▼
kg_window_enrichment                      ← bucket: ready to write proposals (1:1 with future proposal)
   │ Stage 4: write proposals (deterministic, no LLM)
   ▼
claim_proposal (+ claim_proposal_node/edge/evidence)   ← bucket: ready to promote
   │ Stage 5: promoter routine (existing, separate schedule)
   │   - assigns TTL to State/Event nodes via state_ttl_estimator
   │   - decay job (separate maintenance routine) auto-closes stale eras
   ▼
kg_node_metadata + kg_edge_metadata        ← live KG (written by the promoter, stage 5)
```

5 stages run inside `KGPipeline`. Stage 5 (`proposal_promoter`) is a separate scheduled routine, unchanged from today.

**Canonical identity is `unified_log_id` end-to-end.** No intermediate filter-copy table. Every stage references messages by their `unified_log_2026.id`. Drill-down from any KG node back to source: `node → proposal → evidence → unified_log_id → unified_log_2026` (single hop to source).

## Worker model

- **5 daemon threads**, one per stage (Stages 1, 2, 3, 3.5, 4). Each thread owns one stage's worker function.
- Each thread loops: claim items from input bucket → process → write to output bucket → sleep `poll_interval` → repeat.
- **No multi-worker-per-stage parallelism today.** The principle scales (atomic claim from a shared bucket allows N workers), but we start with 1 worker per stage. Add workers per stage later only when a measurable bottleneck demands it.
- Stages overlap in time (concurrent), giving 5× throughput vs. fully sequential.
- Worker function signature is uniform across stages:

```python
def claim_one(input_table, output_table) -> Optional[InputRow]:
    """Atomically claim one input row that has no matching output row. Returns None if bucket is empty."""

def process(input_row) -> OutputRow:
    """Stateless work — agents, tools, anything. No DB session held during LLM calls."""

def commit(output_row):
    """Single-row INSERT into output bucket."""
```

## Stages

### Stage 1: Resolve entities per message

**Worker:** `ResolveMessagesStep` (LLM: `entity_resolver` agent, gpt-5.4-mini)
**Input:** `unified_log_2026` rows that are chat-eligible AND not yet in `kg_resolved_message`
**Output:** `kg_resolved_message` (one row per resolved chat-eligible message, keyed by `unified_log_id`)

**Behavior:** Per-day chronological. For each day, batch up to 10 unresolved messages with up to 10 already-resolved messages from the same day as context. Resolves entity references (`I` → `(Alex)`, `she` → `(Jamie)`, `the kids` → `(Casey, Drew)`). Original message stays in `unified_log_2026.message` (never modified anywhere).

**Chat-eligibility filter** (applied in the resolver's input query — there is no separate filter stage):
- `source` ∈ {`chat`, `room_slack`, `room_sms`, `room_ui`}
- `role` ∈ {`user`, `assistant`}
- non-empty message
- `room_id` ∈ {`master_room`} or null

**Queue query:** `unified_log_2026 u LEFT JOIN kg_resolved_message rm ON rm.unified_log_id = u.id WHERE rm.id IS NULL AND <eligibility>`. Order by day, then chronologically within day.

**Schema:**
```
kg_resolved_message
  id                  TEXT PRIMARY KEY (UUID)
  unified_log_id      TEXT UNIQUE NOT NULL  (FK to unified_log_2026.id)
  unified_timestamp   TIMESTAMP NOT NULL    (denormalized for cheap chronological scans)
  resolved_text       TEXT NOT NULL         (entity-substituted)
  resolved_entities   JSON                  (reserved; unused at start)
  resolver_version    TEXT
  resolved_at         TIMESTAMP DEFAULT now()
```

### Stage 2: Segment into coherent conversation units

**Worker:** `SegmentMessagesStep` (LLM: `conversation_boundary` agent, gpt-5.4-mini)
**Input:** `kg_resolved_message` (rows not yet covered by any window, scoped per-day)
**Output:** `kg_window` + `kg_window_message` (membership join)

**Behavior:** Reads up to one day's worth of new resolved messages. Calls the segmenter agent with the full span. Agent returns windows — coherent conversation units, each spanning one or more messages. Each window is written as one `kg_window` row plus N `kg_window_message` rows.

**Cadence:** Only segment when (a) there are at least N resolved messages contiguously available since the watermark, OR (b) the resolver has caught up to the most recent resolved message for that day. Prevents premature 1-msg windows on live conversations.

**Cross-window links:** When a new window continues a topic from an older window (the A B A pattern), the agent emits `related_previous_window_ids` so downstream consumers can rejoin them on demand. The worker never goes back and modifies older windows.

**Queue query:** `kg_resolved_message rm LEFT JOIN kg_window_message wm ON wm.unified_log_id = rm.unified_log_id WHERE wm.unified_log_id IS NULL`.

**Schema:**
```
kg_window
  id                            TEXT PRIMARY KEY (UUID)
  start_unified_log_id          TEXT NOT NULL  (FK to unified_log_2026.id)
  end_unified_log_id            TEXT NOT NULL  (FK to unified_log_2026.id)
  start_timestamp               TIMESTAMP NOT NULL
  end_timestamp                 TIMESTAMP NOT NULL
  message_count                 INTEGER NOT NULL
  summary                       TEXT             (segmenter's one-line summary)
  standalone                    BOOLEAN          (does this window stand on its own?)
  related_previous_window_ids   JSON             (list of window ids this continues)
  reason_for_boundary           TEXT             (segmenter's explanation, debug)
  segmenter_version             TEXT
  created_at                    TIMESTAMP DEFAULT now()

kg_window_message
  window_id        TEXT NOT NULL  (FK to kg_window.id)
  unified_log_id   TEXT NOT NULL UNIQUE  (FK to unified_log_2026.id — one window per message)
  item_order       INTEGER NOT NULL
  PRIMARY KEY (window_id, unified_log_id)
```

### Stage 3: Critique + extract (combined)

**Worker:** `CritiqueAndExtractStep` (LLMs: `window_critic` agent + `fact_extractor` agent)
**Input:** `kg_window` (rows not in `kg_window_extraction`)
**Output:** `kg_window_extraction` (one row per window)

**Behavior:** Combined per the design call — critic and extractor share the same input/output bucket. For each window:
1. Run `window_critic` against the window's user messages + assistant context.
2. If critic rejects → write `kg_window_extraction` row with `verdict = 'rejected'`, empty nodes/edges. Done.
3. If critic approves → run `fact_extractor` over the window.
4. Write `kg_window_extraction` row with `verdict = 'extracted'` + nodes/edges JSON.

If a flush is needed (re-extract with a tuned prompt), `TRUNCATE kg_window_extraction` and the next worker run reprocesses every window. Yes, that's both the critic AND the extractor re-running for every window — accepted cost of the combination.

**Queue query:** `kg_window s LEFT JOIN kg_window_extraction se ON se.window_id = s.id WHERE se.id IS NULL`.

**Schema:**
```
kg_window_extraction
  id                  TEXT PRIMARY KEY (UUID)
  window_id          TEXT UNIQUE NOT NULL  (FK to kg_window.id)
  verdict             TEXT NOT NULL          ('extracted' | 'rejected' | 'skipped' | 'error')
  verdict_reason      TEXT                   (critic reason if rejected; error message; etc.)
  nodes               JSON                   (extractor output; null if rejected/skipped)
  edges               JSON                   (extractor output; null if rejected/skipped)
  extractor_version   TEXT
  critic_version      TEXT
  created_at          TIMESTAMP DEFAULT now()
```

### Stage 3.5: Enrich

**Worker:** `EnrichExtractionStep` (LLM: `meta_data_add` agent, gpt-5.4-mini, one call per connected component)
**Input:** `kg_window_extraction` (rows where `verdict = 'extracted'`, not yet in `kg_window_enrichment`)
**Output:** `kg_window_enrichment` (one row per connected component → many rows per extraction)

**Behavior:**
1. Split the extraction's nodes/edges into connected components (deterministic — `split_into_connected_components`).
2. For each component, call `meta_data_add` to fill in: aliases, hash_tags, start_date / end_date (ISO + confidence + prose anchor), valid_during, category, semantic_label, goal_status, confidence, importance.
3. Write one `kg_window_enrichment` row per component carrying the enriched nodes + edges.

After Stage 3.5, each enrichment row maps **1:1 to a future claim_proposal**. This pre-stages the component-split so Stage 4 becomes purely deterministic.

If a flush is needed (re-enrich with a tuned prompt), `TRUNCATE kg_window_enrichment` and the next worker run reprocesses every extraction.

**Queue query:** `kg_window_extraction se LEFT JOIN kg_window_enrichment ke ON ke.window_extraction_id = se.id WHERE se.verdict = 'extracted' AND ke.id IS NULL`.

**Schema:**
```
kg_window_enrichment
  id                        TEXT PRIMARY KEY (UUID)
  window_extraction_id     TEXT NOT NULL  (FK to kg_window_extraction.id)
  component_index           INTEGER NOT NULL   (0, 1, 2, ... within the extraction)
  enriched_nodes            JSON NOT NULL      (component's nodes with enrichment fields)
  enriched_edges            JSON NOT NULL      (component's edges)
  enricher_version          TEXT
  created_at                TIMESTAMP DEFAULT now()

  UNIQUE (window_extraction_id, component_index)
```

### Stage 4: Write proposals

**Worker:** `WriteProposalsStep` (no LLM — pure deterministic DB writes)
**Input:** `kg_window_enrichment` (rows not yet in `claim_proposal_evidence`)
**Output:** `claim_proposal` + `claim_proposal_node` + `claim_proposal_edge` + `claim_proposal_evidence`

**Behavior:** For each enrichment row, write one `claim_proposal` group: one row in each of `claim_proposal`, `claim_proposal_node` (per node), `claim_proposal_edge` (per edge), `claim_proposal_evidence` (one row pointing back to the enrichment + window provenance).

No LLM calls — component-splitting and enrichment have already happened in Stage 3.5. This stage is just structured DB writes per enrichment row.

**Queue query:** `kg_window_enrichment ke LEFT JOIN claim_proposal_evidence cpe ON cpe.enrichment_id = ke.id WHERE cpe.id IS NULL`.

**Schema for `claim_proposal_evidence`:** the `window_id` column accepts ids from either `kg_window` (current pipeline) or `kg_chat_conversation_window` (older provenance archive — the ~3,612 nodes promoted before this pipeline existed point at it). The two UUID spaces are disjoint, so consumers (e.g. the node viewer) check the archive first and fall through to the current tables. `enrichment_id` is a denormalized convenience FK to `kg_window_enrichment.id`.

### Stage 5: Promote (existing routine, untouched)

**Routine:** `proposal_promoter` (runs on its own schedule, NOT part of `KGPipeline`)
**Input:** `claim_proposal` rows where `status = 'pending'`
**Output:** `kg_node_metadata` + `kg_edge_metadata` (live KG)

This is the only path that writes to the live KG. Unchanged from today's design. Two existing per-node enrichments happen here (NOT moved to earlier stages):

- **TTL estimation** for State/Event nodes via `state_ttl_estimator` agent. Stashes `{duration_class, estimated_duration_days, confidence, reasoning}` in `node.attributes["ttl"]`. The classes are: ephemeral (≤1 day), short_term (2–30 days), medium_term (30–180 days), long_term (180–730 days), durable (null = never expires).
- **Sentence canonicalization** for State/Event/Goal nodes via `fact_canonicalizer` agent. Rewrites the extractor sentence into present-tense canonical form before storing as `Node.original_sentence`.

Both of these happen at the moment a fresh `kg_node_metadata` row is created — they're shipping decisions, not extraction concerns, so they live in the promoter not in the enrich stage.

### Decay (separate maintenance routine)

The `step_state_decay.py` step in `kg_maintenance_pipeline` runs nightly and consumes the TTLs that Stage 5 set. For each active State/Event node:
- `expected_end = start_date + estimated_duration_days + GRACE_DAYS`
- If exceeded with no fresh re-observation → auto-close (`valid_to = expected_end`)
- Skips nodes with `confidence < LOW_CONFIDENCE_FLOOR` (stays open)
- Re-observation reopens / refreshes the era

Not part of `KGPipeline`. Listed here for completeness because TTL would be hanging metadata otherwise.

## Pipeline class + step contracts

```
app/assistant/pipelines/kg_pipeline/
├── pipeline.py              # KGPipeline class
├── runner.py                # bucket-aware runner (5 daemon threads)
├── steps/
│   ├── __init__.py
│   ├── resolve_messages.py       # Stage 1 (chat-eligibility filter + entity resolve)
│   ├── segment_messages.py       # Stage 2
│   ├── critique_and_extract.py   # Stage 3
│   ├── enrich_extraction.py      # Stage 3.5
│   └── write_proposals.py        # Stage 4
└── README.md                # links back to this doc
```

Each step exports a class with two methods:
- `claim_next() -> Optional[InputUnit]`
- `process(input_unit) -> OutputUnit`

The runner coordinates: spawns one daemon thread per step, each thread loops `claim_next → process → commit` on its bucket.

## Provenance archive

Three older tables are still on disk in read-only mode because
`kg_node_evidence.window_id` and `claim_proposal_evidence.window_id`
on ~3,612 KG nodes promoted before this pipeline existed point at
them:

- `kg_chat_projection` — canonical chat-only message log
- `kg_chat_conversation_window` — conversation grouping
- `kg_chat_conversation_window_item` — message ↔ window membership

Drill-down from any of those KG nodes to source conversation reads
through these tables. The live pipeline writes to `kg_resolved_message`
/ `kg_window` / `kg_window_message` instead; consumers (e.g. the node
viewer) check the archive first and fall through to the current tables
because the two UUID spaces are disjoint.

## Properties this gives us

- **Observable.** `SELECT COUNT(*) FROM kg_window_extraction WHERE verdict = 'rejected'` is a real metric. So is queue depth at every stage.
- **Replayable.** Flush stage N's output table → next pipeline run reprocesses everything for stage N onward. No surgery needed.
- **Debuggable.** "Where is window X stuck?" is one row's existence across N tables.
- **Decoupled.** Stage 4 doesn't know or care that stage 2 is still working. Each stage runs at its own pace.
- **Easy to add new sources later.** Email, docs, etc. enter at Stage 1 by extending the eligibility filter. Everything downstream is source-agnostic.

## Open questions / future work

- **Multi-worker-per-stage** when extractor becomes the throughput bottleneck.
- **Cross-window link consumption** — proposal_writer or wiki layer could use `related_previous_window_ids` to merge re-joined topics into one wiki paragraph.
- **Triager** — per-sentence salience tagger (sentence-level filter inside a window). Defer until needed.
- **Resolver `(unknown)` hallucination** — root cause prompt fix for cases like `Uni (university highschool) (unknown)`.
- **`enable_keyword_resource_injection` audit** — verify no other KG-adjacent agent has this flag bloated on.
- **Subsystem flag rename** (`kg_chat_pipeline` → `kg_pipeline`) — UI surface, deferred.

## Glossary

- **Message** — one row in `unified_log_2026`. The immutable source-of-truth log (also carries non-chat events; the resolver filters to chat-eligible ones).
- **Resolved message** — one row in `kg_resolved_message`. The entity-substituted version of one chat-eligible `unified_log_2026` row, keyed by `unified_log_id`.
- **Window** — one row in `kg_window`. A coherent conversation unit (one topic). The atomic unit of work for stages 2 onward.
- **Extraction** — one row in `kg_window_extraction`. The result of running critic+extractor on one window. Holds raw nodes/edges before component-split and enrichment.
- **Enrichment** — one row in `kg_window_enrichment`. One connected component from an extraction, with metadata enrichment (dates, aliases, importance, etc.) applied. Maps 1:1 to a future proposal.
- **Proposal** — one row in `claim_proposal`. One connected subgraph ready to be evaluated for KG promotion.
- **Promotion** — moving a proposal's nodes/edges into the live KG. Done by the promoter routine. State/Event nodes get TTL stamped at this moment.
- **Decay** — the maintenance pass that auto-closes State/Event eras when their TTL expires without re-observation.
