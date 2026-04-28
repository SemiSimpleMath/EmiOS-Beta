# Managers

Managers are orchestrators that coordinate agents, background tasks, and system services.

## Manager Types

### Agent Orchestrators

**MultiAgentManager** (`manager_classes/MultiAgentManager.py`)
- Base class for all agent-orchestrating managers
- Maintains a Blackboard (shared state) per invocation
- Loads agents from config via AgentLoader
- Validates routing config (state_map, control_node references)
- Manages role bindings (flexible agent aliasing)
- Event registration (configurable event handlers)
- Tool scope filtering via ToolScopeService

**RoomManager** (`manager_classes/RoomManager.py`)
- Extends MultiAgentManager with **deterministic routing** (no delegator agent)
- Routing priority:
  1. Explicit `next_agent` set on blackboard
  2. `state_map[last_agent]` lookup
- Enforces max cycle limit (config: `max_cycles`, default 30)
- Exit conditions: cancelled, max_cycles, exit flag, error flag

### Infrastructure Managers

**BackgroundTaskManager** (`background_task_manager/`)
- Thread-per-task daemon management
- Default tasks: db_cleanup, watchdog, ticket_maintenance, routine_runner
- Data fetch routines (email, calendar, weather, etc.) are managed by RoutineManager via `configs/routines.json`

**RoutineManager** (`routine_manager/`)
- Executes scheduled routines from `configs/routines.json`
- 5 runner types: task, job, tool, function, pipeline
- 5 scheduling policies: interval, daily, weekly, quiet_hours
- Guards: AFK guard, manual toggle, feature guard
- Thread-safe state persistence to `resource_routine_status.json`
- Fixed-capacity worker thread pool (`max_workers` config)

See: [06_PIPELINES_AND_ROUTINES.md](06_PIPELINES_AND_ROUTINES.md)

**TicketManager** (`ticket_manager/`)
- Type-agnostic CRUD with state machine
- States: pending -> proposed -> accepted/dismissed/snoozed/expired
- Terminal states: completed, dismissed, expired, failed
- Enforces valid transitions via `_ALLOWED_TRANSITIONS`

### Feature-Specific Managers

| Manager | Purpose |
|---------|---------|
| `AFKMonitor` | Active-first idle detection; records active segments, infers AFK from gaps |
| `DJManager` | Music selection state machine with vibe planning and candidate selection |
| `LocationManager` | User location tracking/prediction from calendar + patterns |
| `MaintenanceManager` | Daily summaries, db cleanup, log management, rate-limited events |
| `PreferenceManager` | Feedback handling (thumbs up/down), delegates to LabelAgent |
| `UserSettingsManager` | User settings and feature flags storage/retrieval |

## Coordination Mechanisms

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

Common events: `socket_emit`, `repo_update`, `agent_progress_emit`, `proactive_suggestion`, `afk_state_changed`, `dayflow_ticket_responded`

### Blackboard (Scoped State)

Each manager invocation gets its own Blackboard with a scope stack:
- **Global scope**: Manager-level state
- **Local scopes**: Created for agent-to-agent calls (push/pop call context)
- `get_state_value(key)`: searches top-to-bottom of stack
- `update_state_value(key, val)`: writes to current (top) scope
- `add_msg(message)`: appends to message log

## Manager Invocation Chain

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

## Manager Configuration

Managers are configured via YAML files loaded by `ManagerRegistry`. Each config defines:
- `state_map`: Agent routing graph
- `role_bindings`: Agent aliasing
- `entry_agent`: Starting agent
- `max_cycles`: Loop limit
- `events`: Event subscriptions
- `scope`: Tool/resource access controls

## Lifecycle

1. **ServiceLocator bootstrap** — register core services
2. **ManagerRegistry.preload_all()** — load all manager configs
3. **BackgroundTaskManager.start_all()** — start daemon tasks
4. **RoutineManager** — lazy-initialized, called periodically by BTM
5. **RoomSessionManager** — instantiated per room request
6. **Shutdown**: `BackgroundTaskManager.stop_all()` -> `RoutineManager.shutdown()`

## Key Files

| File | Purpose |
|------|---------|
| `manager_classes/MultiAgentManager.py` | Base agent orchestrator |
| `manager_classes/RoomManager.py` | Deterministic routing extension |
| `background_task_manager/background_task_manager.py` | Daemon thread management |
| `routine_manager/routine_manager.py` | Scheduled routine execution |
| `ticket_manager/ticket_manager.py` | Ticket CRUD state machine |
| `manager_runtime/manager_invoker.py` | Canonical invocation entry point |
| `manager_registry/manager_registry.py` | Manager config loading |
| `multi_agent_manager_factory/` | Manager instance factory |
| `ServiceLocator/service_locator.py` | Dependency injection registry |
