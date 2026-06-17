# Glossary

A–Z definitions of every term used in the EmiOS codebase. Keep this open in a tab while reading the architecture docs. Cross-references in **bold**.

---

### action

A field on the **blackboard** that an **agent** sets to indicate what it wants done next. Resolved by **tool_caller**. Common values: a tool name, an agent name, or a control node name.

### action_input

The dict argument to whatever `action` is. Filled by either the agent itself (if it knows the schema) or a downstream **tool arguments** agent.

### Agent

(1) A directory under `app/assistant/agents/<name>/` containing `config.yaml`, `prompts/`, optional `agent_form.py`, optional `input_schema.py`. (2) An instance of one of the **agent classes** (`Agent`, `Planner`, `MultiToolAgent`) created from such a directory.

Agents make decisions; they do not execute actions. See [01_AGENTS.md](architecture/01_AGENTS.md).

### agent_form.py

A Python file under an agent's directory containing a Pydantic `AgentForm` class that defines the agent's structured output. Takes precedence over `config.yaml`'s `structured_output` field when present.

### AFK Monitor

The active-first idle detector. Records active segments, infers AFK from gaps. Routines can opt into an `afk_guard` to skip when the user is away.

### answer_matcher

The **Subconscious** agent (`subconscious::answer_matcher`, `gpt-5.4-mini`, no tools) that closes the ask→answer loop. When a **pending question** is asked and the user later speaks, `answer_capture.check_open_questions` pulls candidate messages and asks the matcher for a `verdict` ∈ `{answered, partial, no_answer}` + `answer_text`. It biases to `partial`/low-confidence when unsure, because a wrong captured answer corrupts the concern it routes back to. On `answered` it journals the answer onto the related concern and fires a cooldown-guarded **noticer** tick. See [SUBCONSCIOUS.md](architecture/SUBCONSCIOUS.md) §5.

### authority_level

An integer (0–100) on `ScopeApprovalPolicy` that gates tool **use** and **approval**. Master room is 99; most other rooms are lower. Conventional bands: 0 (toolless), 98 (compiled tasks), 99 (autonomous routines/subsystems — no human approver, so 99 clears the approval gate), 100 (courier / `.env`-deterministic admin **bypass only**). A tool whose `min_authority` floor exceeds the scope's level is unreachable; below a tool's `approval_min_authority` the call needs human approval. `>= 100` is the admin bypass for both. See [SCOPE.md](architecture/SCOPE.md) §10 and **four-layer tool gate**.

### ask_kg

The agent-facing KG read tool (`lib/tools/ask_kg/`). RAG Q&A over the knowledge graph in two modes: **global** (omit `node` → embedding search across the whole graph) and **node-anchored** (provide `node` → bounded BFS neighborhood, ranked by question-embedding similarity). Sends a compact evidence pack to a fast model and returns a cited answer. The replacement for the retired `kg_query_manager` / `kg_team_manager`. Distinct from `kg_query` (raw read-only SQL investigator). `min_authority` 90.

### archive (beliefs)

The eviction lane for deprecated beliefs (`belief_engine/archive.py`). Deprecation only flips `status='deprecated'`; the row never leaves the live table on its own. The nightly `belief_archive` routine atomically moves every `status='deprecated'` belief **and its evidence** out of `user_beliefs`/`belief_evidence` into `user_beliefs_archive` / `belief_evidence_archive` (same `emi.db`, created `AS SELECT * … WHERE 0`) and drops the belief's `belief_tags`. So the live tables hold only `active` + `contested` beliefs. FK enforcement is OFF during the move so deletes don't cascade into `belief_short_id` / `belief_merges`. See [16_BELIEF_ENGINE.md](architecture/16_BELIEF_ENGINE.md) §9.

### belief kind

One of six classes a belief is tagged with to drive **half-life decay** (`belief_engine/decay/model.py`): `durable_fact` (no decay), `stable_relationship` (5y), `stable_preference` (365d), `routine_pattern` (90d, default), `episodic_context` (14d), `transient_state` (1d). Evidence weight decays as `w × 0.5^(age_days / half_life)`. `durable_fact` immunity is what makes universal decay safe. `classify_kind_heuristic` backfills a kind for rows that lack one. See **RecomputeBeliefSnapshot**, [16_BELIEF_ENGINE.md](architecture/16_BELIEF_ENGINE.md) §5.

### belief_merges

Belief merge-provenance table `(loser_id PK, survivor_id, merged_at, reason)`, written by `BeliefStore.merge_belief` on every canonicalization merge. A merged-away belief keeps its **short id** so it stays citable; the redirect points the loser at its survivor. Write-only — stays live even after the loser is archived. See [16_BELIEF_ENGINE.md](architecture/16_BELIEF_ENGINE.md) §8.

### belief_short_id

A compact, LLM-citable `b<n>` handle assigned once per belief via a monotonic counter (`belief_engine/identity.py`, table `belief_short_id`), **never reused or changed**. Minted at belief creation (`ensure_short_id`) and backfilled nightly (`assign_short_ids`). Survives a merge so provenance stays addressable.

### belief tag vocabulary

The **24-tag standardized retrieval vocabulary** in `configs/belief_tags.yaml` — the controlled set of cross-cutting tags a belief may carry so consumers can **pull** it where it's needed (food/meal/dietary/health/sleep/family/routine/work/…). Multi-label: a belief carries every tag that applies, written to the additive `belief_tags` table by `belief_engine::belief_tagger`. Off-vocab tags are dropped (`sanitize()` is the single enforcement point) — the anti-proliferation guarantee. **Distinct from `domain`**, which stays the nightly *derivation* lane; tags are a separate *retrieval* layer. Consumers read it via a **pull_set**. See [16_BELIEF_ENGINE.md](architecture/16_BELIEF_ENGINE.md) §7.

### beliefs_for_context

