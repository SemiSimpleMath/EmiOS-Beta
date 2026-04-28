# EmiOS Architecture Overview

EmiOS is a local-first personal AI assistant built on Flask + SQLite + ChromaDB. It uses a multi-agent orchestration system with knowledge graph memory, event-driven scheduling, and multi-transport communication (UI, SMS, Slack, Telegram).

## Layered Architecture

```
Transport Layer (UI, SMS, Slack, Telegram)
    |
Room Session Manager (transport abstraction, session modes, persistence)
    |
Manager Layer (MultiAgentManager / RoomManager — deterministic agent loop)
    |
Agent Layer (LLM-powered decision units with structured output)
    |
Control Nodes (deterministic routing, tool dispatch, flow control)
    |
Tool Layer (registered tools with argument schemas)
    |
Service Layer (ServiceLocator/DI, EventHub, Blackboard, ResourceManager)
```

## Core Concepts

### Agents
LLM-powered decision units. Each agent has a `config.yaml`, Jinja2 prompt templates (`system.j2`, `user.j2`), and an optional Pydantic `agent_form.py` for structured output. Agents don't directly execute actions — they produce decisions that control nodes and tools act on.

See: [01_AGENTS.md](01_AGENTS.md)

### Managers
Orchestrators that run agent loops. `MultiAgentManager` is the base; `RoomManager` extends it with deterministic routing via `state_map`. Other managers handle infrastructure: `BackgroundTaskManager` (daemon threads), `RoutineManager` (scheduled jobs), `TicketManager` (state-machine CRUD).

See: [02_MANAGERS.md](02_MANAGERS.md)

### Rooms
Scoped conversation channels. Each room defines identity, conversation rules, safety constraints, permissions, and policy. The `master_room` is the primary UI with full authority (level 99) and dayflow delegation. Other rooms (Telegram, Slack, SMS) have limited capabilities.

See: [03_ROOMS.md](03_ROOMS.md)

### Control Nodes
Deterministic (non-LLM) state machines in the agent loop. They handle routing decisions, tool dispatch, data normalization, and exit logic. `ToolCaller` is the canonical dispatcher for tools and agent-to-agent calls.

See: [04_CONTROL_NODES.md](04_CONTROL_NODES.md)

### Dayflow Orchestrator
An autonomous daily workflow engine that ingests tickets, calendar events, emails, and cross-room chat. It maintains persistent lifecycle state for all items and coordinates multi-agent decision-making to triage, plan, and dispatch actions.

See: [05_DAYFLOW.md](05_DAYFLOW.md)

### Pipelines
Sequential step-based execution frameworks for background data processing. Used for daily insights, KG ingestion, entity cards, belief engine, and weekly synthesis.

See: [06_PIPELINES_AND_ROUTINES.md](06_PIPELINES_AND_ROUTINES.md)

## Key Coordination Mechanisms

| Mechanism | Purpose | Access Pattern |
|-----------|---------|----------------|
| **ServiceLocator (DI)** | Dependency injection | `DI.event_hub`, `DI.tool_registry`, etc. |
| **EventHub** | Async pub-sub messaging | `event_hub.publish(message)` / `register_event(key, handler)` |
| **Blackboard** | Scoped shared state within a manager | `blackboard.get_state_value(key)` / `update_state_value(key, val)` |
| **Global Blackboard** | Cross-manager message history | `DI.global_blackboard` |
| **ResourceManager** | Persistent resource state (JSON files) | `resource_manager.get_resource(id)` / `update_resource(id, data)` |
| **Singletons** | Shared manager instances | `get_background_task_manager()`, `get_routine_manager()`, etc. |

## Request Flow (Master Room Example)

```
User message via WebSocket
  -> RoomSessionManager.process_inbound()
    -> Build InboundEnvelope (transport-agnostic)
    -> Load room context (identity, safety, policy, permissions)
    -> Build scoped history (24h for master_room)
    -> ManagerInvoker.invoke(master_room_manager, message)
      -> RequestPreprocessor.preprocess()
      -> ScopeAdapter.apply()
      -> RoomManager.request_handler()
        -> Agent loop:
          1. chat_gate (LLM: reply | handoff | dayflow_delegate)
          2. ChatTaskRouterNode (deterministic routing)
          3. switchboard agent (if handoff)
          4. SwitchboardArgumentsNode (normalize args)
          5. ToolCaller (dispatch tool/agent)
          6. ToolResultHandler (process result)
          7. FinalAnswerNode (exit)
    -> RoomSessionManager.persist_outbound()
    -> Deliver reply via transport
```

## Directory Map

```
app/assistant/
  agent_classes/          # Base agent classes (Agent, Planner, MultiToolAgent)
  agent_registry/         # Agent loading, factory, configuration
  agent_runtime/          # Runtime services (prompt builder, LLM client, flow controller)
  agents/                 # Agent definitions (config.yaml + prompts/ + agent_form.py)
  background_task_manager/# Daemon thread management
  control_nodes/          # Deterministic routing and dispatch
  dayflow_orchestrator/   # Autonomous daily workflow engine
  event_graph/            # Semantic event hierarchy (EventNode)
  kg_core/                # Knowledge graph core
  lib/
    blackboard/           # Shared state with scope stack
    tool_registry/        # Tool loading and access control
    tools/                # Tool definitions (thin wrappers -> core_tools)
    core_tools/           # Tool implementations
  manager_classes/        # MultiAgentManager, RoomManager
  manager_runtime/        # Manager invocation pipeline
  pipelines/              # Step-based background processing
  room_session_manager/   # Transport abstraction and session handling
  rooms/                  # Room definitions (per room_id)
  routine_manager/        # Scheduled routine execution
  ticket_manager/         # Ticket state machine
  ServiceLocator/         # Dependency injection registry
  utils/                  # Shared utilities
configs/                  # JSON configuration files
resources/                # Resource state files
```
