# Control Nodes

Control Nodes are deterministic (non-LLM) state machines that execute within the agent loop. They handle routing decisions, tool dispatch, data normalization, and exit logic.

## Base Class

`app/assistant/control_nodes/control_node.py`

Interface: `action_handler(message: Message)` — same as agents, so managers route to them interchangeably.

## Control Node Categories

### Router Nodes
Route based on blackboard state:

- **`chat_task_router_node.py`** — Routes chat responses: if `handoff_tf=true` -> switchboard, else -> final answer
- **`master_room_chat_task_router_node.py`** — Master room variant: adds dayflow delegation path
- **`action_selector_router_node.py`** — Dayflow: routes based on ticket_tf vs handoff_tf
- **`tick_router_node.py`** — Top-of-pipeline router for the dayflow orchestrator manager
- **`tool_return_router.py`** — Routes tool results back to the calling agent

(Other room-specific routers follow the same shape: `emi_code_chat_task_router_node.py`,
`kg_dev_chat_task_router_node.py`, `geoguessr_router_node.py`,
`doc_create_final_router_node.py`, `plan_mode_final_router_node.py`,
`task_create_final_router_node.py`, `task_spec_router_node.py`.)

### Tool/Action Execution

- **`tool_caller.py`** (~480 lines) — The canonical dispatcher:
  - Is the action a **tool**? -> Execute via tool registry with scope enforcement
  - Is the action an **agent**? -> Push call context on blackboard, invoke agent
  - Is the action a **control node**? -> Set `next_agent` to the control node
  - Manages scope creation, approval flows, MCP tool execution
  - Creates `blackboard.push_call_context()` for agent-to-agent calls
  - Surface-specific subclasses route a single room's dispatch: `chat_tool_caller.py`,
    `master_room_tool_caller.py` (shared helpers in `_tool_caller_util.py`).

- **`tool_result_handler.py`** — Processes tool results and pops call context
- **`tool_approve_node.py`** (`ToolApproveNode`) — Resolves a tool's approval gate
  before dispatch (raises the owner ticket / blocks on the decision)

### Data Transform Nodes

- **`view_materializer_node.py`** — Builds views from state
- **`task_compile_metadata_node.py`** — Builds task metadata
- **`task_compile_final_output_node.py`** — Compiles final task output

### Flow Gates & Critics

- **`critic_pre_node.py` / `critic_post_node.py` / `critic_capture_node.py`** (`CriticPreNode` etc.) — Critic loop: pre-check, post-check, and capture of the critic verdict around an agent step
- **`relevance_cleaner_gate_node.py`** — Gate in the relevance-cleaner pipeline (paired with `relevance_cleaner_prep_node.py` / `relevance_cleaner_persist_node.py`)
- **`state_transition_guard_node.py`** — Normalizes/guards dayflow state-transition fields (e.g. maps LLM-facing `reactivate_at` to internal `reactivate_at_utc`)
- **`triage_spawn_guard_node.py`** (`TriageSpawnGuardNode`) — Guards intake-triage spawning
- **`task_compile_critic_node.py`** (`TaskCompileCriticNode`) — Quality gate on compiled task output

### Exit/Return Nodes

- **`final_answer_node.py`** — Normalizes output and sets exit flag
- **`graceful_exit_control_node.py`** — Handles clean exits
- **`manager_exit_node.py`** — Multi-manager exit coordination

### Dayflow / Work-Object Nodes

The work-object cutover (2026-06) replaced the item-dispatch lane
(`dayflow_switchboard_arguments_node`, `dayflow_tool_caller`,
`action_result_normalizer_node` — all deleted) with the work-object tick
pipeline. Its control nodes:

- **`strategic_planner_wo_prep_node.py` / `_persist_node.py`** — build the
  evaluator's context (portfolio, ticket replies with resolved work refs,
  `expected_schedule_view` with id-chain provenance) / persist its verdicts
  (mint/change/complete/abandon via `work_persist`, consume cited intake items,
  forward `concern:` refs onto the work object)
- **`work_finalizer_node.py`** — judges each completed node's result; sole
  producer of the `closed` terminal; closure propagates concern outcomes
- **`work_architect_node.py`** — per-goal DAG decomposition + re-plan
- **`work_repair_node.py`** — adjudicates failed nodes (retry / escalate / abandon)
- **`work_node_dispatch_node.py` / `work_node_materializer_node.py`** — dispatch a
  ready node (job thread or ticket) / record results and ticket replies on the graph