The live ranked, tag-scoped belief retrieval API (`belief_engine/retrieval.py`). `beliefs_for_context(query=, tags=, k=)` queries the live DB for **active** beliefs and scores them `0.55·relevance (embedding cosine) + 0.25·recency (30d half-life) + 0.20·frequency (saturating log of observation_count)` (a usage weight was later folded in). `status='active'` is the only hard filter; the tag scope (a consumer's **pull_set**) applies only when the store is actually tagged (else high-recall — never returns nothing). The meal engine uses it live; dayflow stages still read the exported JSON. See [16_BELIEF_ENGINE.md](architecture/16_BELIEF_ENGINE.md) §10.

### Blackboard

Per-invocation scoped state stack owned by a manager. Has a global scope plus pushable local scopes (created when one agent calls another). Methods: `get_state_value(key)` reads top-down, `update_state_value(key, val)` writes to current top scope, `add_msg(message)` appends to message log.

Distinct from **Global Blackboard**.

### canonical sentence

The present-tense form of a State/Event/Goal node's `original_sentence` field. Set at promotion time by the `fact_canonicalizer` agent. Validity dates live in `start_date`/`end_date`/`valid_during` rather than in the sentence itself. See `project_present_tense_canonical_principle.md` (memory).

### canonical_pod_id

The SSOT pod-id builder (`pod_store/pod_utils.canonical_pod_id(kind, *parts)`). Returns `datapod:<snake_kind>:<12-hex blake2b of parts>`, validated against the pod URI regex before return. Re-minting the same logical unit upserts ONE pod, and the id always matches the regex the `PodInjector` + chat linkifier recognize. **Use this instead of hand-formatting pod ids** — hand-rolled ids were the phantom-mint bug the pod audit caught. See [14_PODS.md](architecture/14_PODS.md).

### chat_gate

The first agent in a room manager's loop. Decides per turn: reply directly, hand off to the switchboard, or (master_room only) delegate to dayflow.

### claim_proposal

A row in `claim_proposal` plus its sibling tables (`claim_proposal_node`, `claim_proposal_edge`, `claim_proposal_evidence`). Represents a connected subgraph of nodes/edges produced by the **KG Pipeline** that is *pending* promotion into the live KG. The promoter routine consumes these. See also: **Shadow KG**, **proposal_promoter**.

### compiled task

A pre-planned task IR (intermediate representation) saved as JSON under `tasks/<task_id>/<compiled_file>.json`. Lets a routine skip re-planning. Used by `morning_briefing` and `activity_log`. Steps can pin specific tools (`pinned_tools`) to bypass narrowing.

### concerns register

The durable spine of the **Subconscious** (`resources/subconscious/resource_concerns_register.json`) — four buckets: `active` (live; the only bucket proposers read), `addressing` (work in flight), `resolved`, `dormant` (accepted-chronic, compacted). Each concern carries `concern_id`, `title`, `kind`, `severity`, `horizon`, `evidence[]`, `addressable_by[]` (which lanes route it), plus lifecycle bookkeeping. The **noticer** is the only LLM that mutates concern lifecycle; lifecycle *pressure* (`compute_pressure`) forces a disposition on long-running concerns. Every other component feeds it (noticer, answer-capture) or reads it (proposers, digest, dashboard). See [SUBCONSCIOUS.md](architecture/SUBCONSCIOUS.md) §2.

### confidence band

A belief's *effective* confidence, recomputed nightly from evidence weights and written to `user_beliefs.current_confidence_band` (distinct from the LLM-assigned `confidence` set at extraction). One of `high` / `medium` / `low` / `faded` / `contested` / `deprecated_by_contradiction`, classified by `band_for_weights` (`belief_engine/decay/model.py`): `conflict_ratio ≥ 0.6` → `deprecated_by_contradiction`; both support and contradiction ≥ 2.0 → `contested`; else by net weight. `faded` deprecates the belief; `contested`/`deprecated_by_contradiction` queue it for reevaluation. Agents read the band, not `confidence`. See **RecomputeBeliefSnapshot**.

### consistency_critic

The wiki agent that audits a generated wiki page against the KG and the user's profile/cards, raising `wiki_contradiction` findings on `kg_maintenance_finding`.

### Control Node

Deterministic Python in the agent loop. Reads blackboard state, decides what to do next, sets `next_agent`. Lives in `app/assistant/control_nodes/`. The most important one is `tool_caller`. See [04_CONTROL_NODES.md](architecture/04_CONTROL_NODES.md).

### Dayflow

The autonomous daily workflow engine. Ingests tickets, calendar, emails, cross-room chat into a unified item stream and runs them through a state machine (`new → important_open → actionable → dispatched → acted_on → closed`). See [05_DAYFLOW.md](architecture/05_DAYFLOW.md).

### dayflow item

A single thing that the Dayflow orchestrator is tracking. Stored as a `Message` row in `unified_log_2026` with `source='dayflow_item'`. Has a short_id (numeric) for LLM prompts and a stable Message.id for upserts.

### datapod

The code-level name for a **pod**. URI-addressable content unit with `datapod://` scheme. See [14_PODS.md](architecture/14_PODS.md).

### delegator

The first agent in an `emi_team_manager`-derived flow. Reads the inbound task, picks the right specialized worker, hands off. Reused by all derived managers.

### DI

The dependency-injection container. `from app.assistant.ServiceLocator.service_locator import DI`. Provides `DI.event_hub`, `DI.tool_registry`, `DI.agent_registry`, `DI.global_blackboard`, `DI.resource_manager`, `DI.socket_manager`, etc.

### digest (subconscious)

The daily "what I've been noticing" message — **pure Python templating, no LLM** (the 04:00 **noticer** tick already did the thinking; the digest is the voice). `digest_runner.run_digest_pass` renders the **concerns register** into sections (New this round / Still tracking / Resolved / up to 2 pending questions), writes `app/subconscious_digests/digest_YYYY-MM-DD.md`, persists an assistant row to `unified_log_2026` (`source="subconscious_digest"`), and pushes to live subscribers. Routine `subconscious_digest`, window 07:30–22:00. See [SUBCONSCIOUS.md](architecture/SUBCONSCIOUS.md) §7.

### Dojo

The new name for `practice_runner` (renamed 2026-04-26). The harness that runs a task spec end-to-end against a manager for evaluation.

### Edge

A directed connection between two **Nodes** in the KG. Stored in `kg_edge_metadata`. Has `source_id`, `target_id` (FK to `kg_node_metadata` with `ondelete='CASCADE'` — relevant for the merge_nodes flush ordering), `relationship_type`, `sentence`, `window_id`.

### emi_team_manager

The general-purpose worker manager. Shape: delegator → planner → tool_caller (loop with critic) → summary → final_answer. Specialized managers reuse its delegator + summary while swapping in their own planner + final_answer. See [15_EMI_TEAM_AND_SCOPE.md](architecture/15_EMI_TEAM_AND_SCOPE.md).

### EmiPedia

The user-facing personal-life wiki — Markdown pages about people, places, events that EmiOS generates from the knowledge graph into a vault on disk (default `<your-home>/EmiWiki`). Browseable at `/wiki/`. When prose or code says "the wiki" without qualification, it means EmiPedia.

### Entity Card

A structured **NOW-snapshot** of a person/place/thing — "what's true right now," rendered from the entity's KG neighborhood into ordered sections of present-tense bullets. **v2** since 2026-05-10 (the v1 `EntityCard` ORM and flat `entity_cards` table were retired the same day); stored across four tables (`entity_card_v2` + `entity_card_section` + `entity_card_bullet` + `entity_card_bullet_source_node`). The structured companion to the narrative **wiki page** — same KG, different temporal stance. Built by `build_card(entity_node_id)` (a 5-agent pipeline + structural KG walks); refreshed nightly by `entity_card_refresh`. The user's own entity is deliberately card-less. Viewed/edited at `/entity-cards-v2` (kg cards are read-only there). See [12_ENTITY_CARDS.md](architecture/12_ENTITY_CARDS.md).

### EventHub

Pub-sub messaging service exposed via `DI.event_hub`. Topics like `socket_emit`, `repo_update`, `agent_progress_emit`, `proactive_suggestion`, `afk_state_changed`, `dayflow_ticket_responded`. `event_hub.register_event(topic, handler)` to subscribe; `event_hub.publish(message)` to emit.

### evidence

(KG) Append-only rows in `kg_node_evidence` / `kg_edge_evidence` — one per observation. Every time the promoter creates or matches a node/edge from a `claim_proposal`, a row is appended carrying `source_table` (`unified_log_2026`), `source_id` (the source message id), `source_text` (raw chat snippet), `derived_sentence` (extractor's sentence), `message_timestamp`, `window_id` (FK to `kg_window`), and `merge_action` (`created` | `confirmed` | `updated`). This is the canonical provenance store — JOIN here, don't read denormalized columns off the node row. See [09_KG_PIPELINE.md](architecture/09_KG_PIPELINE.md).

(Investigator) The `evidence` array in an investigation report — list of `(query, finding)` pairs grounding the diagnosis.

### feature_guard

A field on a routine config that gates execution on a user-feature-flag check. `{"feature_guard": "email"}` skips the routine if the user has email disabled. Calls `can_run_feature(name)` from user settings.

### final_answer

The last agent in a manager loop. Produces a human-readable answer plus a structured `final_answer_data_list`. Different manager namespaces have their own variants (`emi_team::final_answer`, `kg_investigation::final_answer`, `kg_mutation::final_answer`).

### finding

(KG maintenance) A row in `kg_maintenance_finding`. Statuses: pending → investigated → executed/dismissed/escalated. Subtypes: duplicate_node, orphan_node, duplicate_edge, missing_description, state_missing_dates, state_auto_closed, wiki_contradiction, synthetic_fact_proposal. See [22_KG_HEALTH_COMPONENTS.md](architecture/22_KG_HEALTH_COMPONENTS.md).

### four-layer tool gate

The access model for a tool call — it must clear all four, any one stops it (`07_TOOLS.md`, `SCOPE.md` §6). **(0/1) `allowed_tools` ceiling**: the inherited grant, narrowed (never widened) at manager ingress by `ScopeAdapter`, plus the visibility narrowing that decides what the planner *sees* (`ToolPolicyResolver`). `["all"]` is the wildcard; an empty set means *nothing*. **(2) `min_authority` floor**: an L1 see+use floor (0–100) on each contract, enforced at execution by `check_tool_access`; a first-party contract that omits it **fails closed at 99**. **(3) `approval_min_authority`**: the L2 approval threshold — below it the call needs the owner's sign-off (`compute_approval_reasons` → an approval ticket homed to `master_room`). Authority `>= 100` is the admin bypass for both walls. **Visibility never grants permission** — `allowed_tools` is the only grant. See **per_manager**, **min_authority**.

### Global Blackboard

Process-wide message log accessed via `DI.global_blackboard`. Survives across manager invocations. Distinct from a manager's per-invocation **Blackboard**.

### the gut

The intake pipeline (named `IngestService` in code) that fans out incoming events to two siblings: `SignalRouter` (reactive event publication) and `PodClassifier` (declarative pod minting). See [14_PODS.md](architecture/14_PODS.md).

### handoff_tf

A boolean field set by chat_gate. `true` → chat_task_router routes to switchboard. `false` → reply directly.

### InboundEnvelope

Transport-agnostic representation of an inbound message, built by `RoomSessionManager` from whatever surface (UI / SMS / Slack / Telegram) the message came from.

### intention.* pods

The **proposals** the **Subconscious** proposer lanes mint — `intention.meal` / `.shopping` / `.meal_set` (daily_meal_proposer), `intention.wellness` / `.wellness_set` (wellness_proposer), `intention.romantic` / `.romantic_set` (romantic_proposer). Each proposer reads `active`+`addressing` concerns whose `addressable_by` names it and mints `intention.*` **pods**; the **scheduler arbiter** aggregates them into one **plan.weekly_schedule**. They surface on `/subconscious` with per-item comment boxes (a comment → `feedback.comment` pod → belief). Nothing acts on the world directly — proposers only propose. See [SUBCONSCIOUS.md](architecture/SUBCONSCIOUS.md) §8.

### investigation report

The structured output of `kg_investigation_manager`: `{diagnosis, evidence, proposed_action, open_questions}`. Stored in `kg_maintenance_finding.investigation_report_json`.

### investigator

The `kg_investigation_manager` — read-only KG investigator that converts a pending finding into an investigation report. Uses the `kg_query` tool with an allowlist.

### is_kg_admissible

The gate that decides whether a `datapod:*` URI may be a KG edge endpoint (`pod_store/pod_kind_registry.is_kg_admissible(kind)`, backed by the `kg_admissible` flag in `configs/pod_kinds.json`). The promoter (`kg/proposal_promoter.py`, `_pod_uri_is_admissible`) accepts a pod endpoint iff it exists in `pod_store` AND its kind is admissible — otherwise it abandons that edge ("pod not admissible"). This check **replaces the dropped FK** from `kg_edge_metadata` to `kg_node_metadata` (pods are referenced by URI, not mirrored — see **kg_mirror**). Today only `image` is admissible; `email`/`chat_cluster` deliberately are not. Missing kinds fail closed. See [14_PODS.md](architecture/14_PODS.md).

### KG / Knowledge Graph

The structured-fact store. SQLite tables: `kg_node_metadata`, `kg_edge_metadata`, `kg_node_evidence`, `kg_edge_evidence`. Plus a parallel ChromaDB collection for embeddings. Owner-scoped via `ScopeContext.owner_id`.

### kg_mirror

**DELETED.** Formerly wrote a `node_type="Pod"` row into `kg_node_metadata` on every `PodStore.put`. Pods are now referenced by their `datapod:` URI string only — there is no Pod node, and the FK on `kg_edge_metadata.{source,target}_id → kg_node_metadata.id` was dropped to allow URI endpoints. `pod_store` is the sole source of truth for pod content; the **is_kg_admissible** gate in the promoter replaced the dropped FK. See [14_PODS.md](architecture/14_PODS.md).

### kg_node_id

Stable UUID identifying a node in `kg_node_metadata`. Foreign key from many places: `claim_proposal_node`, wiki page frontmatter, entity card binding, etc.

### kg_query

The raw read-only KG investigation tool (`lib/tools/kg_query/`): runs one `SELECT`/`WITH` SQL statement (or `PRAGMA table_info`) against the live `emi.db` graph + supporting tables, opened read-only at the SQLite level. Returns rows as a markdown table + structured `data.rows`. Used by the **investigator** to follow provenance, count, find duplicates. Distinct from **ask_kg** (embedding RAG Q&A) and from the retired `kg_query_manager` / `kg_team_manager`. `min_authority` 95.

### kg_pipeline

The chat → KG ingest pipeline. Bucket-per-stage architecture: `unified_log_2026` → `kg_resolved_message` → `kg_window` → `kg_window_extraction` → `kg_window_enrichment` → `claim_proposal*` → live KG. See [09_KG_PIPELINE.md](architecture/09_KG_PIPELINE.md).

### kg_revision_log

Audit table for KG mutations. Every `kg_mutator_tool` commit writes a row with `before_json`/`after_json` snapshots so changes can be reverted. See [13_KG_MUTATOR_TOOLS.md](architecture/13_KG_MUTATOR_TOOLS.md).

### locked (belief)

`user_beliefs.locked` — an integer flag (default 0); `1` marks an **owner correction** made through the `/beliefs` editor. A locked belief is durable: the `belief_updater`, the decay snapshot (`RecomputeBeliefSnapshot`), and canonicalization (`merge_verifier`) **must not modify, fade, flip, or merge it**. A mutating decision downgrades to `no_change` (evidence still attaches and counts advance, but statement and status stay as the owner set them); status transitions are gated by `COALESCE(locked,0)=0`; canonicalization excludes locked beliefs from both sides. The lock is v1's durability mechanism (v1 rows are otherwise mutable). See [16_BELIEF_ENGINE.md](architecture/16_BELIEF_ENGINE.md). (Distinct from a KG node's `locked_by_user_at`, the entity-card user-pin.)

### Manager

An orchestrator that owns an agent loop. Inherits from `MultiAgentManager` (most general) or `RoomManager` (deterministic routing for room flows). See [02_MANAGERS.md](architecture/02_MANAGERS.md).

### ManagerInvoker

The canonical entry point for invoking a manager. `DI.manager_invoker.invoke(manager, message)`. Runs `RequestPreprocessor.preprocess()` and `ScopeAdapter.apply()` before handing off to the manager.

### master_room

The primary chat UI room. Authority level 99 (full access). Has a special chat_gate variant that can delegate to the dayflow orchestrator.

### merge_verifier

The belief-dedup agent (`belief_engine::merge_verifier`, `gpt-5.2`) — the precision gate in Step 5 of the belief pipeline (`canonicalize_belief_set.py`). Two recall channels (embedding NN ≥ 0.80, and shared-distinctive-keyword) *propose* candidate duplicate **pairs**; `merge_verifier` *decides* each pair same/not-same and returns a reconciled `canonical_statement`. It is **asymmetric** — defaults to not-same, because a wrong merge silently destroys a distinct belief. On `same`, the better-supported belief survives (its statement rewritten to the canonical), the loser is deprecated (`merge_belief`), and the **belief_merges** redirect is recorded. Owner-**locked** beliefs are excluded. Replaced the dead chunk-based `belief_canonicalizer`. See [16_BELIEF_ENGINE.md](architecture/16_BELIEF_ENGINE.md) §4.

### Message

Pydantic class in `utils/pydantic_classes.py`. The unit of communication across the system: agent inputs/outputs, manager invocations, blackboard log entries, unified_log persistence. Carries `task`, `information`, `agent_input`, `scope_context`, `referenced_pods`, etc.

### min_authority

(Tools) The L1 see+use floor on a tool contract's `metadata` (0–100). Enforced at execution by `check_tool_access` via `resolve_tool_min_authority`: a tool is reachable only when `scope.approval.authority_level >= min_authority`. A first-party contract that **omits** it fails closed at 99; MCP/dynamic/core tools (no first-party contract) have no floor; authority `>= 100` clears every floor. Parsed and range-checked at boot — a bad value aborts load. Layer 2 of the **four-layer tool gate**. (Pods carry a same-named `min_authority` read floor — see **read_pod_gated**.)

### MultiAgentManager

Base class for agent-orchestrating managers. Lives in `manager_classes/MultiAgentManager.py`.

### Node

(KG) A row in `kg_node_metadata`. Has `id`, `label`, `node_type` (Entity / State / Event / Goal / Concept / Property), `category`, `aliases`, `description`, `original_sentence`, `start_date`, `end_date`, `start_date_prose`, `end_date_prose`, `valid_during`, `importance`, `goal_status`, `semantic_label`, `hash_tags`. Per-observation provenance (window, source message, derived sentence, merge action) lives in `kg_node_evidence` — JOIN through `node_id` rather than denormalizing onto the row. The legacy `window_id` / `original_message_id` / `sentence_id` columns were dropped 2026-05-04.

(Code) A class in `app/assistant/kg/db/knowledge_graph_db_sqlite.py`.

### NOW filter

`entity_card_v2.is_now_admissible(...)` — decides whether a connected KG node belongs on an **entity card** (the present) vs. only the wiki (the past). Rules in order: a `locked_by_user_at` lock always admits; explicit `valid_currently is False` rejects; a **State** admits iff still open (`end_date` is null/future); a **Goal** iff active/pending; an **Event** iff its category is in `DEFINITIONAL_EVENT_CATEGORIES` (birth, death, wedding, graduation, move, hire, …); a **Property** always; **Entity/Concept/Pod** never (link targets, not bullets). Applied during fact collection and re-applied on incremental refresh, so a state that just closed drops its bullets. See [12_ENTITY_CARDS.md](architecture/12_ENTITY_CARDS.md).

### noticer

The single LLM that runs the **Subconscious**'s "thinking" (`subconscious::noticer`, `gpt-5.4-mini`, **`allowed_tools: []`** — context fully pre-injected). Two passes: **inward** (verify existing concerns, process forced dispositions, drain the answered-question mailbox, detect new pattern-drift from friction signals) and **outward** (mandatory calendar anticipated-need scan + opportunity/gift scouting). Output applied by `persist.apply_noticer_output` to the **concerns register**; it *observes and delegates* (routes concerns to dayflow/proposers via `addressable_by`) — it never acts. Triggered nightly (04:00, window 04:00–22:00) plus a cooldown-guarded ad-hoc tick from **answer_matcher**. See [SUBCONSCIOUS.md](architecture/SUBCONSCIOUS.md) §3.

### per_manager

A scope-level tool rule: `scope.tools.per_manager[<manager>]` = `ScopeToolRule{allow, block}`, a **flat** dict that fires whenever that manager appears **anywhere** in the call tree (keyed on the *hosting* manager's name, not the room id or agent name). The surgical lever for restricting a specific manager's tool surface — e.g. a room confining its switchboard to `emi_team_manager`. Fail-closed; it **folds into `allowed_tools` at narrowing time**, so it binds at execution (`check_tool_access` reads `allowed_tools`), not just visibility. The lever that survives manager narrowing replacing a room's `allowed_tools`. See [SCOPE.md](architecture/SCOPE.md) §6, **four-layer tool gate**.

### pending questions

The SQLite queue of questions the assistant wants to ask (`pending_question` table; `pending_questions/store.py`) — the substrate for **all** proactive data-gathering, not just the **noticer**'s. Lifecycle `pending → asked → answered → closed` (or `expired`/`dismissed`). Each row carries `topical_tag`, `priority`, `ask_mode` (`chat` | `ticket`), `related_concern_id`. Two delivery bridges: an **in-chat nudge** (`pick_question_for_nudge`, a soft hint woven into `master_room::chat_gate`'s reply, budgeted 6/24h) and the **conversation_starter fast-path**; high-stakes (`severity=high` + near horizon) go out as an `ask_user` **ticket**. Answers close the loop via **answer_matcher**. See [SUBCONSCIOUS.md](architecture/SUBCONSCIOUS.md) §4.

### Pipeline

Sequential step-based code that runs once when invoked. Lives in `app/assistant/pipelines/<id>/`. Each step implements `inputs`, `outputs`, `run` on a shared `PipelineContext`. Idempotent (skips steps whose outputs exist unless `force=True`). See [06_PIPELINES_AND_ROUTINES.md](architecture/06_PIPELINES_AND_ROUTINES.md).

### plan.weekly_schedule

The single weekly source of truth the **Subconscious**'s **scheduler arbiter** mints — one pod aggregating all `intention.{meal,wellness,romantic}` proposals in `[today, +14d]` against the household calendar (hard constraints) and key dates. The body is grouped by day with `is_anchor` markers (locked constraints vs flex); `for_agents` names every proposer + meal planner so they "honor last week's plan" on the next run via `build_weekly_schedule_block()`. Conflicts the arbiter can't resolve become dayflow tickets (`suggestion_type="scheduler_conflict"`). See [SUBCONSCIOUS.md](architecture/SUBCONSCIOUS.md) §8.

### Planner

(1) The Python class `agent_classes/Planner.py` — multi-step plan-validation variant of `Agent`. Less common than people expect; many "planner" agents use `class_name: Agent` in their config. (2) The role of an agent that emits a structured plan, regardless of which class backs it.

### pod

A URI-addressable content unit. Code uses **datapod**. Has a stable URI like `datapod://unified_log/<id>` or `datapod://resource/<name>`, plus `one_liner` summary and optional body. Lets agents pass references instead of full text. See [14_PODS.md](architecture/14_PODS.md).

### pod_classifier

LLM agent (model: gpt-5.4-mini) that examines a freshly intaked event and produces a structured classification (one_liner, kind, etc.) for pod minting.

### pod_search / pod_fetch

Agent-facing tools. `pod_search` returns matching pod ids; `pod_fetch` hydrates a pod id to its body.

### proposal_promoter

Nightly routine (`02:30`) that drains pending `claim_proposal` rows into the live KG. Also stamps TTL on State/Event nodes via `state_ttl_estimator` and rewrites their `original_sentence` to canonical form via `fact_canonicalizer`. Holds `datapod:` edge endpoints to the **is_kg_admissible** gate (`_pod_uri_is_admissible`), which replaced the dropped node FK.

### prompts/

A directory under each agent's directory containing Jinja2 templates: `system.j2` (role definition), `user.j2` (request context), optional `description.j2`. Variables resolved by `ContextInjector` from blackboard / resource manager / inbound Message.

### pull_set

A consumer's belief-retrieval tag set, declared in `pull_sets:` in `configs/belief_tags.yaml`. A belief surfaces for the consumer if it carries **any** tag in the set. Live sets: `meal_engine`, `health_status`, `entertainment`, `routine_stage`. **Bridge tags** (`dietary`, `family`, `social`, `meal`) let a consumer reach beliefs filed under a different primary **domain** (e.g. the health consumer reaches a `food`-domain belief via the `dietary` bridge). Pulled via `beliefs_for_context(tags=…)` and `belief_engine.tagging.pull_set(name)`. See **belief tag vocabulary**, [16_BELIEF_ENGINE.md](architecture/16_BELIEF_ENGINE.md) §7.

### read_pod_gated

The universal pod-body access gate (`pod_store/pod_utils.read_pod_gated(pod_id, scope)`) — the **one** place both walls are composed, so no surface re-rolls the logic. Callers: `pod_fetch`, the `/pod expand` slash command, the `/api/pods` route. **Scope wall** (`resolve_allowed_scopes`): the pod's `scope_id` must be in the caller's `scope.pods.allowed_scopes` (default `["self"]` = own room; `["all"]` = owner cross-room surface) — a None scope is a trusted system read (unrestricted, authority skipped). **Authority wall** (`authority.py`): the caller's `authority_level` must clear the pod's `min_authority`. `PodNotFound` covers both missing and out-of-scope (indistinguishable on purpose). See **pod authority bands**, [14_PODS.md](architecture/14_PODS.md).

### pod authority bands

The five named read floors a pod's `min_authority` can take (`pod_store/authority.py`), reusing the tool authority axis: **10** public (display-only `redacted`/`format`), **50** chat surface (`AUTH_CHAT`, default for content pods), **70** gated chat (sensitive-but-shareable), **99** user-equivalent, **100** courier-only. The 99/100 cap is the wall: **no LLM agent reads a 100-band projection** — only the deterministic courier does. `check_authority` raises `PodAuthorityError` (fail-closed to 0) on a shortfall. The body-sensitivity axis, orthogonal to the **scope wall** (cross-room privacy). See **read_pod_gated**.

### RecomputeBeliefSnapshot

Step 3 of the belief pipeline (`RecomputeBeliefSnapshotStep` → `belief_engine/decay/recompute.py`) — no LLM, idempotent, **universal** (it replaced the old `DecayStaleBeliefsStep`/`decay_enabled` time-threshold path in 2026-05-11). For each active belief it decays the `belief_evidence` rows by the belief **kind**'s half-life, sums evidence-weighted support/contradiction, derives a **confidence band**, and writes back the four `current_*` columns + `last_contradicted_at`. `faded` → `deprecated`; `contested` / `deprecated_by_contradiction` → `contested` + queued for reevaluation. All transitions gated by `COALESCE(locked,0)=0`. See [16_BELIEF_ENGINE.md](architecture/16_BELIEF_ENGINE.md) §4–5.

### research_finding

A durable web-research result minted as a `kind="research_finding"` **pod** by the web planner (`Planner._mint_research_findings`); id is `canonical_pod_id("research_finding", run, unit)` (deterministic per `(run, unit)`, so re-emitting upserts the same pod). Source URLs ride in `metadata.source_urls`; `scope_id` is the originating room. Pod ids flow into the message stream and hydrate downstream like any URI; they surface to the user via the `/pod expand` slash command and `/api/pods` ("Saved findings") — the only kind in `DISPLAYABLE_POD_KINDS`. Not `kg_admissible`. See [14_PODS.md](architecture/14_PODS.md).

### ResourceManager

Service exposed via `DI.resource_manager` that loads JSON resource files from `resources/` and exposes them by id (`resource_user_data`, `resource_routine_status`, etc.). Agents declare resources they need in `system_context_items` / `user_context_items`.

### Room

A scoped conversation channel — a directory under `app/assistant/rooms/<room_id>/` whose config is a single **ROOM.md** (identity/policy/permissions/access) plus an optional `scope.yaml`. See [03_ROOMS.md](architecture/03_ROOMS.md).

### ROOM.md

The single config file for a **room** (`app/assistant/rooms/<room_id>/ROOM.md`) — YAML frontmatter (three required mappings: `policy`, `permissions`, `access`) plus a markdown body whose H1 sections map to the blackboard keys agents read at prompt time (`# Identity` → `room_identity`, `# Conversation` → `room_conversation`, `# Safety` → `room_safety`, …). The canonical contract is `rooms/ROOM_CONTRACT.md`. Pre-2026-05-09 each room was 7–9 loose JSON files (`policy.json`, `permissions.json`, `access.json`, plus `resource_*.json` wrappers); that shape collapsed into one `ROOM.md` with no backwards-compat fallback (the loose `.json` files may linger on disk but the loader reads only `ROOM.md`). A missing/malformed `ROOM.md` or missing `# Identity` raises loudly. An optional sibling **scope.yaml** is authoritative for the permission bucket. See [03_ROOMS.md](architecture/03_ROOMS.md).

### RoomManager

Manager class extending `MultiAgentManager` with deterministic state-map routing and a max-cycle limit. The default class for room-driven flows.

### RoomSessionManager

The transport-abstraction layer. Receives messages from any surface (UI / SMS / Slack / Telegram) → `InboundEnvelope` → loads room context → invokes manager → formats outbound reply for the surface. ~1600 LOC in `app/assistant/room_session_manager/`.

### Routine

A schedule definition — **one JSON file per routine**, `<id>.json` under `configs/routines/{public,private}/` (the old monolithic `configs/routines.json` now holds settings only; private wins on id collision). Tells RoutineManager when to fire something (a `trigger`) and what to fire (a `runner`: one of `tool`, `task`, `job`, `function`, `pipeline` — `function` is now dominant). Reloaded every refresh tick — edit, save, no restart. See **trigger**, **active_window**, **on_error**, **@routine_handler**, [06_PIPELINES_AND_ROUTINES.md](architecture/06_PIPELINES_AND_ROUTINES.md).

### trigger

A routine's firing condition (`RoutineConfig.trigger`). Two types: **`time`** — `{policy, active_window}`, evaluated each ~60s refresh tick; **`event`** — `{topic}`, fires on an `event_hub` publish (subscribed once per process, skipped by the polling loop; e.g. `camera_dispatch` on `ring_snapshot_captured`). Time triggers carry one of **three** scheduling policy types — `interval` / `daily` / `weekly` (there is **no** `quiet_hours` policy; time-of-day restriction is the **active_window**). An entry with only a legacy `run_policy` is treated as a `time` trigger.

### active_window

The time-of-day gate on a routine's **trigger** — a named window from `configs/windows.json` (`sleep`, `work_hours`, `daytime`, `kg_active`, …) or an inline `{"from","to","local","weekdays_only"}`. Checked **first** in `_should_run`, before any policy — outside the window the routine is skipped (`outside active window`). Wraps midnight automatically (`from > to`); re-resolved each tick so a hot edit takes effect without restart. Replaces the old `quiet_hours` policy notion.

### on_error

A routine's failure-handling block. Defaults: `{max_failures: 3, backoff_base_seconds: 60, backoff_max_seconds: 3600, then: "disable_with_ticket", auto_retry_after_seconds: 0}`. Each consecutive failure increments the streak and pushes `next_attempt_after_utc` out by exponential backoff; success resets it. On the `max_failures`-th failure, `then="disable_with_ticket"` writes `enabled=false` to the status file and raises a `dayflow_notify` ticket (`log_only` keeps it enabled). `auto_retry_after_seconds > 0` enables a self-heal probe.

### @routine_handler

The autodiscovery decorator for `function`-runner routines (`routine_handlers/__init__.py`). Drop `app/assistant/routine_handlers/<name>.py`, decorate a function with `@routine_handler()` (or `name="alias"`), and `discover_handlers()` walks the package at import time and folds it into `ROUTINE_FUNCTION_REGISTRY` under its name — no edit to `routine_functions.py`. Explicit opt-in (a module can import helpers without exposing them); a hand-registered entry wins on a name collision. Handlers receive `target_date=` / `routine=` / `event_message=` and should raise on failure so **on_error** can back off.

### RoutineManager

Service that globs `configs/routines/{public,private}/*.json` every refresh tick (~60s), evaluates each routine's **trigger** (window + policy) + guards, and fires the runner when ready. Persists runtime state (per-routine `enabled`, `last_*`, failure streak, backoff) to `resources/resource_routine_status.json` — the status file's `enabled` overrides the spec default, so user toggles survive repo pulls.

### scheduler arbiter

The **Subconscious** agent (`scheduler_arbiter`, `gpt-5.1`) that synthesizes all the proposer lanes' **intention.* pods** into one weekly **plan.weekly_schedule** — the single weekly source of truth. Reads the household calendar as hard constraints + key dates, resolves what conflicts it can (`conflicts_resolved`), and surfaces the rest as dayflow tickets (`conflicts_for_user`). Runs daily at 05:30, after the three proposers. See [SUBCONSCIOUS.md](architecture/SUBCONSCIOUS.md) §8.

### scope_contract

A YAML block on a manager config that narrows the inbound `ScopeContext`. Can only narrow, never widen — a key load-bearing rule. See [15_EMI_TEAM_AND_SCOPE.md](architecture/15_EMI_TEAM_AND_SCOPE.md).

### ScopeContext

The permission envelope on every Message. Fields: `scope_id`, `owner_id`, `actor_id`, `surface`, `room_id`, plus four sub-policies (`approval`, `resources`, `writes`, plus tool-related). See [15_EMI_TEAM_AND_SCOPE.md](architecture/15_EMI_TEAM_AND_SCOPE.md).

### scope_gate / requires_scope

The shared "scope is the key, the thing carries the lock" primitive (`app/assistant/utils/scope_gate.py`). A **skill** or **resource** may declare a `requires_scope` lock — a dict of *identity* fields (`acting_as` / `surface` / `room_id` / `room_context_id` / `visibility`). The lock passes iff **every** field matches the live scope (canonicalized per-field; `acting_as=self` resolves to the configured assistant name). An empty/absent lock passes trivially — **no lock = free**. Identity fields only; permission fields (authority, tools, pods) are never gateable here. Extracted from `SkillInjector` so skills and resources gate identically.

### section_tagging

The shared promotion-time tagging layer (`app/assistant/kg/section_tagging.py`) that decides which card/wiki section a KG fact belongs to. Tags are written **once at promotion time** (by `kg_node_section_tagger`) and read by **both** the entity-card builder and the wiki page builder — neither re-tags at projection time. `NodeSectionTag` (table `kg_node_section_tag`): `(node_id, namespace, section_name)` + `tagger_version` + drop-tracking columns. Namespaces: `NAMESPACE_CARD = "card"` and `NAMESPACE_WIKI = "wiki"` (plus a `_processed` sentinel so reject/empty results aren't re-sent to the LLM). `CARD_SECTION_VOCAB` / `WIKI_SECTION_VOCAB` bound which keys the tagger may emit per namespace. **Drop-tracking** (try-and-mark): a fact the renderer rejected is re-admitted only when the node's content hash changes or the builder version bumps. See [12_ENTITY_CARDS.md](architecture/12_ENTITY_CARDS.md).

### Sensei

The new name for the analyst + spec_refiner pair (renamed 2026-04-26). Reviews task execution traces and produces refined task specs.

### ServiceLocator

The DI container. Lives in `app/assistant/ServiceLocator/service_locator.py`. Bootstrap happens in `app/bootstrap.py`. Access via `from app.assistant.ServiceLocator.service_locator import DI`.

### Shadow KG

The `claim_proposal*` staging layer where every proposed change waits before being promoted into the canonical KG (`kg_node_metadata` + `kg_edge_metadata`). Used interchangeably with "proposal layer". Reviewable through the `/kg-proposals/` admin UI: list, detail with graph viz, manual `approve` / `reject` per proposal. The auto-promoter routine drains pending proposals nightly. See `docs/architecture/09_KG_PIPELINE.md` for the full lifecycle.

### SignalRouter

The reactive event-publication sibling of the gut. Subscribes to incoming events, fires synchronous side-effects.

### state_map

A dict on a manager config mapping `last_agent → next_agent` (or list of candidates). Defines routing through the agent loop in a deterministic way. Used by `RoomManager` and emi_team-derived managers.

### Subconscious

EmiOS's autonomous background "mind" (`app/assistant/subconscious/`). While the user is away it maintains the durable **concerns register**, turns concerns into **proposals** (**intention.* pods**) and **questions** (the **pending questions** queue), surfaces a daily **digest**, and feeds proactive outreach. The **noticer** is the only LLM that mutates concern lifecycle; proposer lanes (meal/wellness/romantic) mint intention pods and the **scheduler arbiter** synthesizes them into **plan.weekly_schedule**. Nothing it produces acts on the world directly — it observes, proposes, and asks. Authority 99. See [SUBCONSCIOUS.md](architecture/SUBCONSCIOUS.md).

### synthetic-fact drain

The wiki subsystem's confirm-loop for inferred facts (`wiki_generator/synthetic_fact_drain.py`, `run_fact_drain`). The `wiki_connection_investigator` infers facts the prose implies but the KG lacks and files them as `synthetic_fact_proposal` **findings**; the drain gates each (trivia is dismissed by the worthiness gate), turns worthy ones into natural confirmation **questions** (≤2/run, riding the standard pending-question + answer-capture loop), and on confirmation promotes them to `auto_apply` findings the executor materializes after a grace window. Routine `wiki_fact_drain`, window from 08:00 (so morning noticer/digest questions get priority). See [11_WIKI_GENERATOR.md](architecture/11_WIKI_GENERATOR.md), [22_KG_HEALTH_COMPONENTS.md](architecture/22_KG_HEALTH_COMPONENTS.md).

### TaskRunner

A routine runner type that executes a task spec. Either invokes a manager to run the spec live, or executes a `compiled_file` directly for a pre-planned task IR.

### Ticket

A row in the tickets table managed by `TicketManager`. State machine: `pending → proposed → accepted | dismissed | snoozed | expired`. Terminal states: completed, dismissed, expired, failed.

### tool_caller

The canonical control node that resolves an `action` field on the blackboard and dispatches it. Handles tools, agent-to-agent invocation (with call-context push/pop), MCP tools, approval flows. ~700 LOC.

### Tool

A capability invoked from agents indirectly. Lives in two places: `lib/tools/<name>/` (entry-point wrapper with contract + arg forms + prompts) and `lib/core_tools/<name>/` (implementation). Auto-registered by the tool registry. See [07_TOOLS.md](architecture/07_TOOLS.md).

### unified_log_2026

The immutable source-of-truth event log table. Every chat message, every email, every dayflow item lands here. Source for the KG pipeline (filtered to chat-eligible rows by the resolver).

### Wiki

When code or prose says "wiki" without a qualifier, it means **EmiPedia** — the user-facing personal-life Markdown vault EmiOS generates from the knowledge graph (default `<your-home>/EmiWiki`), browseable at `/wiki/`, produced by the wiki_generator subsystem. Things like `wiki_writer.py`, `wiki_renderer.py`, the `wiki_nightly_refresh` routine, and `kg_maintenance_finding.finding_type='wiki_contradiction'` all refer to EmiPedia, not these docs.

### window

(KG) A row in `kg_window`. A coherent conversation unit (one topic) — the atomic unit of work for the KG pipeline.

### wiki_contradiction

A `finding_type` raised by the wiki consistency_critic when a generated wiki page disagrees with the KG, the user's profile, or an entity card. Triggers an investigator → mutator pass.

### writer

Generic role of an agent that produces user-visible prose: `wiki_writer`, `wiki_lead_writer`, `daily_summary::writer`, `ticket_writer`, `task_spec_writer`, etc. Each one has its own directory and prompt.
