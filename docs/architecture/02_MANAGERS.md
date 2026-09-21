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

- One `Blackboard` per manager instance (shared scoped state, message log).
  Create a fresh instance for each invocation; `request_handler` does not reset it.
- Loads agents via `AgentLoader` from each manager's YAML config.
- Validates routing config (`state_map`, control-node refs).
- Manages role bindings (flexible agent aliasing — e.g.
  `delegator: room::delegator`).
- Tool scope filtering via `ToolScopeService`.

Invoked through `ManagerInvoker.invoke(manager, message)`; the agent
loop (`_run_loop`) runs until an exit condition (exit flag,
max_cycles, error, explicit cancel) and returns a `ToolResult`.

**`max_cycles` counts loop-selected non-`ControlNode` activations**
(a 2026-06-11 change in `MultiAgentManager._run_loop`). Deterministic
plumbing — control nodes, tool dispatch/handler hops — is free; only a
non-`ControlNode` activation increments `agent_cycles`. A separate
`iteration_cap = max(max_cycles * 8, 40)` backstops infinite
control-node spins. The delegator call and agents called internally by a node
or tool are outside this counter; it is not a total LLM-call or time budget.
At each boundary the checks run in this order: cancellation, agent budget,
iteration cap, `exit`, then `error`. A budget reached on the preceding
activation therefore takes precedence over an exit flag set by that activation.

On entry, `request_handler` seeds the blackboard (task/information/data,
room context, scope). If no `scope_context` reaches this method,
`_apply_no_inbound_scope` raises in production; `request_handler` catches
that exception and enters error-exit handling. Under a test harness
(`EMI_TEST_MODE=1` / pytest), it substitutes a permissive scope. It also publishes `flow_config`,
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
   (agents/control nodes/ingress set this). The standard agent input path
   clears it on entry; control nodes must clear or replace it themselves. Otherwise
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
        -> drain mailbox (out-of-band agent_inject messages)
        -> check cooperative cancellation flag
        -> delegator routes (state_map or explicit next_agent)
        -> next_agent.action_handler(message)
        -> Loop until exit/max_cycles/error/cancel
      -> Return ToolResult
    -> MAMInstanceManager.unregister(...)  # finally after preprocessing/scope/loop
```

`ManagerInvoker` (`manager_runtime/manager_invoker.py`) is the canonical
entry. It wraps every call in `MAMInstanceManager.register` /
`unregister` (try/finally), stashes the `invocation_id` on the
manager's blackboard (so the mailbox dispatcher can address it), and
fires a generic `manager_invocation_started` event carrying the
per-instance `display_name`, `room_id`, and `reply_to`.
`MAMInstanceManager` (`manager_runtime/mam_instance_manager.py`) owns
the running-instance registry, display-name assignment, and
cancel/status surface — `ManagerInvoker` holds no running-instance registry.

Preprocessing may return immediately: a task whose trimmed text starts with
`SKIPPED:` (case-insensitive, with or without a task file) returns a
`final_answer` result with skipped status. Registration still unwinds, but
`ScopeAdapter` and the manager loop do not run. Preprocessing or scope-adapter
exceptions propagate through the invoker after cleanup.

ScopeAdapter resolves an explicit scope first, then room-derived scope, then
`data.scope_contract`. Strict mode normally rejects remaining scope-less
requests, but permits system derivation from task-file/resource-contract
request data. Tests are lenient unless `SCOPE_CONTRACT_STRICT` overrides them.

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
- `execution_trace` — opt-in `{enabled: ...}` step recorder.

(There is no manager-level `events:` key. Managers are fresh instances
per invocation, so a per-instance event_hub subscription would leak one
handler per request; the unused feature was removed 2026-07-08.)

### flow_config

`flow_config` holds **`state_map`** (the routing graph — note it lives
*under* `flow_config`, not at top level) plus optional flow-policy
sub-sections. `_validate_strict_routing_config` (always on) enforces:

- `state_map` is a non-empty dict of non-empty string→string edges.
- `control_nodes` exists and names ≥1 node.
- When `tool_return` is a dict, `tool_call_result_handler_node` must be a
  string that exists in `state_map`.
- When they are dicts, `critic` requires `subject_agent` / `critic_agent` /
  `continue_agent`; `summary` requires `source_agent` / `summary_agent`
  / `resume_agent`; each required field must be a non-empty string.
- Edge targets and existing `*_return_control` prefixes must occur among
  configured agent/node names or role-binding keys/values. This is a name
  membership check: it does not prove a role target has a loaded instance,
  require every emitted return-control edge, or check graph reachability.
  Non-dict optional `tool_return`, `critic`, and `summary` sections skip these
  subsection checks. `strict_routing: false` does not disable validation.

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

The factory creates a new manager and `Blackboard` each time. The instance
then has this scope stack:

- **Global scope** — manager-level state.
- **Root request scope** — pushed after ingress state/config is seeded, with
  `(manager_name, manager_name, root_scope_id)` as its call context.
- **Nested scopes** — pushed for agent-to-agent calls. Popping discards local
  state; the shared message log remains. `request_handler` unwinds remaining
  contexts through its root in `finally`.
- `get_state_value(key)` — searches top-to-bottom of stack.
- `update_state_value(key, val)` — writes to current (top) scope.
- `add_msg(message)` — appends to message log.

At the top of every loop cycle the manager **drains its mailbox**
(`_drain_mailbox` → `MailboxDispatcher.drain_to`), delivering
out-of-band `agent_inject` messages into per-agent steering slots.
`MAMInstanceManager.cancel` marks the invocation and descendants as cancelling,
revokes a work-owned attempt durably, and retains the global `cancelled=True` flag.
Checks run at manager/agent/tool admission and after model return, including after
approval waits. A running unsupported tool remains visible until actual exit.
A successful cancellation request does not establish completion.

Mailbox TTL is checked on drain (default 30 minutes); unregister clears the
queue present at that moment. A post after cleanup can remain until an
explicit drain/clear or process exit; there is no background TTL sweep.

Service managers don't use Blackboard.

## Exit results

Successful finalization returns `ToolResult(result_type="final_answer")`.
`FinalAnswerNode` normalizes final-answer fields or `result`; `ManagerExitNode`
can also capture a terminal agent's structured output. `handle_exit` attaches
`final_answer_raw` when available on a successful structured result.

Cancellation returns `manager_aborted` directly. Max-budget/error/unknown exits
attempt configured graceful-exit routing with `max_exit_cycles` (default 10).
`GracefulExitControlNode` writes an abort report and `manager_exit_kind="aborted"`.
If the exit loop fails, `handle_default_error_exit` returns `manager_aborted`
with structured `aborted` and `exit_state` fields. Completion is checked before
budget exhaustion; agent budgets restrict the next agent activation while terminal
control nodes can still finish. Cancellation retains priority.

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


## Execution ownership update (2026-09-20)

See [Execution ownership and cancellation](EXECUTION_OWNERSHIP.md) for the current
invocation tree, immutable main-attempt binding, cancellation boundaries and durable
takeover barrier. Timeout result recording now revokes further calls/worker writes;
it does not mean the old thread exited. The two additive execution tables supplement
the five graph tables. The finalizer remains responsible for judgment and failure counts.
