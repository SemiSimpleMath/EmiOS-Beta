# Pods (Datapods)

A pod is a URI-addressable content unit. Agents pass `datapod:kind:id` strings around instead of full chat transcripts, email bodies, image bytes, or tool output. The recipient hydrates a header on demand and only fetches the full body when it actually needs to read.

The milestone "chat became addressable memory" landed 2026-04-19; email and image pods followed shortly after. Before pods, every consumer that wanted to know "what did Alex say about creamer?" had to scan raw `unified_log_2026`. Now there is a curated semantic layer in front of the log, and the unit of currency between agents is a 6+ char id.

Pods are also the substrate for two later subsystems built on top of this primitive: **authority/scope gating** (`pod_store/authority.py`, `pod_store/pod_utils.py`) — every body read clears a scope wall and an authority band — and **secret/identity pods** (multi-projection, authority-banded credentials; see [SECRETS_ACCOUNTS.md](SECRETS_ACCOUNTS.md)).

> **For the concrete end-to-end trace** of what happens when a user pastes an image into chat, through to "find that picture and email it to the user's partner" being a working two-tool-call workflow, see [14b_PODS_MEDIA_LIFECYCLE.md](14b_PODS_MEDIA_LIFECYCLE.md).

## Naming

- **In prose**: "pod."
- **In code**: `datapod` everywhere — the table is `pod_store`, the URI scheme is `datapod:`, the runtime model is `Pod`, the contract is `PodRow`. The `pod_*` short form survives only in directory names (`pod_store/`, `pod_classifier_service.py`, `pod_search/`, `pod_fetch/`).
- **URI shape**: `datapod:<kind>:<id>` — `<kind>` is snake_case; `<id>` is lowercase alphanumeric, 6+ chars. The live kind set is the registry in `configs/pod_kinds.json` (see [Pod kinds](#pod-kinds-the-ssot-registry)), not a hardcoded list. Ids come in three shapes, all matching the one regex: 24-hex sha256 prefixes (chat clusters, emails, file ingest), 12-hex blake2b (`canonical_pod_id`, the SSOT builder), and 16-hex uuid slices (secret pods).

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
| `kind` | str, indexed | one of the kinds registered in `configs/pod_kinds.json` ([below](#pod-kinds-the-ssot-registry)) |
| `tags_json` | JSON list | tag names from `configs/pod_tags.yaml` |
| `one_liner` | text | terse 3–6 word subject line; load-bearing — shown to agents without hydration |
| `body` | text, nullable | full content if small enough to inline; null when better resolved from `source_refs` — or when the pod's values live in `PodProjection` rows (secret pods) |
| `source_refs_json` | JSON list of `{kind, id}` | back-pointers to evidence: `unified_log`, `event_repository:email`, `resource`, `image_file` |
| `for_agents_json` | JSON list | union of agents whose `pod_interest.tags` intersect this pod's tags — denormalized for fast query-by-agent |
| `scope_id` | str, nullable, indexed | originating room_id; null = system-wide |
| `min_authority` | int, default 50 | **read floor.** Scope authority must clear this to read the body. Defaults to `AUTH_CHAT` (50); content pods carrying sensitive payloads set higher. Bands in `pod_store/authority.py`. |
| `importance` | float, nullable, indexed | curation signal (0–10, NULL = unrated). Set at mint, editable in the pods UI via `PodStore.update_curation`. Sortable so it can rank retrieval. A distinct axis from `min_authority` (priority, not permission). |
| `created_by` | str, nullable | agent id or `pod_classifier` |
| `created_at` | timestamptz | default `now()` |
| `metadata_json` | JSON, nullable | kind-specific fields (sender, subject, tool name, classifier reasoning, critic verdict, `source_urls`). Not indexed — for post-hoc inspection. |

Two side tables back the secret/identity pods (`models.py`), created alongside `PodRow` by `PodStore._ensure_tables`:

- **`PodProjection`** — one row per authority-banded *view* of a pod's underlying value. An identity pod (SSN, DOB, phone) has projections like `full` / `last4` / `redacted` / `format`, each with its own `min_authority` and a `storage_kind` of `plain` (value inlined for low-authority derived views), `env` (`env_ref` names an `os.environ` var, resolved at fetch), or `file` (`file_ref` under `data/pod_secrets/`). The high-authority projection stores a *pointer*, never the secret bytes.
- **`PodAudit`** — one row per authority-gated operation (`fetch` / `list` / `put` / `revoke`), recording WHAT (pod + projection), WHO (caller scope + authority + agent), and the `allowed` / `denied` outcome. The value is never logged — only the projection name, `storage_kind`, and (for `env`) the var name.

The Pydantic counterpart is `Pod` in `app/assistant/pod_store/contracts.py` (carrying optional `min_authority` and `importance` overrides). `PodHeader` (same file) is the lightweight payload `PodInjector` attaches to a `Message` after scanning its text for pod URIs — `pod_id + kind + tags + one_liner + scope_id + created_by + created_at + content_type`, no body. Agents pass the bare `pod_id` string between each other; the injector hydrates the header on receipt.

## body vs. artifact principle

`pod.body` is the **searchable text representation** of the pod's content — chat transcript, email text, vision caption, OCR, transcript, etc. The actual underlying artifact (bytes, URL, external row) lives wherever artifacts of that type naturally live, and `pod.metadata` carries the pointer:

| Pod kind | `body` contains | Artifact lives |
|---|---|---|
| `chat_cluster` | the transcript itself | n/a — transcript is the content |
| `email` | the email body text | optional `.eml` on disk; for now the body is the artifact |
| `image` | vision caption + OCR text (filled by `image_pod_enrichment`) | `data/images/<hash[:2]>/<hash>.<ext>` (content-addressed) |
| `video` / `audio` / `document` / `file` | empty until an extraction pass runs (placeholder one_liner) | `data/pods/<hash[:2]>/<hash><ext>` (content-addressed, `file_ingest.py`) |
| `web_page` (future) | extracted readable text | `metadata.url` (HTTP, fetch on demand) |

`pod_search` (substring match over `one_liner` + `body`) finds pods via the searchable text; consumers fetch the artifact via `metadata.stored_path` (or `metadata.url`, etc.) only when they need to act on it (`send_email` attachments, vision recompute, etc.).

Why: putting bytes in `body` defeats `pod_search`. Putting bytes in a separate column inflates the DB. Re-extraction (better vision prompts) over the original artifact is cheap when the body is just a cache.

The full principle plus per-kind metadata conventions live in the `project_pod_body_vs_artifact_principle` memory note.

## Pod kinds (the SSOT registry)

`configs/pod_kinds.json` is the single source of truth for every pod `kind`. Each entry carries a `description`, a `body_extraction` hint, `default_for_agents`, and — load-bearing — a `kg_admissible` flag (see next section). `pod_store/pod_kind_registry.py` loads it once and exposes `known_kinds()`, `is_kg_admissible(kind)`, and `description(kind)`. **Missing kinds fail closed** (`is_kg_admissible` → False; missing file → empty registry). Adding a pod kind is a config edit, not a code change (`skills/extending-emi-pod-kinds/SKILL.md`).

Registered kinds today (17): `image`, `email`, `chat_cluster`, `note` (mint_pod), `file`, `tool_result`, `service_loop` (pipeline keep-alive), `research_finding` (web planner), `feedback`, `intention`, `plan`, `delivery.email`, `health.private`, and the secret-pod kinds `auth.session` / `auth.bearer` / `auth.oauth` / `identity.ssn`. The MIME-derived `video` / `audio` / `document` (`file_ingest.py`) are still minted without registry entries. The `PodKind` `Literal` in `contracts.py` is a non-authoritative subset — the DB column is a free string and the registry is the gate.

## Pods ↔ KG: reference-by-URI + `is_kg_admissible` gate

**Pods are NOT mirrored into the KG.** There was a `kg_mirror` that wrote a `node_type="Pod"` row into `kg_node_metadata` on every `PodStore.put`; it was **deleted**. `pod_store` is now the sole source of truth for pod content. The KG references a pod purely by its URI string — there is no Pod node, and the foreign key on `kg_edge_metadata.{source,target}_id → kg_node_metadata.id` was **dropped** to allow it.

The replacement gate lives in the promoter. `kg/proposal_promoter.py` (the `_resolve_endpoint` / `_pod_uri_is_admissible` region) accepts a `datapod:*` edge endpoint iff **(1)** it exists in `pod_store` AND **(2)** its kind is `kg_admissible: true` in `pod_kinds.json`. Otherwise the proposal abandons that edge with reason `"pod not admissible"`. Pod URIs bypass proposal-node resolution entirely (they're already resolved ids); the admission check is what replaces the dropped FK.

Today `image` and `file` are `kg_admissible` — user-shared media are referenced by entities/events:

```
the user --depicted_in--> datapod:image:abc...        (the user is in this photo)
the user --has_profile_image--> datapod:image:abc...  (canonical profile photo)
```

`email` and `chat_cluster` are deliberately **not** admissible: emails are noisy/sensitive/token-heavy, and chat clusters are conversation context, not facts (the KG ingests facts extracted *from* clusters, not the clusters themselves).

**Edge direction convention: KG-node → Pod.** The KG node "owns" or "documents-via" the pod ("the user is depicted in this pod"). Multiple anchors can edge into one pod via different roles. The `fact_extractor` learns one rule — treat `datapod:` URIs in resolved sentences as already-resolved node ids — and writes pod-targeting edges with a small kind-keyed vocabulary (see [14b](14b_PODS_MEDIA_LIFECYCLE.md)).

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

1. **Pass 1 — `pod_classifier`** (`app/assistant/agents/pod_classifier/`, `gpt-5.6-luna`). Inputs: rendered burst + tag vocabulary. Outputs `tags`, terse `one_liner`, and `sections` — the burst regrouped by topic with each line reproduced verbatim including its `- [time] Speaker:` prefix. Empty tags ⇒ no pod. The system prompt explicitly enumerates churn signatures (task-runner bookkeeping, tool output, ack-only replies, bare greetings) that should mint nothing.

2. **Pass 1.5 — `pod_critic`** (`app/assistant/agents/pod_critic/`, `gpt-5.6-luna`). Rejection gate. Reads `one_liner + tags + sections + pre_context`. Accept iff the pod contains **knowledge, preference, memory, or actionable** content (`AgentForm.content_type`). Rejects: no substance, ack-only, not about the user, derivative recap, KG-shaped entity facts, broad-trend material the nightly belief job will catch. **Fails open** — any exception in the critic call defaults to `accept=True` so a critic outage does not silently suppress pods (`:396`).

3. **Pass 2 — `pod_entity_resolver`** (`app/assistant/agents/pod_entity_resolver/`, `gpt-5.6-luna`). Annotates kept sections with inline entity parentheticals after pronouns and vague references — `"i (Alex) just got rdr2 / the game (RDR2) is amazing"`. Original words preserved exactly; only parens added. Entity cards are loaded via `entity_scan_keys: [sections_text, pre_context]` in the agent config. If the resolver returns a mismatched section count, falls back to raw sections (`:429`).

`pod_id` is deterministic from the sorted `signal_id`s in the burst (`_make_cluster_pod_id`, `:521`): `datapod:chat_cluster:<sha256[:24]>`. Same envelope set ⇒ same id ⇒ idempotent `put()`. `for_agents` is computed at mint time as the union of agents whose `pod_interest.tags` intersect the pod's tags (`_compute_for_agents`, `:510`; subscription map built once at startup by scanning the agent registry, `:543`).

Both critic and resolver get a "pre-context" block — up to 20 messages from the last 24h of the same room, loaded via the same `RoomHistoryBuilder` `master_room` chat_gate uses (`_fetch_chat_history`, `:448`). The burst's own envelopes are filtered out so they do not appear twice in the resolver prompt.

> Model choice: mini tier, not nano. Per the `pod_classifier_model` memory note, nano hangs on large bursts with rule-heavy prompts. All three pod agents (`pod_classifier`, `pod_critic`, `pod_entity_resolver`) run `gpt-5.6-luna` on the mini tier.

## PodStore

`app/assistant/pod_store/pod_store.py`. Plain SQLAlchemy wrapper. The core operations:

- `put(pod)` — upsert by `pod_id`, idempotent. Writes go through the `db_manager` serialized writer (pod_classifier fires `put` during the post-boot write storm, so this queues instead of colliding on SQLite's single-writer lock). **No KG mirror** — the docstring states pod_store is the sole source of truth.
- `get(pod_id) -> Optional[Pod]`.
- `update_curation(pod_id, *, importance=…, min_authority=…)` — the only mutator of an existing pod beyond `put`. Content/identity is immutable once minted; only the two curation fields are editable (pods UI). Uses an `_UNSET` sentinel so `importance=None` (clear the rating) differs from "omitted". Raises `KeyError` on a bad id (fail loud).
- `query(*, for_agent=None, tags=None, scope=None, kind=None, linked_to_entity=None, linked_via=None, since=None, since_utc=None, query=None, limit=50)`. Filters compose with AND; tags compose with OR within the list. `kind` narrows to one pod kind. `linked_to_entity` sub-selects pods that are the target of a KG edge from an `Entity` node with that label (alias-aware: exact label OR JSON-LIKE against the node's `aliases`); `linked_via` restricts that to specific edge `relationship_type`s. `since` accepts a `datetime`, ISO string, or shorthand (`24h`, `3d`, `2w`, `1m`, `today`).

The `query` substring search tokenizes the input, OR-matches each token against `one_liner` and `body` with case-insensitive `LIKE`, and ranks by `(one_liner_hits, total_token_hits, recency)`. Trailing-`s` stems are matched too (so "graphs" finds "graph"). Pulls 5× the limit, then ranks Python-side. Never returns zero when some tokens match — prefers a partial match to silence.

`ensure_tables()` runs in the constructor; `PodRow`, `PodProjection`, and `PodAudit` are created on first instantiation if missing.

The authority-gated projection API also lives on `PodStore` (`fetch_projection`, `list_projections`, `put_secret_pod`, `revoke_secret_pod`, `_audit`) — see [Secret / identity pods](#secret--identity-pods).

## Authority + scope gating

Two orthogonal walls guard every pod body read. Both are composed in **one place** — `pod_utils.read_pod_gated(pod_id, scope)`, the universal gate — so no surface re-rolls the logic. Its callers are `pod_fetch`, the `/pod expand` slash command, and the `/api/pods` route.

**Authority wall** (`pod_store/authority.py`). Pods reuse the same scope-authority axis that gates tool execution. Named bands:

| band | name | who |
|---|---|---|
| 10 | public | any agent — display-only projections (`redacted`, `format`) |
| 50 | chat surface | default for content pods (`AUTH_CHAT` = `DEFAULT_POD_MIN_AUTHORITY`) |
| 70 | gated chat | sensitive but shareable (area code, DOB year) |
| 99 | user-equivalent | master_room with explicit confirm |
| 100 | courier-only | non-LLM deterministic code only — full SSN, raw bytes |

The 99/100 cap is the key separation: **no LLM agent reads a 100-band projection** — only the courier (the deterministic substitution path inside `send_email` etc.) ever runs at 100. `caller_authority(scope)` reads `scope.approval.authority_level`, **fail-closed to 0** if absent. `check_authority(...)` raises `PodAuthorityError` (carrying required/actual) on a shortfall.

**Scope wall** (`pod_store/pod_utils.py`). `resolve_allowed_scopes(scope_ctx)` reads `scope.pods.allowed_scopes` (default `["self"]`, where `self` expands to the caller's `room_id`) and returns `["all"]` (unrestricted), a list of concrete scope_ids, or `["__none__"]`. A **None scope is a trusted system-internal read** (routine / dayflow tick with no room) → unrestricted, authority wall skipped. `SYSTEM_SCOPES = {master_room, dayflow_orchestrator}` are mutually pod-visible: a pod minted by either system surface is readable from both (`is_system_scope`, `_expand_system_scopes`). `pod_in_scope` is the wall itself: `["all"]` reads anything, otherwise the pod's `scope_id` must be in the allowed set (a null pod `scope_id` is in no concrete set → not readable, so cross-room fishing fails).

`PodNotFound` is raised for *both* "missing" and "out of scope" — indistinguishable on purpose, so a caller can't enumerate which non-readable pods exist.

### `canonical_pod_id` — the SSOT id builder

`pod_utils.canonical_pod_id(kind, *parts)` builds `datapod:<snake_kind>:<12-hex blake2b of parts>`, validated against `POD_URI_RE` before return. Re-minting the same logical unit upserts ONE pod, and the id always matches the regex the `PodInjector` + chat linkifier recognize. **Use this instead of hand-formatting pod ids** — hand-rolled ids that didn't match the regex were the phantom-mint bug the audit caught.

## `pod_search` and `pod_fetch`

Both tools are thin facades over `PodStoreTool` (`app/assistant/lib/core_tools/pod_store/pod_store_tool.py:50`) — same pattern as `KnowledgeGraphSearch`: one BaseTool class exposing two read-only `handle_*` methods.

### `pod_search` — header browse

Inputs: `query?`, `kind?`, `linked_to_entity?`, `linked_via?`, `tags?`, `since?`, `limit=20`. Returns headers only (no body): `{pod_id, kind, tags, one_liner, scope_id, created_by, created_at, content_type}`. Use to scan candidates and decide which are worth opening.

The tool contract (`app/assistant/lib/tools/pod_search/tool_contract.json`) is opinionated: `query` is preferred for topic/person/keyword search; `tags` is restricted to the fixed vocabulary `{food, entertainment, health, schedule, work}` — no inventing tag names. If a search returns zero, the contract instructs the caller to drop filters and retry rather than give up.

Filter compositions worth knowing:

- `kind="email", query="<sender>", since="7d"` — recent emails matching a sender or subject substring.
- `kind="image", linked_to_entity="<entity>", linked_via=["depicted_in","has_profile_image"], since="today"` — pods of that entity from today (the subquery joins `pod_store.pod_id` against `kg_edge_metadata.target_id` for edges from a matching `Entity` node; no pod node needed, the KG references pods by URI).
- `kind="image", linked_to_entity="<entity>", linked_via=["has_profile_image"]` — the canonical profile photo (one expected).
- `kind="video", linked_to_entity="<entity>", query="birthday"` — video pods of an entity matching a topic.

`scope` is **not** an agent-facing argument — it is derived from `tool_message.scope_context.pods.allowed_scopes` via `resolve_allowed_scopes` (the same SSOT the gate uses), then enforced: `["all"]` → no filter (owner surfaces like master_room); a single scope → straight filter; multi-scope → query each and merge deduped. So `pod_search` only ever returns pods the caller's scope can read — the earlier "cross-room is the default" framing no longer holds (chat-cluster pods are minted with `scope_id = room_id`, profile/findings pods FOR their originating room).

> Status: planned, not yet built — `pod_search` excerpts. The future plan is to return a short body excerpt around the matched query in each header so a caller can skip a `pod_fetch` round-trip.

### `pod_fetch` — full body

Input: `pod_ids` (list of strings, required). Returns `{pods, missing, denied}`. Each fetched pod includes header fields plus `body`, `source_refs`, `for_agents`, `metadata`. The result triages each requested id through **both** walls (mirroring `read_pod_gated`):

- **`missing`** — not found, OR out of the caller's scope (cross-scope ids are folded into `missing` so the caller can't tell the two apart).
- **`denied`** — in scope but the caller's authority is below the pod's `min_authority`. Never an error — reported separately so a caller knows the pod exists but is gated.
- A None scope (trusted system caller) does scope-only, skipping the authority wall.

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

The `[a-z0-9]{6,}` regex (`pod_uri.POD_URI_RE`) accepts every minter's id shape. In practice three are emitted: `sha256[:24]` (`_make_cluster_pod_id` for chat clusters, `_make_email_pod_id`, `file_ingest`), `blake2b` 12-hex (`canonical_pod_id` — the SSOT builder used for `research_finding` and any new code), and `uuid4().hex[:16]` (secret pods in `put_secret_pod`). All deterministic except the secret-pod uuid, so re-minting the same logical unit upserts one row.

## Email pods

`PodClassifierService.handle_envelope` dispatches on `signal_type`:

- `unified_log` → chat-burst path (existing — buffered per room, three-pass classifier).
- `email` → single-shot path (`_process_email`, no buffering): every email envelope from `EmailRepoSource` mints exactly one `kind="email"` pod immediately.

The email path is deliberately simpler than the chat path. Each email is already an atomic unit with structured metadata (subject, sender, body) — there's no "burst" to buffer, no entity-resolution problem (sender/recipient are explicit headers), and no need for the chat critic's spam/churn filter (Gmail labels did that upstream). For v1 there is no LLM tagging pass — `tags=[]` for email pods; consumers filter by `kind="email"` + `query` (sender/subject substring) + `since` instead.

Pod shape:
- `kind="email"`, `one_liner = "<sender>: <subject>"`, `body = full email text`.
- `source_refs = [{kind: "event_repository:email", id: "<signal_id>"}]`.
- `scope_id = account_id` so multi-account works.
- `metadata` carries `subject`, `sender_display`, `sender_email`, `account_id`, `uid`, `occurred_at_utc` for cheap consumer-side filtering without `pod_fetch`.

Idempotency: `pod_id = sha256(signal_id)[:24]` (mirrors the chat-cluster shape). Re-receiving the same email envelope no-ops via `PodStore.get`-then-skip rather than overwriting.

The first email-pod consumer is the `personal_admin` planner — its prompt teaches it to prefer `pod_search(kind="email", query=<sender>, since=...)` over Gmail-hitting tools for find-by-sender / scan-recent tasks. Workflow: pass `pod_id` strings forward, never inline bodies; downstream agents `pod_fetch` only when they need to act.

## Image pods

User-attached images flow through the chat upload path (`POST /process_request`) into `image_ingest.ingest_image_file`, which:

1. Hashes the bytes (sha256) and copies into `data/images/<hash[:2]>/<hash>.<ext>` (content-addressed, dedup-by-content).
2. Mints a `kind="image"` pod via `PodStore.put` (`image` is `kg_admissible`, so KG edges may later target it by URI).
3. Writes a sidecar JSON stamp (`<file>.emipod.json`) next to the stored copy with `{pod_id, sha256, stamped_at_utc}` so the reconciler can identify the file later regardless of filename.
4. The pod's `metadata` carries `sha256`, `stored_path` (repo-relative), `width`, `height`, `format`, `file_size_bytes`, `source_kind` (`chat_attachment` / `email_attachment` / `manual_upload`).
5. The `body` starts empty with `vision_extraction_status: "pending"` and a structural `one_liner` placeholder (`[image: jpeg, 320kb, 1920x1080]`).

**Vision extraction is built** (`pod_store/image_pod_enrichment.py`). `enrich_image_pod(pod_id, …)` runs the `image_pod_captioner` agent over the stored bytes plus surrounding chat context (the trigger message + a few messages from `unified_log_2026` around the pod's `occurred_at_utc`), then writes back `one_liner` + `body` (caption + OCR) + merged `tags` + `depicted_entities`, and flips `vision_extraction_status` to `"done"` (idempotent — a second call no-ops). It's triggered off the chat turn via a monitored thread so vision latency doesn't block the reply. Camera-captured pods get the analogous pass via `camera_dispatcher` running `scene_analyzer`.

After ingest, `ui_inbound_service` emits the **naked pod URI** as the chat marker:

```
"Hey the assistant here is a picture of me datapod:image:abc12345..."
```

The URI then flows through unified_log → entity_resolver (which preserves `datapod:` tokens verbatim, per a prompt rule) → fact_extractor (which has a kind-keyed edge vocabulary table — see [14b](14b_PODS_MEDIA_LIFECYCLE.md)) → proposal_writer → proposal_promoter, which writes `kg_edge_metadata` rows with `target_id = datapod:image:...`.

There are also **two image surfaces** with asymmetric needs:

- **Curated identity** — `resources/identity/<entity>.<ext>` (`jukka.jpg`, `peter.jpg`). Filename IS semantic. Replacing a photo keeps the same name. Profile/avatar use case. Personal photos gitignored except `README`.
- **Content-addressed flow** — `data/images/<hash>` (above). Hashed by content. Auto-populated by ingest paths.

Both surfaces produce the same kind of pod. KG edges express role: `has_profile_image` (one canonical, from `resources/identity/`) vs `depicted_in` (many, from any flow image). The `image_reconcile.reconcile_directory` routine walks either surface and uses sidecar stamps + content-hash lookup to handle file moves without breaking pod-store ↔ filesystem links — sidecar JSON over xattr/EXIF for v1 because portable. See `project_image_storage_and_stamping` memo.

## Generic file ingestion (`file_ingest.py`)

`pod_store/file_ingest.py` generalizes content-addressed ingestion beyond images. `ingest_file(src_path, …)`:

- **Routes image-shaped files** (MIME `image/*`) back through `image_ingest.ingest_image_file` so they keep the Pillow probe + vision-extraction wiring.
- **Everything else** lands in `data/pods/<sha256[:2]>/<sha256><ext>` with a minimal kind-tagged pod. The `kind` is derived from the MIME top-level slice: `video` / `audio` straight through, `application/*` + `text/*` → `document`, anything else → `file` (catch-all). The exact subtype goes in `metadata.mime_type` so callers can filter precisely (only PDFs, only mp4s) without parsing kind.
- `one_liner` is a structural placeholder (`[<kind>: <ext>, <KB>kb] <basename>`); `body` is empty until an extraction pass runs. `pod_id = datapod:<kind>:<sha256[:24]>`. Idempotent — re-ingesting the same bytes returns the existing pod.

## Minting tools

Two agent-facing tools mint pods directly, both writing real `pod_store` rows:

- **`mint_pod`** (`lib/tools/mint_pod/`) — persist provided content (a list, a note, contacts) as a `kind="note"` pod and return its canonical `pod_id`. Inputs: `title` (→ `one_liner`), `body` (verbatim), optional `tags`, `importance`, `min_authority` (default 50). The contract drills the agent to **surface the returned id** so the pod can be reopened with `pod_fetch`. `note` is the only allowed kind.
- **`mint_pod_from_path`** (`lib/tools/mint_pod_from_path/`) — mint a pod from a file on disk (delegates to `file_ingest.ingest_file`): hashes the bytes, content-addresses, dedups, kind-tags. Input: `path` (absolute; `~` accepted). Returns `{pod_id, kind, mime_type, sha256, stored_path, deduped}`. The canonical "pick file → mint → `send_email(pod_ids=[…])`" path. `min_authority: 95` (filesystem reach). This tool is the fix for the phantom-mint bug — agents that previously hand-formatted pod ids (which the injector/linkifier then couldn't recognize) now get a real, regex-valid, content-addressed id.

## Research-finding pods + generic propagation

The web research path mints durable findings as pods. `Planner._mint_research_findings` (`app/assistant/agent_classes/Planner.py`) turns each `findings_to_pod` unit into a `kind="research_finding"` pod whose id is `canonical_pod_id("research_finding", run, unit)` — deterministic per `(run, unit)`, so re-emitting a unit upserts the same pod (`run` = the session/scope id, dedup key). Source URLs ride in `metadata.source_urls`; `scope_id` is the originating room (None for ownerless system contexts → owner-only). It also accumulates a `research_notebook` of headers on the blackboard so the planner stops re-podding, and suppresses the raw tool-result scrapes from its working history once they're durably captured.

Propagation is generic — the pod ids flow into the message stream and `PodInjector` hydrates them downstream like any other URI. These surface to the user via the **`/pod expand`** slash command and the **`/api/pods`** route below ("Saved findings").

## `/pod expand` (slash command) and `/api/pods` (web)

Both are deterministic read surfaces that dereference a pod through the universal gate — **no LLM loop**.

- **`/pod expand <prefix>`** (`room_session_manager/services/pod_command.py`). Runs at room ingress and short-circuits the pipeline. Resolves the prefix against pod ids **surfaced in THIS room's last-24h messages** (not the global store — that both gives short matches and stops anyone fishing for pods they were never shown), then reads the body via `pod_utils.read_pod_gated` and posts it back out the same transport. `build_room_scope(room_id, surface)` constructs the gating scope from ROOM.md authority + `permissions.pod_scopes` — shared with `/api/pods` so both build it identically. Ambiguous prefixes prompt for more characters; `PodNotFound`/`PodAuthorityError` both become a polite refusal.
- **`/api/pods/<pod_id>`** (`app/routes/pod_api.py`, `get_pod_contents`). Returns a displayable pod's contents as JSON for the chat pod-viewer. **Two fences**: (1) the read goes through `read_pod_gated` using the owner web UI's `master_room` scope (so a 100-band courier pod, or one outside the owner's scope, is denied → 404), and (2) only kinds in `DISPLAYABLE_POD_KINDS = {"research_finding"}` are served — a fail-closed allowlist, so secret/credential/identity pods 404 even if the gate would pass them. The sibling `/api/pods/<pod_id>/image` streams image-pod bytes for inline chat rendering, guarded by a `relative_to` path-traversal check (not `startswith`).

## Secret / identity pods

Pods double as the credential store. `identity.*` (SSN, DOB, phone, account#) and `auth.*` (bearer / oauth) pods carry **multiple authority-banded projections** of one underlying secret via the `PodProjection` side table, gated by the bands in [Authority + scope gating](#authority--scope-gating). The full reference is **[SECRETS_ACCOUNTS.md](SECRETS_ACCOUNTS.md)**; in brief:

- `PodStore.put_secret_pod(pod_type, owner_subject_id, name, env_ref, scope, …)` reads the raw value **once** from `os.environ[env_ref]`, hands it to the registered **materializer** for `pod_type` (`pod_store/materializers/`), and writes the `PodRow` + all `PodProjection` rows in one transaction. The raw value goes out of scope immediately; only the `env_ref` pointer persists. Requires `AUTH_USER` (99) — minting a secret is itself sensitive.
- `fetch_projection(pod_id, projection, scope)` authority-checks, audits, then resolves the value by `storage_kind` (`plain` inline / `env` from `os.environ` / `file` from `data/pod_secrets/`). `list_projections` filters by authority so a 50-band agent listing an SSN pod sees `last4`/`redacted`/`format` and **cannot even enumerate** `full`.
- `revoke_secret_pod` hard-deletes a pod + all its projections (also `AUTH_USER`-gated, audited). Every operation writes a `PodAudit` row; the value is never logged.

## Errors as pods

> Status: planned, not yet built. The intent (per the `errors_as_messages` memory) is to write errors to `unified_log` as `source="error"` rows; the gut would then route them to `signal_router` for immediate surface and to `pod_classifier` for review on demand. Additive to the gut — no separate pipeline.

## Consumers (state of play)

What reads pods today:

- **`DietTrackerStep`** (`app/assistant/pipelines/dayflow/steps/diet_tracker_stage.py`) — the canonical consumer. On every dayflow tick it probes `PodStore.query(tags=["food"], since=last_run)`. If new food pods exist (or new diet-relevant tickets), it pulls them, renders them into a prompt block for the `diet_tracker` agent, and merges the agent's output into `resource_diet_log_today.json`. Demonstrates the pod_interest pattern in production: incremental, reactive, cheap.
- **`personal_admin` planner** — first email-pod and image-pod consumer. Its prompt teaches it two workflows:
  - "find by sender / scan recent" → `pod_search(kind="email", query=<sender>, since=...)`. Pass `pod_id` strings forward; never inline bodies.
  - "find media + email it" → `pod_search(kind="image", linked_to_entity=<user>, linked_via=[...], since=...)` followed by `send_email(pod_ids=[...])`. The send_email tool resolves each `pod_id` to its `metadata.stored_path` and attaches the file via Gmail's API (MIMEMultipart).
- **`PodInjector`** — auto-hydrates pod headers for any agent whose context contains URI strings. No agent has to opt in.
- **`fact_extractor`** — emits KG edges that target pod URIs (e.g., `the user --depicted_in--> datapod:image:...`). The extractor's prompt has a kind-keyed edge vocabulary; the proposal layer threads pod URIs through as already-resolved endpoints, gated by `is_kg_admissible` ([above](#pods--kg-reference-by-uri--is_kg_admissible-gate)).
- **`/pod expand` + `/api/pods`** — deterministic surfaces that show `research_finding` pods ("Saved findings") to the user without an agent loop ([above](#pod-expand-slash-command-and-apipods-web)).

What is planned but not built:

- A general **retrieval agent** that takes a user question, drives `pod_search` with appropriate query/tags/since/linked_to_entity, picks the most relevant headers, calls `pod_fetch`, and feeds the bodies to the answering agent. Today every consumer rolls its own retrieval against `PodStore.query`.
- **Error pods** (above).
- **`pod_search` excerpt return.**
- **Cross-task pod consumers** beyond `diet_tracker` (e.g., meal-calendar for `food`-tagged pods, entertainment recap for `entertainment`-tagged pods, sleep / exercise / expense trackers shaped like diet_tracker).

(Done since this doc's first draft: image **vision extraction** — `image_pod_enrichment.py`; **video / audio / document / file** ingestion — `file_ingest.py`.)

## Key files

| Path | Purpose |
| --- | --- |
| `app/assistant/pod_store/__init__.py` | Public API surface for the pod store package |
| `app/assistant/pod_store/models.py` | `PodRow`, `PodProjection`, `PodAudit` SQLAlchemy tables |
| `app/assistant/pod_store/contracts.py` | `Pod`, `PodHeader`, `PodSourceRef` |
| `app/assistant/pod_store/pod_store.py` | `PodStore` (`put`, `get`, `update_curation`, `query`; secret-pod projection API) |
| `app/assistant/pod_store/pod_utils.py` | SSOT pod ACCESS gate (`read_pod_gated`, `resolve_allowed_scopes`, `SYSTEM_SCOPES`) + `canonical_pod_id` |
| `app/assistant/pod_store/authority.py` | Authority bands (10/50/70/99/100), `check_authority`, `PodAuthorityError` |
| `app/assistant/pod_store/pod_kind_registry.py` | Loads `pod_kinds.json`; `is_kg_admissible` / `known_kinds` |
| `configs/pod_kinds.json` | **SSOT for pod kinds** + `kg_admissible` flag |
| `app/assistant/pod_store/pod_uri.py` | URI regex + `extract_pod_ids` / `hydrate_headers_from_text` |
| `app/assistant/pod_store/pod_classifier_service.py` | The minter — chat burst path + email single-shot path |
| `app/assistant/pod_store/image_ingest.py` | Image bytes → content-addressed storage + `kind=image` pod |
| `app/assistant/pod_store/image_pod_enrichment.py` | Vision + chat-context captioning of an image pod (`enrich_image_pod`) |
| `app/assistant/pod_store/file_ingest.py` | Generic file → content-addressed pod (video/audio/document/file) |
| `app/assistant/pod_store/file_stamp.py` | Sidecar JSON stamp for content-addressed files |
| `app/assistant/pod_store/image_reconcile.py` | Walk a directory, sync pods with disk, handle moves |
| `app/assistant/pod_store/materializers/` | Per-type secret-pod projection builders (see SECRETS_ACCOUNTS.md) |
| `app/assistant/agents/pod_classifier/` | Pass 1 agent (gpt-5.6-luna) |
| `app/assistant/agents/pod_critic/` | Pass 1.5 rejection gate (gpt-5.6-luna) |
| `app/assistant/agents/pod_entity_resolver/` | Pass 2 entity parentheticals (gpt-5.6-luna) |
| `configs/pod_tags.yaml` | Tag vocabulary (food, entertainment, health, schedule, work) |
| `app/assistant/ingest/ingest_service.py` | The gut — poll + fan-out |
| `app/assistant/ingest/contracts.py` | `IngestEnvelope`, `IngestSource`, `IngestSubscriber` |
| `app/assistant/ingest/sources/unified_log_source.py` | Chat source |
| `app/assistant/ingest/sources/email_repo_source.py` | Email source |
| `app/assistant/ingest/cursors.py` | `IngestCursorStore` |
| `app/assistant/signal_router/signal_router_service.py` | Sibling subscriber: reactive watch matching |
| `app/assistant/lib/core_tools/pod_store/pod_store_tool.py` | `pod_search` and `pod_fetch` handlers (scope + authority walls) |
| `app/assistant/lib/tools/pod_search/tool_contract.json` | Agent-facing search contract |
| `app/assistant/lib/tools/pod_fetch/tool_contract.json` | Agent-facing fetch contract |
| `app/assistant/lib/tools/mint_pod/`, `mint_pod_from_path/` | Minting tools (`note` pod; file → content-addressed pod) |
| `app/assistant/kg/proposal_promoter.py` | `_resolve_endpoint` / `_pod_uri_is_admissible` — the KG admission gate |
| `app/assistant/room_session_manager/services/pod_command.py` | `/pod expand` slash command + `build_room_scope` |
| `app/routes/pod_api.py` | `/api/pods/<id>` (JSON, `DISPLAYABLE_POD_KINDS`) + `/image` byte stream |
| `app/assistant/agent_runtime/services/pod_injector.py` | Auto-hydrates URIs in agent context |
| `app/assistant/utils/pydantic_classes.py` | `Message.referenced_pods` |
| `app/assistant/initialize_system.py` | Wiring: gut + signal_router + pod_classifier |
| `app/assistant/agent_classes/Planner.py` | `_mint_research_findings` — research_finding pods + notebook |
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
