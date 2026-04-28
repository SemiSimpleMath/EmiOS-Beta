# Pods (Datapods)

A pod is a URI-addressable content unit. Agents pass `datapod:kind:id` strings around instead of full chat transcripts, email bodies, or tool output. The recipient hydrates a header on demand and only fetches the full body when it actually needs to read.

The milestone "chat became addressable memory" landed 2026-04-19. Before pods, every consumer that wanted to know "what did Alex say about creamer?" had to scan raw `unified_log_2026`. Now there is a curated semantic layer in front of the log, and the unit of currency between agents is a 6+ char id.

## Naming

- **In prose**: "pod."
- **In code**: `datapod` everywhere — the table is `pod_store`, the URI scheme is `datapod:`, the runtime model is `Pod`, the contract is `PodRow`. The `pod_*` short form survives only in directory names (`pod_store/`, `pod_classifier_service.py`, `pod_search/`, `pod_fetch/`).
- **URI shape**: `datapod:<kind>:<id>` — `<kind>` is snake_case (`chat_cluster`, `email`); `<id>` is lowercase alphanumeric, 6+ chars. Old ids are 24-hex sha256 prefixes; new ids are 6 base36 (both match the same regex).

The canonical regex lives in `app/assistant/pod_store/pod_uri.py:23`:

```
\bdatapod:[a-z_][a-z_0-9]*:[a-z0-9]{6,}\b
```

**The `pod_id` IS the URI.** There is no separate URI/id split: every consumer (the regex in `pod_uri.py`, `PodStore.get`, the injector, the search/fetch tools) operates on the same `datapod:kind:id` string. The DB column `PodRow.pod_id` stores it verbatim, the `Pod` Pydantic model exposes it as `pod_id`, and agents emit and consume it as a single token.

## Pod schema

`PodRow` (`app/assistant/pod_store/models.py`) — one row per pod in the `pod_store` table:

| column | type | notes |
| --- | --- | --- |
| `pod_id` | str PK | full URI including scheme, e.g. `datapod:chat_cluster:abc…`. Deterministic when 1:1 to a source; uuid otherwise. |
| `kind` | str, indexed | `chat_cluster`, `email`, `tool_result`, `summary`, `resource_snapshot` |
| `tags_json` | JSON list | tag names from `configs/pod_tags.yaml` |
| `one_liner` | text | terse 3–6 word subject line; load-bearing — shown to agents without hydration |
| `body` | text, nullable | full content if small enough to inline; null when better resolved from `source_refs` |
| `source_refs_json` | JSON list of `{kind, id}` | back-pointers to evidence: `unified_log`, `event_repository:email`, `resource` |
| `for_agents_json` | JSON list | union of agents whose `pod_interest.tags` intersect this pod's tags — denormalized for fast query-by-agent |
| `scope_id` | str, nullable, indexed | originating room_id; null = system-wide |
| `created_by` | str, nullable | agent id or `pod_classifier` |
| `created_at` | timestamptz | default `now()` |
| `metadata_json` | JSON, nullable | kind-specific fields (sender, subject, tool name, classifier reasoning, critic verdict). Not indexed — for post-hoc inspection. |

The Pydantic counterpart is `Pod` in `app/assistant/pod_store/contracts.py:36`. `PodHeader` (same file) is the lightweight payload `PodInjector` attaches to a `Message` after scanning its text for pod URIs — `pod_id + kind + tags + one_liner + scope_id + created_by + created_at + content_type`, no body. Agents pass the bare `pod_id` string between each other; the injector hydrates the header on receipt.

## End-to-end pipeline

