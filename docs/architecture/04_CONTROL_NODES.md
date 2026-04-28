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
- **`dayflow_ticket_builder_router_node.py`** — Routes ticket_builder output to create_dayflow_ticket
- **`tool_return_router.py`** — Routes tool results back to the calling agent

### Tool/Action Execution

- **`tool_caller.py`** (~710 lines) — The canonical dispatcher:
  - Is the action a **tool**? -> Execute via tool registry with scope enforcement
  - Is the action an **agent**? -> Push call context on blackboard, invoke agent
  - Is the action a **control node**? -> Set `next_agent` to the control node
  - Manages scope creation, approval flows, MCP tool execution
  - Creates `blackboard.push_call_context()` for agent-to-agent calls

- **`tool_result_handler.py`** — Processes tool results and pops call context

### Data Transform Nodes

- **`action_result_normalizer_node.py`** — Converts tool outputs to standard format
- **`view_materializer_node.py`** — Builds views from state
- **`task_compile_metadata_node.py`** — Builds task metadata
- **`task_compile_final_output_node.py`** — Compiles final task output

### Flow Gates

- **`plan_gate_node.py`** — Validates plan outputs
- **`critic_gate_node.py`** — Quality gates
- **`cooldown_gate_node.py`** — Timing gates

### Exit/Return Nodes

- **`final_answer_node.py`** — Normalizes output and sets exit flag
- **`graceful_exit_control_node.py`** — Handles clean exits
- **`manager_exit_node.py`** — Multi-manager exit coordination

### Dayflow-Specific Nodes

- **`dayflow_switchboard_arguments_node.py`** — Validates action, persists dispatch records (`action_dispatch:UUID` items), stamps `dispatched_at` on acted-on tasks, then routes to tool
- **`dayflow_ticket_builder_router_node.py`** — Routes ticket_builder output into `create_dayflow_ticket` dispatch with `trigger_context.source_task_ids` for feedback loop

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
  -> sets next_agent = "room_switchboard_arguments"

RoomSwitchboardArgumentsNode
  -> normalizes tool_arguments dict
  -> sets next_agent = "tool_caller"

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
| `control_nodes/dayflow_switchboard_arguments_node.py` | Dayflow dispatch records |
| `lib/blackboard/Blackboard.py` | Scoped state stack |
