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

### authority_level

An integer on `ScopeApprovalPolicy` that gates approval-required tools. Master room is 99; most other rooms are lower. Tools whose `requires_approval` minimum exceeds the scope's authority level get blocked.

### Blackboard

Per-invocation scoped state stack owned by a manager. Has a global scope plus pushable local scopes (created when one agent calls another). Methods: `get_state_value(key)` reads top-down, `update_state_value(key, val)` writes to current top scope, `add_msg(message)` appends to message log.

Distinct from **Global Blackboard**.

### canonical sentence

The present-tense form of a State/Event/Goal node's `original_sentence` field. Set at promotion time by the `fact_canonicalizer` agent. Validity dates live in `start_date`/`end_date`/`valid_during` rather than in the sentence itself. See `project_present_tense_canonical_principle.md` (memory).

### chat_gate

The first agent in a room manager's loop. Decides per turn: reply directly, hand off to the switchboard, or (master_room only) delegate to dayflow.

### claim_proposal

A row in `claim_proposal` plus its sibling tables (`claim_proposal_node`, `claim_proposal_edge`, `claim_proposal_evidence`). Represents a connected subgraph of nodes/edges produced by the **KG Pipeline** that is *pending* promotion into the live KG. The promoter routine consumes these. See also: **Shadow KG**, **proposal_promoter**.

### compiled task

A pre-planned task IR (intermediate representation) saved as JSON under `tasks/<task_id>/<compiled_file>.json`. Lets a routine skip re-planning. Used by `morning_briefing` and `activity_log`. Steps can pin specific tools (`pinned_tools`) to bypass narrowing.

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

### Dojo

The new name for `practice_runner` (renamed 2026-04-26). The harness that runs a task spec end-to-end against a manager for evaluation.

### Edge

A directed connection between two **Nodes** in the KG. Stored in `kg_edge_metadata`. Has `source_id`, `target_id` (FK to `kg_node_metadata` with `ondelete='CASCADE'` — relevant for the merge_nodes flush ordering), `relationship_type`, `sentence`, `window_id`.

### emi_team_manager

The general-purpose worker manager. Shape: delegator → planner → tool_caller (loop with critic) → summary → final_answer. Specialized managers reuse its delegator + summary while swapping in their own planner + final_answer. See [15_EMI_TEAM_AND_SCOPE.md](architecture/15_EMI_TEAM_AND_SCOPE.md).

### EmiPedia

The user-facing personal-life wiki — Markdown pages about people, places, events that EmiOS generates from the knowledge graph into a vault on disk (default `<your-home>/EmiWiki`). Browseable at `/wiki/`. When prose or code says "the wiki" without qualification, it means EmiPedia.

### Entity Card

A row in the `entity_cards` SQLite table. Structured profile (summary + key_facts + contact_info + …) for a person/place/thing. Generated by the entity_cards pipeline from the KG; edited via `/entity_cards`. Distinct from the long-form **wiki page** (EmiPedia entry) for the same entity. See [12_ENTITY_CARDS.md](architecture/12_ENTITY_CARDS.md).

### entity card maintenance

Weekly pipeline (`entity_card_maintenance_pipeline`) that scans cards for quality issues (broken KG link, junk name, blank content, low confidence, stale, no KG link) and writes findings to `entity_card_maintenance_finding`.

### entity_cards table

SQLite table that stores **entity cards**. One row per entity card.

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

(Entity card maintenance) A row in `entity_card_maintenance_finding` (same lifecycle, different fields).

### Global Blackboard

Process-wide message log accessed via `DI.global_blackboard`. Survives across manager invocations. Distinct from a manager's per-invocation **Blackboard**.

### the gut

The intake pipeline (named `IngestService` in code) that fans out incoming events to two siblings: `SignalRouter` (reactive event publication) and `PodClassifier` (declarative pod minting). See [14_PODS.md](architecture/14_PODS.md).

### handoff_tf

A boolean field set by chat_gate. `true` → chat_task_router routes to switchboard. `false` → reply directly.

### InboundEnvelope

Transport-agnostic representation of an inbound message, built by `RoomSessionManager` from whatever surface (UI / SMS / Slack / Telegram) the message came from.

### investigation report

The structured output of `kg_investigation_manager`: `{diagnosis, evidence, proposed_action, open_questions}`. Stored in `kg_maintenance_finding.investigation_report_json`.

### investigator

The `kg_investigation_manager` — read-only KG investigator that converts a pending finding into an investigation report. Uses the `kg_query` tool with an allowlist.

### KG / Knowledge Graph

The structured-fact store. SQLite tables: `kg_node_metadata`, `kg_edge_metadata`, `kg_node_evidence`, `kg_edge_evidence`. Plus a parallel ChromaDB collection for embeddings. Owner-scoped via `ScopeContext.owner_id`.

### kg_node_id

Stable UUID identifying a node in `kg_node_metadata`. Foreign key from many places: `claim_proposal_node`, wiki page frontmatter, entity card binding, etc.

### kg_pipeline

The chat → KG ingest pipeline. Bucket-per-stage architecture: `unified_log_2026` → `kg_resolved_message` → `kg_window` → `kg_window_extraction` → `kg_window_enrichment` → `claim_proposal*` → live KG. See [09_KG_PIPELINE.md](architecture/09_KG_PIPELINE.md).

### kg_revision_log

Audit table for KG mutations. Every `kg_mutator_tool` commit writes a row with `before_json`/`after_json` snapshots so changes can be reverted. See [13_KG_MUTATOR_TOOLS.md](architecture/13_KG_MUTATOR_TOOLS.md).