```
┌──────────────────────────┐
│ unified_log_2026         │  (chat rows; rowid-cursored)
│ event_repository.emails  │  (email rows; timestamp-cursored)
└────────────┬─────────────┘
             │ pull()
             ▼
   ┌───────────────────┐
   │ IngestService     │  the gut. Polls every 120s; fans out to subscribers.
   │ (app/assistant/   │
   │  ingest/)         │
   └─────┬───────┬─────┘
         │       │  IngestEnvelope (alias of SignalEnvelope)
         │       │
         ▼       ▼
 ┌──────────────┐  ┌──────────────────────────────┐
 │ Signal       │  │ PodClassifierService         │
 │ Router       │  │ (subscribes to chat only;    │
 │ (reactive    │  │  buffers per-room; flushes   │
 │  watches →   │  │  on quiet timer)             │
 │  events)     │  │                              │
 └──────────────┘  │  Pass 1 pod_classifier       │
                   │   ↓ tags + one_liner +       │
                   │     verbatim sections        │
                   │  Pass 1.5 pod_critic gate    │
                   │   ↓ accept/reject            │
                   │  Pass 2 pod_entity_resolver  │
                   │   ↓ inline parentheticals    │
                   │  PodStore.put()              │
                   └────────────┬─────────────────┘
                                ▼
                       ┌──────────────────┐
                       │ pod_store table  │
                       └────────┬─────────┘
                                │
              pod_search ───────┴────── pod_fetch     (read-only tools)
              PodInjector (auto-hydrate URIs in message text)
              Direct calls (DietTrackerStep, etc.)
```

## The gut (`IngestService`)

`app/assistant/ingest/ingest_service.py:36`. Polls registered `IngestSource`s on a fixed interval, normalizes each pull into `IngestEnvelope`s, and dispatches each envelope to every registered subscriber, sequentially. A subscriber that raises does not stop the next one — exceptions are logged and contained (`:89`).

Design choices documented in the module docstring (`:1`):

- **Direct callbacks**, not EventHub pub/sub. One fewer moving part; tighter debugging.
- **Sources own their cursors**, so the gut never re-delivers and has no internal dedupe.
- **No noise filtering inside the gut.** Filtering is a subscriber concern.

Sources today (`app/assistant/ingest/sources/`):
- `UnifiedLogSource` — pulls new chat-like rows from `unified_log_2026`, filtered by an explicit positive room list (`master_room`, `slack/*`, `tg_*`, `telegram/*`). rowid-based cursor; on first run pins to current `MAX(rowid)` so a fresh install does not re-ingest history.
- `EmailRepoSource` — pulls new rows from `EventRepository` for type `email`. Timestamp-cursored. First run pins to `now`.

Cursors persist in the `ingest_cursor` table via `IngestCursorStore` (`app/assistant/ingest/cursors.py`).

Wiring lives in `app/assistant/initialize_system.py:85-113`:

```
ingest_service = IngestService(
    sources=[UnifiedLogSource(), EmailRepoSource()],
    poll_interval_seconds=120,
)
ingest_service.register_subscriber(signal_router.handle_envelope)
ingest_service.register_subscriber(pod_classifier_service.handle_envelope)
ingest_service.start()
```

Both subscribers are wired in **before** `start()` so no envelope is dispatched into an empty subscriber list. Either can be disabled via `subsystems.yaml` (`ingest`, `signal_router`, `pod_classifier`).

## SignalRouter (sibling consumer)

`app/assistant/signal_router/signal_router_service.py:46`. The reactive sibling of the pod classifier. Consumers register watches (keyword_contains, email_semantic_match); each envelope runs through `garbage_filter → watcher_agent → dedupe → emit WatchMatchEvent`. Signal_router is a **pure subscriber** — it does not poll any source itself; it only handles whatever the gut sends it.

Pod-side and signal-side run independently on the same envelope stream. They do not coordinate, and that is the point: signals are reactive ("alert me if X arrives"); pods are declarative ("preserve any meal mentions for later").

## PodClassifier

`app/assistant/pod_store/pod_classifier_service.py`. Subscribes to the gut (`handle_envelope`, `:93`), filters to chat envelopes (`source_type == "unified_log"`), and **buffers per-room**. A background thread sweeps every `tick_interval_seconds` (default 60s); a room flushes when idle for `quiet_threshold_seconds` (default 300s) or when its buffer hits `_MAX_BURST_SIZE` (200) — whichever comes first.

Flush logic (`_process_burst`, `:210`) is three LLM passes:

1. **Pass 1 — `pod_classifier`** (`app/assistant/agents/pod_classifier/`, `gpt-5.4-mini`). Inputs: rendered burst + tag vocabulary. Outputs `tags`, terse `one_liner`, and `sections` — the burst regrouped by topic with each line reproduced verbatim including its `- [time] Speaker:` prefix. Empty tags ⇒ no pod. The system prompt explicitly enumerates churn signatures (task-runner bookkeeping, tool output, ack-only replies, bare greetings) that should mint nothing.

