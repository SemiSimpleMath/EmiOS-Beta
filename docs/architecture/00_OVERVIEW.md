# EmiOS Architecture Overview

EmiOS is a local-first personal AI assistant built on Flask + SQLite + ChromaDB. It uses a multi-agent orchestration system with knowledge-graph memory, event-driven scheduling, and multi-transport communication (UI, SMS, Slack, Telegram).

## Layered Architecture

```
Transport Layer (UI/WebSocket, SMS, Slack, Telegram)
    |
Room Session Manager (transport abstraction, session modes, persistence)
    |
Manager Layer (MultiAgentManager — agent loop; scope applied at ingress)
    |
Agent Layer (LLM decision units with structured output)
    |
Control Nodes (deterministic routing, tool dispatch, flow control)
    |
Tool Layer (registered tools w/ Pydantic schemas; four-layer scope + authority + approval gate)
    |
Service Layer (ServiceLocator/DI, EventHub, Blackboard, ResourceManager)
```

## Core Concepts

### Agents
LLM decision units. Each agent has a `config.yaml`, Jinja2 prompt templates (`system.j2`, `user.j2`), and an optional Pydantic `agent_form.py` for structured output. `Agent` is now a thin orchestrator over injected per-agent runtime services (context injection, prompt building, tool-policy resolution, result application). Agents don't execute actions — they produce decisions that control nodes and tools act on.

See: [01_AGENTS.md](01_AGENTS.md)

### Managers
Orchestrators that run agent loops. `MultiAgentManager` is the one class (rooms included); routing is deterministic via the `Delegator` agent's `flow_config.state_map` lookup. Other (service) managers handle infrastructure: `BackgroundTaskManager`, `RoutineManager`, `TicketManager`.

See: [02_MANAGERS.md](02_MANAGERS.md), [10_SERVICE_MANAGERS.md](10_SERVICE_MANAGERS.md)

### Rooms
Scoped conversation channels. Each room is a single `ROOM.md` (frontmatter policy/permissions/access + body→blackboard mapping) plus a `scope.yaml`. `master_room` is the primary UI (authority 99, owner-only) with dayflow delegation; other surfaces have limited authority.

See: [03_ROOMS.md](03_ROOMS.md)

### Control Nodes
Deterministic (non-LLM) nodes in the agent loop — routing, tool dispatch, prep/persist, normalization, exit. `ToolCaller` is the canonical dispatcher for tools and agent-to-agent calls.

See: [04_CONTROL_NODES.md](04_CONTROL_NODES.md)

### Dayflow Orchestrator
An autonomous daily workflow engine that ingests chat, email, delegations, and pods, maintains persistent lifecycle state per item, and runs a multi-agent pipeline to triage, plan, and dispatch. Tickets are tools (`create_dayflow_ticket`), not items.

See: [05_DAYFLOW.md](05_DAYFLOW.md)

### Pipelines & Routines
Step-based pipelines (sequential, idempotent) for background processing, and a routine scheduler (one file per routine under `configs/routines/public/`, time- or event-triggered, with on_error/backoff/auto-disable).

See: [06_PIPELINES_AND_ROUTINES.md](06_PIPELINES_AND_ROUTINES.md), [24_ROUTINE_INVENTORY.md](24_ROUTINE_INVENTORY.md)

## Memory, Knowledge & Autonomy

### Knowledge Graph
A temporal autobiographical knowledge graph (SQLite + ChromaDB): entities, states, events, goals, and provenance-bearing edges. A nightly pipeline turns chat/insights into proposals the promoter writes; maintenance routines heal duplicates, importance, dates, and disambiguation. Agents read it via the `ask_kg` tool. It's rendered for humans as a per-entity **wiki** and structured **entity cards** (a "what's true now" snapshot).

See: [09_KG_PIPELINE.md](09_KG_PIPELINE.md), [11_WIKI_GENERATOR.md](11_WIKI_GENERATOR.md), [12_ENTITY_CARDS.md](12_ENTITY_CARDS.md), [13_KG_MUTATOR_TOOLS.md](13_KG_MUTATOR_TOOLS.md), [22_KG_HEALTH_COMPONENTS.md](22_KG_HEALTH_COMPONENTS.md), [23_NODE_IMPORTANCE.md](23_NODE_IMPORTANCE.md)

### Belief Engine
Derives confidence-tracked beliefs about the user from daily insights + ticket signals — stable belief keys, LLM evolve-in-place, evidence-weighted decay, pairwise-verifier dedup, tagging, stable short-ids, and a ranked tag-scoped retrieval API. Exported to `resource_user_beliefs.json` that agents read.

See: [16_BELIEF_ENGINE.md](16_BELIEF_ENGINE.md)

### Subconscious
The autonomous background "mind": a concerns register, proactive outreach (questions woven into chat), a daily digest, and proposer/arbiter lanes (meal, wellness, romantic, scheduling).

See: [SUBCONSCIOUS.md](SUBCONSCIOUS.md), [MEAL_PLANNING.md](MEAL_PLANNING.md)

### Pods
Typed, URI-addressable artifacts (`datapod:kind:id`) — chat clusters, images, emails, research findings, secrets. Agents pass references, not bytes; the KG links to pods by URI (no mirror); reads go through a scope + authority gate.

