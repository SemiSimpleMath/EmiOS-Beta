# Managers

The word "Manager" is an overloaded suffix in this codebase. Two
fundamentally different things wear it:

1. **Agent-orchestrating managers** (`MultiAgentManager` and its
   subclasses) — the subject of this page. Run a per-invocation agent
   loop with a blackboard, route turns via `state_map`, are invoked
   through `ManagerInvoker`, and produce a structured result for the
   caller.
2. **Service managers** — long-lived services whose responsibility is
   scheduling, lifecycle, or domain state. `RoutineManager`,
   `BackgroundTaskManager`, `TicketManager`, `AFKMonitor`, etc.
   They never run an agent loop and share nothing with `MultiAgentManager`
   except the suffix. **See
   [10_SERVICE_MANAGERS](10_SERVICE_MANAGERS.md).**

Don't read sections about `MultiAgentManager` (blackboard, state_map,
agent loop, manager_invoker) and assume they apply to service
managers — they don't.

## MultiAgentManager (`manager_classes/MultiAgentManager.py`)

Base class for every manager that runs LLM agents in a loop:

- One `Blackboard` per invocation (shared scoped state, message log).
- Loads agents via `AgentLoader` from each manager's YAML config.
- Validates routing config (`state_map`, control-node refs).
- Manages role bindings (flexible agent aliasing — e.g.
  `delegator: shared::delegator`).
- Tool scope filtering via `ToolScopeService`.
- Event registration (configurable subscriptions).

Invoked through `ManagerInvoker.invoke(manager, message)`; the agent
loop runs until an exit condition (return_control, max_cycles, error,
explicit cancel) and returns a `ToolResult`.

## RoomManager (`manager_classes/RoomManager.py`)

`MultiAgentManager` subclass with **deterministic routing** (no LLM
delegator agent). Used for rooms (master_room, dayflow_orchestrator,
etc.) where the per-turn routing is a static graph rather than an
LLM-chosen next agent.

- Routing priority each turn:
  1. Explicit `next_agent` set on the blackboard
  2. `state_map[last_agent]` lookup
- Enforces `max_cycles` (default 30).
- Exit conditions: cancelled, max_cycles, exit flag, error flag.

## Manager invocation chain

```
RoomSessionManager.invoke_manager(envelope, request_data)
  -> ManagerInvoker.invoke(manager_instance, user_message)
    -> RequestPreprocessor.preprocess()    # normalize message
    -> ScopeAdapter.apply()                # apply scope context
    -> MultiAgentManager.request_handler()
      -> Seed blackboard from room history
      -> Seed resource subscriptions
      -> Agent loop:
        -> Pick next agent (state_map or explicit next_agent)
        -> agent.action_handler(message)
        -> Loop until exit/max_cycles/error
      -> Return ToolResult
```

`RoutineManager` and the other service managers are NOT invoked
through this chain. They have their own dispatch mechanisms (cron-style
scheduling for routines, `BackgroundTaskManager`-owned threads for
daemons).

## Manager YAML configuration

Agent-orchestrating managers are configured via YAML files loaded by
`ManagerRegistry`. Each config defines:

- `state_map` — agent routing graph.
- `role_bindings` — agent aliasing.
- `entry_agent` — starting agent.
- `max_cycles` — loop limit.
- `events` — event subscriptions.
- `scope` — tool/resource access controls.

Service managers do NOT use these configs.

## Coordination primitives (used during agent-orchestration)

These primitives are owned by [17_SERVICE_LAYER](17_SERVICE_LAYER.md);
this section is just how the agent-orchestrators interact with them.

### ServiceLocator (DI)

```python
from app.assistant.ServiceLocator.service_locator import DI

DI.event_hub           # EventHub (pub-sub)
DI.global_blackboard   # Cross-manager message history
DI.tool_registry       # All available tools
DI.agent_registry      # All available agents
DI.resource_manager    # Resource state
DI.socket_manager      # WebSocket connections
DI.reply_router        # Maps request_id to delivery destination
```

### EventHub (Pub-Sub)

```python
event_hub.register_event('repo_update', handler)
event_hub.publish(message)  # message.event_topic = 'repo_update'
```

Common events: `socket_emit`, `repo_update`, `agent_progress_emit`,
`proactive_suggestion`, `afk_state_changed`, `dayflow_ticket_responded`.

### Blackboard (per-invocation scoped state)

Each `MultiAgentManager` invocation gets its own `Blackboard` with a
scope stack:

- **Global scope** — manager-level state.
- **Local scopes** — created for agent-to-agent calls (push/pop call
  context).
- `get_state_value(key)` — searches top-to-bottom of stack.
- `update_state_value(key, val)` — writes to current (top) scope.
- `add_msg(message)` — appends to message log.

Service managers don't use Blackboard.

## Lifecycle

1. **ServiceLocator bootstrap** — register core services.
2. **ManagerRegistry.preload_all()** — load all
   `MultiAgentManager`-derived configs.
3. **BackgroundTaskManager.start_all()** — start daemon tasks (this
   step is in service-manager territory; see
   [10_SERVICE_MANAGERS](10_SERVICE_MANAGERS.md)).
4. **RoomSessionManager** — instantiates `RoomManager` instances per
   room request.

## Key files

| File | Purpose |
|------|---------|
| `manager_classes/MultiAgentManager.py` | Base agent orchestrator |
| `manager_classes/RoomManager.py` | Deterministic-routing subclass |
| `manager_runtime/manager_invoker.py` | Canonical invocation entry point |
| `manager_registry/manager_registry.py` | Manager YAML config loading |
| `multi_agent_manager_factory/` | Manager instance factory |
| `ServiceLocator/service_locator.py` | DI registry (see [17_SERVICE_LAYER](17_SERVICE_LAYER.md)) |