2. **Pass 1.5 — `pod_critic`** (`app/assistant/agents/pod_critic/`, `gpt-5.4-mini`). Rejection gate. Reads `one_liner + tags + sections + pre_context`. Accept iff the pod contains **knowledge, preference, memory, or actionable** content (`AgentForm.content_type`). Rejects: no substance, ack-only, not about the user, derivative recap, KG-shaped entity facts, broad-trend material the nightly belief job will catch. **Fails open** — any exception in the critic call defaults to `accept=True` so a critic outage does not silently suppress pods (`:396`).

3. **Pass 2 — `pod_entity_resolver`** (`app/assistant/agents/pod_entity_resolver/`, `gpt-5.4-mini`). Annotates kept sections with inline entity parentheticals after pronouns and vague references — `"i (Alex) just got rdr2 / the game (RDR2) is amazing"`. Original words preserved exactly; only parens added. Entity cards are loaded via `entity_scan_keys: [sections_text, pre_context]` in the agent config. If the resolver returns a mismatched section count, falls back to raw sections (`:429`).

`pod_id` is deterministic from the sorted `signal_id`s in the burst (`_make_cluster_pod_id`, `:521`): `datapod:chat_cluster:<sha256[:24]>`. Same envelope set ⇒ same id ⇒ idempotent `put()`. `for_agents` is computed at mint time as the union of agents whose `pod_interest.tags` intersect the pod's tags (`_compute_for_agents`, `:510`; subscription map built once at startup by scanning the agent registry, `:543`).

Both critic and resolver get a "pre-context" block — up to 20 messages from the last 24h of the same room, loaded via the same `RoomHistoryBuilder` `master_room` chat_gate uses (`_fetch_chat_history`, `:448`). The burst's own envelopes are filtered out so they do not appear twice in the resolver prompt.

> Model choice: `gpt-5.4-mini`, not nano. Per the `pod_classifier_model` memory note, nano hangs on large bursts with rule-heavy prompts. All three pod agents (`pod_classifier`, `pod_critic`, `pod_entity_resolver`) use mini.

## PodStore

`app/assistant/pod_store/pod_store.py`. Plain SQLAlchemy wrapper. Three operations matter:

- `put(pod)` — upsert by `pod_id`. Idempotent (`:74`).
- `get(pod_id) -> Optional[Pod]` (`:99`).
- `query(*, for_agent=None, tags=None, scope=None, since=None, query=None, limit=50)` (`:110`). Filters compose with AND; tags compose with OR within the list. `since` accepts a `datetime`, ISO string, or shorthand (`24h`, `3d`, `2w`, `1m`, `today`).

The `query` substring search (`:160`) tokenizes the input, OR-matches each token against `one_liner` and `body` with case-insensitive `LIKE`, and ranks by `(one_liner_hits, total_token_hits, recency)`. Trailing-`s` stems are matched too (so "graphs" finds "graph"). Pulls 5× the limit, then ranks Python-side. Never returns zero when some tokens match — prefers a partial match to silence.

`ensure_tables()` runs in the constructor; the table is created on first instantiation if missing.

## `pod_search` and `pod_fetch`

Both tools are thin facades over `PodStoreTool` (`app/assistant/lib/core_tools/pod_store/pod_store_tool.py:50`) — same pattern as `KnowledgeGraphSearch`: one BaseTool class exposing two read-only `handle_*` methods.

### `pod_search` — header browse

Inputs: `query?`, `tags?`, `since?`, `limit=20`. Returns headers only (no body): `{pod_id, kind, tags, one_liner, scope_id, created_by, created_at, content_type}`. Use to scan candidates and decide which are worth opening.

The tool contract (`app/assistant/lib/tools/pod_search/tool_contract.json`) is opinionated: `query` is preferred for topic/person/keyword search; `tags` is restricted to the fixed vocabulary `{food, entertainment, health, schedule, work}` — no inventing tag names. If a search returns zero, the contract instructs the caller to drop filters and retry rather than give up.

`scope` is intentionally not an agent-facing argument (`:104`): pods are user memory, not room-private, so cross-room retrieval is the default. A future scope policy would derive room from `tool_message.scope_context` rather than asking the agent.

> Status: planned, not yet built — `pod_search` excerpts. The future plan is to return a short body excerpt around the matched query in each header so a caller can skip a `pod_fetch` round-trip.