See: [14_PODS.md](14_PODS.md), [14b_PODS_MEDIA_LIFECYCLE.md](14b_PODS_MEDIA_LIFECYCLE.md)

### Scope, Skills & Secrets
Capability contracts per source — a four-layer tool gate (allowed-tools ceiling → visibility → authority floor → approval), injected skills gated by the same `scope_gate` primitive, and a locked secrets/accounts model (pods-as-pointers, courier@100).

See: [SCOPE.md](SCOPE.md), [15_EMI_TEAM_AND_SCOPE.md](15_EMI_TEAM_AND_SCOPE.md), [21_SKILLS.md](21_SKILLS.md), [SECRETS_ACCOUNTS.md](SECRETS_ACCOUNTS.md)

### Tools, Resources & Service Layer
Tools are registered capabilities with Pydantic schemas behind the scope/authority gate. Resources are JSON/derived context files agents read (scope-gated; some computed dynamically). The service layer is the DI container + EventHub + Blackboard + ResourceManager, brought up in a two-phase bootstrap.

See: [07_TOOLS.md](07_TOOLS.md), [19_RESOURCES.md](19_RESOURCES.md), [17_SERVICE_LAYER.md](17_SERVICE_LAYER.md), [18_TRANSPORTS.md](18_TRANSPORTS.md)

## Key Coordination Mechanisms

| Mechanism | Purpose | Access Pattern |
|-----------|---------|----------------|
| **ServiceLocator (DI)** | Dependency injection | `DI.event_hub`, `DI.tool_registry`, etc. |
| **EventHub** | Async pub-sub messaging | `event_hub.publish(message)` / `register_event(key, handler)` |
| **Blackboard** | Scoped shared state within a manager | `blackboard.get_state_value(key)` / `update_state_value(key, val)` |
| **Global Blackboard** | Cross-manager message history | `DI.global_blackboard` |
| **ResourceManager** | Scope-gated resource state (JSON/derived) | `resource_manager.get_resource(scope_context=..., resource_id=...)` |
| **Singletons** | Shared manager instances | `get_background_task_manager()`, `get_routine_manager()`, etc. |

## Request Flow (Master Room Example)

```
User message via WebSocket
  -> RoomSessionManager (build transport-agnostic InboundEnvelope)
    -> Load room context (ROOM.md identity/policy/permissions) + scoped history
    -> ManagerInvoker.invoke(master_room_manager, message)
      -> RequestPreprocessor -> ScopeAdapter.apply() (scope narrowed at ingress)
      -> MultiAgentManager loop (deterministic state_map):
         chat_gate (reply | handoff | dayflow_delegate)
           -> switchboard (on handoff) -> ToolCaller (tool/agent dispatch + gate + approval)
           -> ToolResultHandler -> final_answer
    -> RoomSessionManager persists + delivers the reply via the transport
```

## Directory Map

```
belief_engine/             # Belief inference engine (top-level package; tables live in emi.db)

app/assistant/
  agent_classes/           # Base agent classes (Agent, Planner, OneShotAgent, Delegator, ...)
  agent_registry/          # Agent loading + factory
  agent_runtime/services/  # Per-agent runtime services (context injector, prompt builder, tool policy, ...)
  agents/                  # Agent definitions (config.yaml + prompts/ + agent_form.py)
  background_task_manager/ # Daemon threads (db cleanup, watchdog, routine runner, ticket maintenance)
  control_nodes/           # Deterministic routing/dispatch (incl. ToolCaller)
  dayflow_orchestrator/    # Autonomous daily workflow engine
  entity_management/       # Entity cards v2 (sections/bullets)
  importance/              # Node/edge importance derivation + lens consumers
  kg_core/  kg/  kg_maintenance/   # KG model / write path (promoter) / healing routines
  lib/
    blackboard/            # Scoped shared state
    tool_registry/         # Tool loading
    tool_execution/        # Four-layer tool access gate + approval
    tools/  core_tools/    # Tool wrappers + implementations
  manager_classes/         # MultiAgentManager
  manager_runtime/         # Manager invocation pipeline + ScopeAdapter
  pipelines/               # Step-based background processing (daily_insights, kg_pipeline, ...)
  pod_store/               # Pods: store, classifier, materializers, ingest
  room_session_manager/    # Transport abstraction + ~24 room services
  rooms/                   # Room definitions (ROOM.md + scope.yaml per room_id)
  routine_manager/         # Scheduled routine execution
  routine_handlers/        # @routine_handler functions (auto-discovered)
  scope/                   # Scope loader + contracts
  subconscious/            # Autonomous "mind": concerns, proposers, digest, noticer
  ticket_manager/          # Ticket state machine
  transports/              # SMS / Slack / Telegram adapters
  wiki_generator/          # KG -> per-entity wiki + synthetic-fact drain + growth
  ServiceLocator/          # Dependency injection registry
  utils/                   # Shared utilities (scope_gate, path_utils, ...)
app/
  resource_manager/        # ResourceManager (scope-gated + dynamic resources)
  skill_registry/          # Skills (injection, requires_scope gate)
  routes/                  # Flask routes (incl. admin pages: /beliefs, /routines, /subconscious, ...)
configs/                   # JSON/YAML config (routines/, belief_tags.yaml, pod_kinds.json, windows.json, ...)
resources/                 # Resource state files (kg_derived/, subconscious/, ...)
```