### Manager

An orchestrator that owns an agent loop. Inherits from `MultiAgentManager` (most general) or `RoomManager` (deterministic routing for room flows). See [02_MANAGERS.md](architecture/02_MANAGERS.md).

### ManagerInvoker

The canonical entry point for invoking a manager. `DI.manager_invoker.invoke(manager, message)`. Runs `RequestPreprocessor.preprocess()` and `ScopeAdapter.apply()` before handing off to the manager.

### master_room

The primary chat UI room. Authority level 99 (full access). Has a special chat_gate variant that can delegate to the dayflow orchestrator.

### Message

Pydantic class in `utils/pydantic_classes.py`. The unit of communication across the system: agent inputs/outputs, manager invocations, blackboard log entries, unified_log persistence. Carries `task`, `information`, `agent_input`, `scope_context`, `referenced_pods`, etc.

### MultiAgentManager

Base class for agent-orchestrating managers. Lives in `manager_classes/MultiAgentManager.py`.

### Node

(KG) A row in `kg_node_metadata`. Has `id`, `label`, `node_type` (Entity / State / Event / Goal / Concept / Property), `category`, `aliases`, `description`, `original_sentence`, `start_date`, `end_date`, `start_date_prose`, `end_date_prose`, `valid_during`, `importance`, `goal_status`, `semantic_label`, `hash_tags`. Per-observation provenance (window, source message, derived sentence, merge action) lives in `kg_node_evidence` — JOIN through `node_id` rather than denormalizing onto the row. The legacy `window_id` / `original_message_id` / `sentence_id` columns were dropped 2026-05-04.

(Code) A class in `app/assistant/kg/db/knowledge_graph_db_sqlite.py`.

### Pipeline

Sequential step-based code that runs once when invoked. Lives in `app/assistant/pipelines/<id>/`. Each step implements `inputs`, `outputs`, `run` on a shared `PipelineContext`. Idempotent (skips steps whose outputs exist unless `force=True`). See [06_PIPELINES_AND_ROUTINES.md](architecture/06_PIPELINES_AND_ROUTINES.md).

### Planner

(1) The Python class `agent_classes/Planner.py` — multi-step plan-validation variant of `Agent`. Less common than people expect; many "planner" agents use `class_name: Agent` in their config. (2) The role of an agent that emits a structured plan, regardless of which class backs it.

### pod

A URI-addressable content unit. Code uses **datapod**. Has a stable URI like `datapod://unified_log/<id>` or `datapod://resource/<name>`, plus `one_liner` summary and optional body. Lets agents pass references instead of full text. See [14_PODS.md](architecture/14_PODS.md).

### pod_classifier

LLM agent (model: gpt-5.4-mini) that examines a freshly intaked event and produces a structured classification (one_liner, kind, etc.) for pod minting.

### pod_search / pod_fetch

Agent-facing tools. `pod_search` returns matching pod ids; `pod_fetch` hydrates a pod id to its body.

### policy.json

A required file in every room directory (`app/assistant/rooms/<room_id>/policy.json`). Specifies manager, surface, default visibility, authority level, retention rules.

### proposal_promoter

Nightly routine (`02:30`) that drains pending `claim_proposal` rows into the live KG. Also stamps TTL on State/Event nodes via `state_ttl_estimator` and rewrites their `original_sentence` to canonical form via `fact_canonicalizer`.

### prompts/

A directory under each agent's directory containing Jinja2 templates: `system.j2` (role definition), `user.j2` (request context), optional `description.j2`. Variables resolved by `ContextInjector` from blackboard / resource manager / inbound Message.

### ResourceManager

Service exposed via `DI.resource_manager` that loads JSON resource files from `resources/` and exposes them by id (`resource_user_data`, `resource_routine_status`, etc.). Agents declare resources they need in `system_context_items` / `user_context_items`.

### Room

A scoped conversation channel — a directory under `app/assistant/rooms/<room_id>/` with required identity/policy/permissions/access files. See [03_ROOMS.md](architecture/03_ROOMS.md).

### RoomManager

Manager class extending `MultiAgentManager` with deterministic state-map routing and a max-cycle limit. The default class for room-driven flows.

### RoomSessionManager

The transport-abstraction layer. Receives messages from any surface (UI / SMS / Slack / Telegram) → `InboundEnvelope` → loads room context → invokes manager → formats outbound reply for the surface. ~1600 LOC in `app/assistant/room_session_manager/`.

### Routine

A schedule definition in `configs/routines.json`. Tells RoutineManager when to fire something and what to fire (a tool, task, job, function, or pipeline). Five scheduling policy types: interval, daily, weekly, quiet_hours, plus disabled. See [06_PIPELINES_AND_ROUTINES.md](architecture/06_PIPELINES_AND_ROUTINES.md).

### RoutineManager

Service that reads `configs/routines.json` every refresh tick (~60s), evaluates each routine's policy + guards, and fires the runner when ready. Persists state to `resources/resource_routine_status.json`.

### scope_contract

A YAML block on a manager config that narrows the inbound `ScopeContext`. Can only narrow, never widen — a key load-bearing rule. See [15_EMI_TEAM_AND_SCOPE.md](architecture/15_EMI_TEAM_AND_SCOPE.md).

### ScopeContext

The permission envelope on every Message. Fields: `scope_id`, `owner_id`, `actor_id`, `surface`, `room_id`, plus four sub-policies (`approval`, `resources`, `writes`, plus tool-related). See [15_EMI_TEAM_AND_SCOPE.md](architecture/15_EMI_TEAM_AND_SCOPE.md).

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
