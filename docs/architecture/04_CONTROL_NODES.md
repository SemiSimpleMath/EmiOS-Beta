# Control Nodes

Control Nodes are Python orchestration steps that execute within the agent loop. They handle routing decisions, tool dispatch, data normalization, and exit logic. Their own routing can be deterministic while their implementation calls tools or LLM agents (for example, work architecture and finalization).

## Base Class

`app/assistant/control_nodes/control_node.py`

Interface: `action_handler(message: Message)` — same as agents, so managers route to them interchangeably.

## Control Node Categories

### Router Nodes
Route based on blackboard state:

- **`chat_task_router_node.py`** — Routes chat responses: if `handoff_tf=true` -> switchboard, else -> final answer
- **`master_room_chat_task_router_node.py`** — Master room variant: adds dayflow delegation path
- **`action_selector_router_node.py`** — Dayflow: routes **everything** to the switchboard, one path for
  all dispatches. A `ChatTaskRouterNode` subclass whose only override is `_cfg()`, reading
  `flow_config.action_selector` instead of `flow_config.chat_gate`. (It branched on ticket vs handoff
  before the work-object cutover; there is no ticket branch here now — the switchboard decides.)
- **`work_node_wake_prep_node.py`** — Head of `dayflow_wake_manager`: stages the one due node for the state_mover, or ends the pass if it is no longer ready
- **`work_node_wake_router_node.py`** — After the state_mover in the wake pass: dispatch the node if left `actionable`, end the pass if held
- **`tool_return_router.py`** — Validates the result-handler source, saves the calling agent as `resume_target`, and leaves routing to `state_map`

(Other room-specific routers follow the same shape:
`kg_dev_chat_task_router_node.py`, `geoguessr_router_node.py`,
`doc_create_final_router_node.py`, `plan_mode_final_router_node.py`
(master_room only — the dayflow plan_mode was retired 2026-09-18),
`task_create_final_router_node.py`, `task_spec_router_node.py`.)

### Tool/Action Execution

- **`tool_caller.py`** — The canonical dispatcher:
  - Is the action a **tool**? -> Execute via tool registry with scope enforcement
  - Is the action an **agent**? -> Push call context on blackboard, invoke agent
  - Is the action a **control node**? -> Set `next_agent` to the control node
  - Manages scope creation, approval flows, MCP tool execution
  - Creates `blackboard.push_call_context()` for agent-to-agent calls
  - Surface-specific subclasses route a single room's dispatch: `chat_tool_caller.py`,
    `master_room_tool_caller.py` (shared helpers in `_tool_caller_util.py`).

- **`tool_result_handler.py`** — Records tool results in the current scope; its separate agent-return path pops the nested call context
- **`tool_approve_node.py`** (`ToolApproveNode`) — Resolves a tool's approval gate
  before dispatch (raises the owner ticket / blocks on the decision)

### Data Transform Nodes

- **`task_compile_metadata_node.py`** — Builds task metadata
- **`task_compile_final_output_node.py`** — Compiles final task output

### Flow Gates & Critics

- **`critic_pre_node.py` / `critic_post_node.py` / `critic_capture_node.py`** (`CriticPreNode` etc.) — Critic loop: pre-check, post-check, and capture of the critic verdict around an agent step
- **`relevance_cleaner_gate_node.py`** — RETIRED at the work-object cutover; on disk but wired into no manager (paired with `relevance_cleaner_prep_node.py` / `relevance_cleaner_persist_node.py`)
- **`triage_spawn_guard_node.py`** (`TriageSpawnGuardNode`) — Guards intake-triage spawning
- **`task_compile_critic_node.py`** (`TaskCompileCriticNode`) — Quality gate on compiled task output

### Exit/Return Nodes

- **`final_answer_node.py`** — Normalizes output and sets exit flag
- **`graceful_exit_control_node.py`** — Writes an abort report for error/budget exit routing
- **`manager_exit_node.py`** — Materializes the current manager's final answer if needed and sets `exit`

### Dayflow / Work-Object Nodes

