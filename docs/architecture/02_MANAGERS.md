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
loop (`_run_loop`) runs until an exit condition (exit flag,
max_cycles, error, explicit cancel) and returns a `ToolResult`.

**`max_cycles` budgets LLM-agent activations, not loop iterations**
(a 2026-06-11 change in `MultiAgentManager._run_loop`). Deterministic
plumbing — control nodes, tool dispatch/handler hops — is free; only a
non-`ControlNode` activation increments `agent_cycles`. A separate
`iteration_cap = max(max_cycles * 8, 40)` backstops infinite
control-node spins.

On entry, `request_handler` seeds the blackboard (task/information/data,
room context, scope), then **fails loud if the inbound message carries
no `scope_context`** — `_apply_no_inbound_scope` raises in production
and only substitutes a permissive scope under a test harness
(`EMI_TEST_MODE` / pytest). It also publishes `flow_config`,
per-node `control_nodes` configs, and the loop counters onto the
blackboard.

## One manager class; routing is the Delegator's state_map lookup

Every agent-orchestrating manager — rooms included (master_room,
dayflow_orchestrator) — is `class_name: MultiAgentManager`. There is no
separate room manager class (a dead `RoomManager` subclass was deleted
2026-07-08; it had zero references).

Routing is still deterministic: the `delegator` role binding resolves to
the `Delegator` agent class, which is a **state-map lookup, not an LLM**
(`agent_classes/Delegator.py`). Each cycle it:

1. Honors an explicit `next_agent` already set on the blackboard
   (agents/control nodes/ingress set this; every agent activation clears
   it on entry, so a set value is always fresh), otherwise
2. Looks up `flow_config.state_map[last_agent]`.
3. Dead-end (no match) → error flag, loop exits via the error path.