- **`workobject_render_node.py`** — renders work-object views
- **`state_mover_prep_node.py` / `_persist_node.py`** — is_ready promotion + HOLD
- **`intake_triage_prep_node.py` / `triage_persist_node.py`**,
  **`context_enricher_prep_node.py` / `_persist_node.py`** — intake triage and
  enrichment prep/persist pairs
- **`post_room_finalize_node.py`** — closes acted_on items, persists state
  mutations, writes action log entries
- **`fast_tick_promoter_node.py`** — fast-tick deterministic promoter
- **`dag_executor_node.py` / `dag_manager_control_node.py`** — DAG-shaped
  multi-step execution

### Node families

Many nodes come in per-agent **prep/persist pairs**: a `*_prep_node.py` loads that
agent's context off the items table before it runs, and a `*_persist_node.py` writes its
output back. Examples: `context_enricher_prep_node` / `context_enricher_persist_node`,
`relevance_cleaner_prep/persist`, `state_mover_prep/persist`, `triage_persist_node`,
`planner_persist_node`, `summary_pre/post_node`, `task_compile_metadata/post/final_output_node`.
There is no monolithic blackboard builder — each agent's prep node loads its own slice.

## Blackboard Interaction

Control nodes read and write state via the Blackboard:

```python
# Reading state
handoff_tf = self.blackboard.get_state_value("handoff_tf")
last_agent = self.blackboard.get_state_value("last_agent")

# Writing state
self.blackboard.update_state_value("next_agent", "switchboard")
self.blackboard.update_state_value("action", "get_weather")
self.blackboard.update_state_value("tool_arguments", {"city": "NYC"})

# Flow control
self.blackboard.update_state_value("exit", True)
self.blackboard.update_state_value("error", True)
```

## ToolCaller: The Canonical Dispatcher

ToolCaller is the most complex control node. It handles:

1. **Tool execution**: Resolves tool from registry, builds scope context, executes
2. **Agent-to-agent calls**: Pushes a new call context (scope) on the blackboard stack, invokes the target agent, then pops the scope when done
3. **MCP tools**: Dispatches to MCP server tools with namespace resolution
4. **Approval flows**: Checks tool approval requirements from scope policy

### Call Context Stack

```python
# When Agent A calls Agent B:
blackboard.push_call_context(
    calling_agent="A",
    called_agent="B",
    scope_id=f"scope_{uuid}"
)
# Agent B reads/writes to new scope
# ToolResultHandler pops context, routes result back to A
```

## Example Flow Through Control Nodes

```
chat_gate agent
  -> outputs: handoff_tf=true, switchboard_task="check weather"
  -> sets next_agent = "chat_task_router"

ChatTaskRouterNode
  -> reads handoff_tf from blackboard
  -> sets next_agent = "switchboard"

switchboard agent
  -> outputs: delegate_to="one_shot_tool_runner", action="get_weather"
  -> sets next_agent = "chat_switchboard_arguments_node" (or master_room_ variant)

ChatSwitchboardArgumentsNode / MasterRoomSwitchboardArgumentsNode
  -> normalizes tool_arguments dict (shared helper in _switchboard_arguments_util.py)
  -> domain-specific extras (master_room writes a dispatch marker; chat does not)
  -> sets next_agent = "tool_caller"
(The dayflow orchestrator's switchboard routes work-object NODES instead —
 create_dayflow_ticket / run_work_node via node_dispatch, not this path.)

ToolCaller
  -> resolves "get_weather" from tool registry
  -> executes tool with scope enforcement
  -> sets next_agent = "tool_result_handler"

ToolResultHandler
  -> processes result
  -> routes back to source agent or final_answer
```

## How to Add a New Control Node

1. Create file in `app/assistant/control_nodes/`
2. Inherit from base `ControlNode`
3. Implement `action_handler(message)` with deterministic logic
4. Read inputs from blackboard, write outputs to blackboard
5. Set `next_agent` on blackboard to route to next step
6. Reference the node in a manager's `state_map` to wire it into a flow

## Key Files

| File | Purpose |
|------|---------|
| `control_nodes/control_node.py` | Base class |
| `control_nodes/tool_caller.py` | Canonical tool/agent dispatcher |
| `control_nodes/tool_result_handler.py` | Result processing and context pop |
| `control_nodes/chat_task_router_node.py` | Chat handoff routing |
| `control_nodes/final_answer_node.py` | Output normalization and exit |
| `control_nodes/work_finalizer_node.py` | done→closed judgment (sole `closed` producer) |
| `lib/blackboard/Blackboard.py` | Scoped state stack |