The work-object cutover (2026-06) replaced the item-dispatch lane with the work-object
tick pipeline; `action_result_normalizer_node` was deleted with it.
`dayflow_switchboard_arguments_node` and `dayflow_tool_caller` were **not** deleted —
they are live, and since the 2026-09-17 dispatch split they are the first two stages of
`dayflow_dispatch_manager` (arguments → tool call → `work_finalizer_node`), which runs one
claimed node on its own thread. Its control nodes:

- **`strategic_planner_wo_prep_node.py` / `_persist_node.py`** — build the
  evaluator's context (portfolio, ticket replies with resolved work refs,
  `expected_schedule_view` with id-chain provenance) / persist its verdicts
  (mint/change/complete/abandon via `work_persist`, consume cited intake items,
  forward `concern:` refs onto the work object)
- **`work_finalizer_node.py`** — judges whether each completed node's GOAL was achieved;
  sole producer of the `closed` terminal; runs in `dayflow_dispatch_manager`, not the tick
- **`work_architect_node.py`** — per-goal DAG decomposition + re-plan
- **`work_repair_node.py`** — RETIRED 2026-09-16; on disk but wired into no manager. Its
  three dispositions became the finalizer's verdicts (retry / unrecoverable + stop /
  new_approach / ask_user)
- **`work_node_dispatch_node.py`** — the CLAIM GATE, and nothing else: canonicalize the selector's
  pick, publish `work_node_ref`, mark the node `dispatched`, hand off to
  `work_session.open_session`, end the planning pass. It calls no tool and chooses no branch —
  the switchboard already named the tool, and the dispatch room makes the call on its own thread
- **`work_node_materializer_node.py`** — **builds the actionable list and nothing else**: one item
  per `actionable` non-goal node (`item_id = work_id::node_id`), skipping terminal work objects and
  anything the state_mover has not promoted. Empty list → short-circuits to
  `post_room_finalize_node`. It used to carry a reply pre-step that scanned the ticket store for
  answers to in-flight asks; that was removed on purpose — landing a tool result was never this
  node's job, and doing it here put it seven stops into the tick, behind the steward. A ticket
  response is now recorded by the ask's own session, whose tool call returns the user's reply
- **`workobject_render_node.py`** — renders the work-object view a worker sees. Wired into the
  WORKER managers (`work_emi_team_manager`, `work_web_manager`), not the dayflow orchestrator —
  it serves the `node_input: render` handoff shape
- **`state_mover_prep_node.py` / `_persist_node.py`** — is_ready promotion + HOLD
- **`intake_triage_prep_node.py` / `triage_persist_node.py`**,
  **`context_enricher_prep_node.py` / `_persist_node.py`** — intake triage and
  enrichment prep/persist pairs
- **`post_room_finalize_node.py`** — the universal EXIT node: every dayflow path lands here,
  including the materializer's empty-list short-circuit and both wake-pass early exits. It closes
  acted_on items, stamps `execution_result` onto plan steps via `result_formatter`, writes action-log
  rows, and persists planned tasks / synopses / plan completions. It is also the tick's strictest
  validator (seven list-typed keys, three contract validators, three cross-checks that raise).
  **Most of its work is dead code** — the blackboard keys it reads have no producers left; see
  `05a` for the table
- **`dag_executor_node.py` / `dag_manager_control_node.py`** — DAG-shaped
  multi-step execution (wired in `multi_tool_manager`, not the dayflow orchestrator)

### Node families

Many nodes come in per-agent **prep/persist pairs**: a `*_prep_node.py` loads that
agent's context off the items table before it runs, and a `*_persist_node.py` writes its
output back. Examples: `context_enricher_prep_node` / `context_enricher_persist_node`,
`relevance_cleaner_prep/persist`, `state_mover_prep/persist`, `triage_persist_node`,
`summary_pre/post_node`, `task_compile_metadata/post/final_output_node`. (`planner_persist_node` was
listed here until 2026-09-18 — it was deleted with the legacy plan-task planner and no such file
exists.)
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