Rooms enter their flow by seeding `next_agent` in the request data
(ingress sets the mode's source agent, e.g. `master_room::chat_gate`);
`request_handler` writes data keys onto the blackboard, and the first
delegator pass honors it.

The full registry of runtime-reserved blackboard keys, the synthetic
`last_agent` signal states (`<agent>_return_control`, …), and the blessed
input/result idioms live in
[02b_RUNTIME_DATA_CONTRACT.md](02b_RUNTIME_DATA_CONTRACT.md).

## Manager invocation chain

```
RoomSessionManager.invoke_manager(envelope, request_data)
  -> ManagerInvoker.invoke(manager_instance, user_message)
    -> MAMInstanceManager.register(...)    # running-invocation record + display_name
    -> publish "manager_invocation_started" on event_hub
    -> RequestPreprocessor.preprocess()    # normalize message (may short-circuit)
    -> ScopeAdapter.apply()                # apply/derive scope context
    -> manager_instance.request_handler()  # seed blackboard, fail-loud on no scope
      -> Seed blackboard (task, room ctx, scope, flow_config, counters)
      -> Seed local messages from data.seeded_chat_messages
      -> run_agent_loop -> _run_loop:
        -> drain mailbox (out-of-band @-messages / cancel)
        -> delegator routes (state_map or explicit next_agent)
        -> next_agent.action_handler(message)
        -> Loop until exit/max_cycles/error/cancel
      -> Return ToolResult
    -> MAMInstanceManager.unregister(...)  # in finally — registry can't drift
```

`ManagerInvoker` (`manager_runtime/manager_invoker.py`) is the canonical
entry. It wraps every call in `MAMInstanceManager.register` /
`unregister` (try/finally), stashes the `invocation_id` on the
manager's blackboard (so the mailbox dispatcher can address it), and
fires a generic `manager_invocation_started` event carrying the
per-instance `display_name`, `room_id`, and `reply_to`.
`MAMInstanceManager` (`manager_runtime/mam_instance_manager.py`) owns
the running-instance registry, display-name assignment, and
cancel/status surface — `ManagerInvoker` itself is stateless.

`RoutineManager` and the other service managers are NOT invoked
through this chain. They have their own dispatch mechanisms (cron-style
scheduling for routines, `BackgroundTaskManager`-owned threads for
daemons).

## Manager YAML configuration

Agent-orchestrating managers are configured via `config.yaml` (one per
`multi_agents/<name>/` directory) loaded by `ManagerRegistry`. Real
top-level keys:

- `name`, `class_name` (`MultiAgentManager`),
  `display_name` (per-manager persona name), `description`.
- `max_cycles` (default 30), `max_exit_cycles` (graceful-exit budget,
  default 10).
- `role_bindings` — agent aliasing. **The entry agent is the
  `delegator` role binding** (e.g. `delegator: room::delegator`); there
  is no `entry_agent` field.
- `agents` — list of `{name, class}` agent bindings to instantiate.
- `control_nodes` — list of `{name, class}` control-node bindings
  (required: validation demands ≥1 named node).
- `tools.allowed_tools` / `tools.except_tools` — the manager tool list.
- `scope_contract` — the manager's own scope policy block
  (`tools.allowed_tools` / `blocked_tools` / `requires_approval_tools`,
  etc.). This is the scope layer, distinct from the `tools:` list above.
- `flow_config` — routing + flow policy (see below).
- `events` — event subscriptions (handler `<event>_handler` must exist
  on the manager or `_register_configured_events` raises).
- `execution_trace` — opt-in `{enabled: ...}` step recorder.

### flow_config

`flow_config` holds **`state_map`** (the routing graph — note it lives
*under* `flow_config`, not at top level) plus optional flow-policy
sub-sections. `_validate_strict_routing_config` (always on) enforces:

- `state_map` is a non-empty dict of non-empty string→string edges.
- `control_nodes` exists and names ≥1 node.
- If present, `tool_return.tool_call_result_handler_node` must be a
  string that exists in `state_map`.
- If present, `critic` requires `subject_agent` / `critic_agent` /
  `continue_agent`; `summary` requires `source_agent` / `summary_agent`
  / `resume_agent`; both must be non-empty strings.

Other common `flow_config` sub-sections: `strict_routing`, `flow`
(per-mode `source_agent`), and `chat_gate` (`source_agent`,
`switchboard_agent`, `final_node`, `exit_flag_key`, response/task keys).

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

At the top of every loop cycle the manager **drains its mailbox**
(`_drain_mailbox` → `MailboxDispatcher.drain_to`), delivering
out-of-band `@`-messages, cancel signals, and runtime context injected
from another thread onto the blackboard — always at the safe cycle
boundary, never mid-LLM-call.

Service managers don't use Blackboard.

## Lifecycle

1. **ServiceLocator bootstrap** — register core services.
2. **ManagerRegistry.preload_all()** — load all
   `MultiAgentManager`-derived configs.
3. **BackgroundTaskManager.start_all()** — start daemon tasks (this
   step is in service-manager territory; see
   [10_SERVICE_MANAGERS](10_SERVICE_MANAGERS.md)).
4. **RoomSessionManager** — creates a fresh manager instance per room
   request via `multi_agent_manager_factory`.

## Key files

| File | Purpose |
|------|---------|
| `manager_classes/MultiAgentManager.py` | The agent orchestrator (`_run_loop`, `_validate_strict_routing_config`, `request_handler`) |
| `manager_runtime/manager_invoker.py` | Canonical (stateless) invocation entry point |
| `manager_runtime/mam_instance_manager.py` | Running-invocation registry, display names, cancel/status |
| `manager_runtime/mailbox.py` | Out-of-band message dispatch into a running invocation |
| `manager_registry/manager_registry.py` | Loads each `multi_agents/<name>/config.yaml` |
| `multi_agent_manager_factory/MultiAgentManagerFactory.py` | Manager instance factory |
| `ServiceLocator/service_locator.py` | DI registry (see [17_SERVICE_LAYER](17_SERVICE_LAYER.md)) |
