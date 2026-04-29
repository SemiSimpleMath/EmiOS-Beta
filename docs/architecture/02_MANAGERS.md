# Managers

The word "Manager" is an overloaded suffix in this codebase. Two
fundamentally different things wear it:

1. **Agent-orchestrating managers** (`MultiAgentManager` and its
   subclasses) — run a per-invocation agent loop with a blackboard,
   route turns via `state_map`, are invoked through `ManagerInvoker`,
   and produce a structured result for the caller.
2. **Service managers** — long-lived services whose responsibility is
   scheduling, lifecycle, or domain state. They never run an agent
   loop. `RoutineManager` is the canonical example: it dispatches
   scheduled work to functions / tasks / tools / pipelines on a clock,
   not via LLM agent decisions.

These two categories share almost nothing except the suffix. Don't
read sections about MultiAgentManager (blackboard, state_map, agent
loop, manager_invoker) and assume they apply to RoutineManager — they
don't.

## Agent-orchestrating managers

### MultiAgentManager (`manager_classes/MultiAgentManager.py`)

Base class for every manager that runs LLM agents in a loop:
- One `Blackboard` per invocation (shared scoped state, message log).
- Loads agents via `AgentLoader` from each manager's YAML config.
- Validates routing config (`state_map`, control-node refs).
- Manages role bindings (flexible agent aliasing — e.g. `delegator: shared::delegator`).
- Tool scope filtering via `ToolScopeService`.
- Event registration (configurable subscriptions).

Invoked through `ManagerInvoker.invoke(manager, message)`; the agent
loop runs until an exit condition (return_control, max_cycles, error,
explicit cancel) and returns a `ToolResult`.

### RoomManager (`manager_classes/RoomManager.py`)

`MultiAgentManager` subclass with **deterministic routing** (no LLM
delegator agent). Used for rooms (master_room, dayflow_orchestrator,
etc.) where the per-turn routing is a static graph rather than an
LLM-chosen next agent.

- Routing priority each turn:
  1. Explicit `next_agent` set on the blackboard
  2. `state_map[last_agent]` lookup
- Enforces `max_cycles` (default 30).
- Exit conditions: cancelled, max_cycles, exit flag, error flag.

### Manager invocation chain (applies ONLY to the above)

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

`RoutineManager` etc. are NOT invoked through this chain. They have
their own dispatch mechanisms (cron-style scheduling, BackgroundTaskManager
threads).

### Manager YAML configuration

Agent-orchestrating managers are configured via YAML files loaded by
`ManagerRegistry`. Each config defines:
- `state_map` — agent routing graph
- `role_bindings` — agent aliasing
- `entry_agent` — starting agent
- `max_cycles` — loop limit
- `events` — event subscriptions
- `scope` — tool/resource access controls

Service managers do NOT use these configs.

## Service managers

These are long-lived services. They share the "Manager" suffix as a
naming convention but otherwise have nothing in common with
`MultiAgentManager`.

### RoutineManager (`routine_manager/`)

Schedules and dispatches routine work from `configs/routines.json`.
- 5 runner types: `task`, `job`, `tool`, `function`, `pipeline`.
- 5 scheduling policies: `interval`, `daily`, `weekly`, `quiet_hours`.
- Guards: AFK guard, manual toggle, feature guard.
- Thread-safe state persistence to `resource_routine_status.json`.
- Fixed-capacity worker thread pool (`max_workers` config).

Function-type routines are registered in
`app/assistant/routine_manager/routine_functions.py`'s
`ROUTINE_FUNCTION_REGISTRY`. Pipeline-type routines invoke a pipeline
class's `.run()`. Neither runs an agent loop.

See: [06_PIPELINES_AND_ROUTINES.md](06_PIPELINES_AND_ROUTINES.md) and
[20_ROUTINES_ADMIN.md](20_ROUTINES_ADMIN.md).

### BackgroundTaskManager (`background_task_manager/`)

Thread-per-task daemon management. Default tasks include
`db_cleanup`, `watchdog`, `ticket_maintenance`, and `routine_runner`
(which is what actually wakes up `RoutineManager` periodically).

### TicketManager (`ticket_manager/`)

Type-agnostic CRUD with state machine.
- States: `pending` -> `proposed` -> `accepted` / `dismissed` / `snoozed` / `expired`.
- Terminal: `completed`, `dismissed`, `expired`, `failed`.
- Enforces valid transitions via `_ALLOWED_TRANSITIONS`.

### Feature-specific service managers

| Manager | Purpose |
|---------|---------|
| `AFKMonitor` | Active-first idle detection; records active segments, infers AFK from gaps |
| `DJManager` | Music selection state machine with vibe planning and candidate selection |
| `LocationManager` | User location tracking/prediction from calendar + patterns |
| `MaintenanceManager` | Daily summaries, db cleanup, log management, rate-limited events |
| `PreferenceManager` | Feedback handling (thumbs up/down), delegates to LabelAgent |
| `UserSettingsManager` | User settings and feature flags storage/retrieval |

These are all plain Python services accessed via `DI` — they don't
run agent loops, don't have manager YAML configs, and aren't invoked
through `ManagerInvoker`.

## Coordination mechanisms

These primitives are used by both categories of manager.

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

### Blackboard (only for agent-orchestrating managers)

Each `MultiAgentManager` invocation gets its own `Blackboard` with a
scope stack:
- **Global scope** — manager-level state.
- **Local scopes** — created for agent-to-agent calls (push/pop call context).
- `get_state_value(key)` — searches top-to-bottom of stack.
- `update_state_value(key, val)` — writes to current (top) scope.
- `add_msg(message)` — appends to message log.

Service managers don't use Blackboard.

## Lifecycle

1. **ServiceLocator bootstrap** — register core services.
2. **ManagerRegistry.preload_all()** — load all
   `MultiAgentManager`-derived configs.
3. **BackgroundTaskManager.start_all()** — start daemon tasks.
4. **RoutineManager** — lazy-initialized; the BTM `routine_runner`
   task wakes it up on a clock.
5. **RoomSessionManager** — instantiates `RoomManager` instances per
   room request.
6. **Shutdown**: `BackgroundTaskManager.stop_all()` ->
   `RoutineManager.shutdown()`.

## Key files

| File | Purpose |
|------|---------|
| `manager_classes/MultiAgentManager.py` | Base agent orchestrator |
| `manager_classes/RoomManager.py` | Deterministic-routing subclass |
| `manager_runtime/manager_invoker.py` | Canonical invocation entry point for agent-orchestrating managers |
| `manager_registry/manager_registry.py` | Manager YAML config loading |
| `multi_agent_manager_factory/` | Manager instance factory |
| `routine_manager/routine_manager.py` | Scheduled routine dispatcher (NOT a MultiAgentManager) |
| `background_task_manager/background_task_manager.py` | Daemon thread management |
| `ticket_manager/ticket_manager.py` | Ticket CRUD state machine |
| `ServiceLocator/service_locator.py` | Dependency injection registry |