1. **Tool execution**: Resolves tool from registry, validates inherited scope, executes
2. **Agent-to-agent calls**: Pushes a call context, invokes the target agent synchronously, stores its returned payload, then calls `ToolResultHandler.action_handler`; that handler reads the child result before popping and records the response in the parent scope
3. **MCP tools**: Dispatches to MCP server tools with namespace resolution
4. **Approval flows**: Checks tool approval requirements from scope policy

For a standard tool, ToolCaller stays in the current scope and directly calls
`ToolResultHandler.process_tool_result_direct` with the returned ToolResult,
including a blocked approval result. It does not schedule that handler with
`next_agent`. An exception escaping execution instead sets manager error state.
The normal result path clears `next_agent` and lets `state_map` route from the
handler; clearance failures and install-approval routing are explicit exceptions.
`ToolReturnRouter` records the eventual resume target so configured processing
nodes can run before the caller resumes.

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

Schematic routing example; each manager config chooses its concrete nodes.
The shared ToolCaller/result-handler segment below shows direct result handling.

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
 create_dayflow_ticket / work_emi_team_manager via the dispatch room, not this path.)

ToolCaller
  -> resolves "get_weather" from tool registry
  -> executes tool with scope enforcement
  -> directly calls tool_result_handler.process_tool_result_direct(result)

ToolResultHandler
  -> processes result
  -> records result in the current scope; sets last_agent to its own name
  -> normally clears next_agent; the next Delegator pass uses state_map

ToolReturnRouter (if configured on that path)
  -> records resume_target; state_map chooses the next processing/resume node
```

## How to Add a New Control Node

1. Create a file in `app/assistant/control_nodes/`. **The filename is the contract**:
   it becomes the registry key, and the class inside must be its CamelCase form
   (`work_node_wake_prep_node.py` → `WorkNodeWakePrepNode`). A file whose name starts
   with `_` is treated as a helper module and never loaded as a node.
2. Inherit from base `ControlNode`. The class must be **defined in that module** — an
   imported `ControlNode` subclass is deliberately ignored, so a node that imports another
   node's class still resolves to its own. If a module defines several local subclasses and
   none matches the expected name, boot raises "Ambiguous control node classes".
3. Implement `action_handler(message)` with deterministic logic. Conventionally clear the
   route first (`self.blackboard.update_state_value("next_agent", None)`) and set
   `last_agent` to your own name at the end.
4. **Read inputs from the BLACKBOARD, never from `message.data`.** The manager copies the
   trigger's `data` onto the blackboard once, at `request_handler`; the activation Message a
   node receives carries none of it. Reading `message.data` yields nothing, silently, and
   the node falls through to its state_map default — the bug that made every dayflow time
   wake run a full planning tick for two days (fixed 2026-09-18).
5. Set `next_agent` to route explicitly (the Delegator honours it and returns early), or
   leave it `None` to let the manager's `state_map` decide.
6. Declare the node in a manager's `control_nodes:` with `name:` + `class:`, and reference
   that `name` in the `state_map`. At construction, values outside the configured
   names plus role-binding keys/values raise `ValueError`. Also verify that aliases
   resolve to loaded instances: the membership check does not prove that.

Two conveniences from the base class worth using instead of re-inventing:

- **Config**: `_node_cfg()` reads this node's own `config:` block from the manager entry;
  `_flow_section_cfg("<section>")` reads a `flow_config` section; `_merged_section_node_cfg`
  overlays the two with the node's own winning. Validators: `_required_str`,
  `_optional_str`, `_optional_node`.
- **Returning to a caller**: `_pop_and_route_to_calling_agent()` pops the call-context
  triple `(calling_agent, called_agent, scope_id)` and routes back to the caller.

A loop-selected `ControlNode` does not increment `max_cycles`; a loop-selected
non-control agent does. Agents/tools called inside a node are outside that
counter, so this is not a total LLM-call or wall-clock budget. A separate
iteration backstop (`max_cycles * 8`, minimum 40) catches control-node loops.

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