### `pod_fetch` — full body

Input: `pod_ids` (list of strings, required). Returns `{pods: [...full pod dump...], missing: [...ids not found...]}`. Missing ids are reported in `missing`, never as an error. Each fetched pod includes header fields plus `body`, `source_refs`, `for_agents`, `metadata`.

Both tools are `risk_level: low`, `side_effects: read_only`, `cost_level: low`.

## Pod-passing protocol

### `Message.referenced_pods`

`app/assistant/utils/pydantic_classes.py:178-180`:

```
# Pod references hydrated from message text (populated by PodInjector).
# Non-empty list means hydration has already run for this message.
referenced_pods: List[PodHeader] = Field(default_factory=list)
```

When a message body or task field contains pod URIs, `PodInjector` (`app/assistant/agent_runtime/services/pod_injector.py`) scans the configured keys (`incoming_message`, `task`, `information`, `recent_history`), extracts URIs via `pod_uri.extract_pod_ids`, hydrates each into a `PodHeader` via `PodStore.get`, and writes the headers onto `message.referenced_pods` as a side effect.

**Idempotency** is structural: if `referenced_pods` is already non-empty, `hydrate_for_context` returns the existing list without re-scanning (`pod_injector.py:46`). And because `PodHeader` itself contains no pod URIs, hydration is one-pass — there is nothing to recurse into.

Missing pods (agent hallucinations or deleted rows) are logged and skipped (`pod_uri.py:69`); they never raise.

### Prompt rendering

`PodInjector.format_block` produces the prompt-ready primer:

```
Referenced pods (IDs are references; call pod_fetch(pod_id) to read the full body):
- datapod:chat_cluster:abc… [food] · preference
    coffee creamer cutback
```

This is the agent's invitation to call `pod_fetch` if it actually needs the body. Pods are **lazy-hydrated** by design — most agents work entirely off the one_liner.

### Id design

The new id format is 6 base36 characters; the pre-existing format is 24 hex (sha256 prefix). The regex in `pod_uri.POD_URI_RE` accepts both. `PodClassifierService._make_cluster_pod_id` currently uses `sha256[:24]` for chat clusters (`:521`); shorter base36 ids are emitted by other minters where applicable.

## Email pods

> Status: partial. Email envelopes flow through the gut today (`EmailRepoSource`), but `PodClassifierService._is_chat_envelope` filters them out (`:303`); only `unified_log` envelopes are buffered and minted. The module docstring (`:13`) explicitly notes "Non-chat envelopes (email, etc.) are ignored for now — the first PodClassifier iteration is chat-only. Email/tool-result pods will be minted by dedicated paths later."

The design intent (per the `emails_as_pods` memory): mint each ingested email as a pod so agents pass `pod_id` references around instead of full email bodies. The pod schema is already email-aware — `PodKind` includes `"email"` and `PodSourceKind` includes `"event_repository:email"` — but the minter that produces those rows is not yet wired.

## Errors as pods

> Status: planned, not yet built. The intent (per the `errors_as_messages` memory) is to write errors to `unified_log` as `source="error"` rows; the gut would then route them to `signal_router` for immediate surface and to `pod_classifier` for review on demand. Additive to the gut — no separate pipeline.

## Consumers (state of play)

What reads pods today:

- **`DietTrackerStep`** (`app/assistant/pipelines/dayflow/steps/diet_tracker_stage.py`) — the canonical consumer. On every dayflow tick it probes `PodStore.query(tags=["food"], since=last_run)`. If new food pods exist (or new diet-relevant tickets), it pulls them, renders them into a prompt block for the `diet_tracker` agent, and merges the agent's output into `resource_diet_log_today.json`. Demonstrates the pod_interest pattern in production: incremental, reactive, cheap.
- **`PodInjector`** — auto-hydrates pod headers for any agent whose context contains URI strings. No agent has to opt in.

What is planned but not built:

- A general **retrieval agent** that takes a user question, drives `pod_search` with appropriate query/tags/since, picks the most relevant headers, calls `pod_fetch`, and feeds the bodies to the answering agent. Today every consumer rolls its own retrieval against `PodStore.query`.
- Email pods, error pods (above).
- `pod_search` excerpt return.
- Cross-task pod consumers beyond `diet_tracker` (e.g., a meal-calendar consumer for `food`-tagged pods, an entertainment recap consumer for `entertainment`-tagged pods).

## Key files

| Path | Purpose |
| --- | --- |
| `app/assistant/pod_store/__init__.py` | Public API surface for the pod store package |
| `app/assistant/pod_store/models.py` | `PodRow` SQLAlchemy table |
| `app/assistant/pod_store/contracts.py` | `Pod`, `PodHeader`, `PodSourceRef` |
| `app/assistant/pod_store/pod_store.py` | `PodStore` (`put`, `get`, `query`) |
| `app/assistant/pod_store/pod_uri.py` | URI regex + `hydrate_headers_from_text` |
| `app/assistant/pod_store/pod_classifier_service.py` | The minter — buffer, flush, 3-pass classify+critique+resolve |
| `app/assistant/agents/pod_classifier/` | Pass 1 agent (gpt-5.4-mini) |
| `app/assistant/agents/pod_critic/` | Pass 1.5 rejection gate (gpt-5.4-mini) |
| `app/assistant/agents/pod_entity_resolver/` | Pass 2 entity parentheticals (gpt-5.4-mini) |
| `configs/pod_tags.yaml` | Tag vocabulary (food, entertainment, health, schedule, work) |
| `app/assistant/ingest/ingest_service.py` | The gut — poll + fan-out |
| `app/assistant/ingest/contracts.py` | `IngestEnvelope`, `IngestSource`, `IngestSubscriber` |
| `app/assistant/ingest/sources/unified_log_source.py` | Chat source |
| `app/assistant/ingest/sources/email_repo_source.py` | Email source |
| `app/assistant/ingest/cursors.py` | `IngestCursorStore` |
| `app/assistant/signal_router/signal_router_service.py` | Sibling subscriber: reactive watch matching |
| `app/assistant/lib/core_tools/pod_store/pod_store_tool.py` | `pod_search` and `pod_fetch` handlers |
| `app/assistant/lib/tools/pod_search/tool_contract.json` | Agent-facing search contract |
| `app/assistant/lib/tools/pod_fetch/tool_contract.json` | Agent-facing fetch contract |
| `app/assistant/agent_runtime/services/pod_injector.py` | Auto-hydrates URIs in agent context |
| `app/assistant/utils/pydantic_classes.py:180` | `Message.referenced_pods` |
| `app/assistant/initialize_system.py:85-113` | Wiring: gut + signal_router + pod_classifier |
| `app/assistant/pipelines/dayflow/steps/diet_tracker_stage.py` | Reference consumer |

## How to add a new pod source

1. Implement the `IngestSource` protocol (`app/assistant/ingest/contracts.py:26`): `name: str`, `pull() -> List[IngestEnvelope]`. Own your cursor (use `IngestCursorStore`); pin to "now" on first run so a fresh install does not re-ingest history.
2. Add it to the source list in `app/assistant/initialize_system.py` where `IngestService` is constructed.
3. Either teach `PodClassifierService._is_chat_envelope` (or add a sibling `_is_<x>_envelope`) to recognize the new `source_type`, or — if the new source is not chat-shaped — write a dedicated minter that subscribes via `ingest_service.register_subscriber(my_minter.handle_envelope)` and writes to `PodStore.put` directly.

## How to add a new pod consumer

1. Decide your retrieval surface:
   - **Reactive**, want to be invoked when matching pods exist: use `PodStore.query(tags=[…], since=<last_run>, limit=N)` from a routine or pipeline step (see `DietTrackerStep` as the template).
   - **Inline within an agent turn**: declare `pod_interest.tags: [...]` in the agent's `config.yaml`. At pod mint time the classifier writes your agent's name into `for_agents`, so a future `for_agent=` query finds your pods cheaply.
   - **Pod URIs already in the message**: do nothing — `PodInjector` auto-hydrates headers and renders them in the prompt; the agent calls `pod_fetch` if it needs the body.
2. If you need full bodies, call `pod_fetch` (via the agent tool) or `PodStore.get(pod_id)` directly for in-process consumers.
3. If you mint *new* pods as part of consumption (e.g., a `summary` pod that aggregates several `chat_cluster` pods), construct a `Pod`, set `source_refs` to the parent pod ids/unified_log ids, and call `PodStore.put`. Idempotent on `pod_id`.
