# Tools

Tools are executable capabilities that agents can invoke. They handle external integrations (Google Calendar, Gmail, weather APIs), device control, internal operations, **and entire sub-agent flows** — see [Managers as tools](#managers-as-tools-wrapper-pattern) below.

## Tool Structure

Tools live in two locations:
- **Thin wrappers**: `app/assistant/lib/tools/<tool_name>/` — entry point + argument models
- **Core implementations**: `app/assistant/lib/core_tools/<tool_name>/` — actual logic

```
lib/tools/get_weather/
  get_weather.py              # Thin wrapper: imports core tool, exports get_tool_class
  tool_contract.json          # Tool metadata: description, inputs, outputs, category
  tool_forms/
    tool_forms.py             # Pydantic argument models
  prompts/
    get_weather_description.j2  # One-line description for planner tool selection
    get_weather_args.j2         # Full prompt for ToolArguments agent
    get_weather_select.j2       # Brief description for tool selection context
lib/core_tools/weather_tool/
  weather_tool.py             # Actual implementation with execute()
```

## Managers as tools (wrapper pattern)

A first-class pattern in this codebase: a `MultiAgentManager` (see [02_MANAGERS](02_MANAGERS.md)) can be exposed as a tool by writing a thin `BaseTool` wrapper that delegates to `ManagerInterface`. From the calling agent's perspective, the manager is just another tool — same `tool_contract.json`, same compact card on the planner's tool list, same `ToolArguments` argument-filling, same `ToolCaller` dispatch. The whole sub-agent flow (delegator → planner → tools → loop → final answer) runs inside the wrapper's `execute()` and returns one `ToolResult`.

The wrapper itself is ~12 lines of boilerplate:

```python
# app/assistant/lib/tools/emi_team_manager/emi_team_manager.py
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.manager_interface.manager_interface import ManagerInterface

class emi_team_manager(BaseTool):
    def __init__(self):
        super().__init__('emi_team')
        self.manager_interface = ManagerInterface('emi_team_manager')

    def execute(self, tool_message):
        return self.manager_interface.execute(tool_message)


def get_tool_class():
    return emi_team_manager
```

`ManagerInterface` looks up the named manager via `multi_agent_manager_factory`, builds a `Message` from the tool arguments, calls `manager_invoker.invoke(...)`, and adapts the manager's `ToolResult` back into the calling tool flow. Scope context, blackboard, agent loop, max-cycles enforcement — all of that happens inside the manager invocation, transparent to the caller.

Examples currently shipped under `lib/tools/`:

| Wrapper tool | Manager invoked | Use |
|---|---|---|
| `emi_team_manager` | `emi_team_manager` | General-purpose worker — see [15_EMI_TEAM_AND_SCOPE](15_EMI_TEAM_AND_SCOPE.md) |
| `kg_explorer_manager` | `kg_explorer_manager` | Multi-step KG exploration |
| `devices_manager` | `devices_manager` | Smart-home device coordination |
| `entertainment_manager` | `entertainment_manager` | Music / video / chess |
| `event_manager` | `event_manager` | Calendar + reminder coordination |
| `fast_tool_manager` | `fast_tool_manager` | Single-step tool dispatch w/o full agent loop |
| `personal_admin_manager` | `personal_admin_manager` | Email / contacts / personal-data ops |
| `playwright_manager` | `playwright_manager` | Browser automation |

The wrapper directory needs the same `tool_contract.json`, `tool_forms/`, and `prompts/` as any other tool — argument schema is what the *outer* caller fills in; everything inside is the manager's business.

This is how a reasonably small agent gets the leverage of an entire sub-team without owning their config or loop. It also means tool-visibility tuning at the outer layer (`hidden_tools`, `tool_narrower`) controls which managers a given agent can invoke.

## Tool Contract

All tools extend `BaseTool` and implement:

```python
class MyTool(BaseTool):
    def __init__(self):
        super().__init__('my_tool')

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        arguments = tool_message.tool_data.get('arguments', {})
        # ... process arguments, call APIs, return result
        return ToolResult(result_type="success", content="Done", data_list=[...])
```

Each tool has a `tool_contract.json` with:
- `name`, `description` — what the tool does
- `inputs` — argument schema (name, type, required, description)
- `outputs` — result schema
- `arguments_prompt` — usage notes and behavior documentation
- `metadata` — category, verbs, entities, risk_level, side_effects, cost_level

## Tool Descriptions (Two Tiers)

Tools have two levels of description for different audiences:

1. **Planner-facing** (compact): Name + one-line description + required/optional arg names. Used by the planner for tool selection. No full schema — the planner's job is to pick the right tool, not fill in arguments.

2. **ToolArguments-facing** (full): Complete argument schema with types, descriptions, examples. Used by the `ToolArguments` agent when filling in the actual tool call.

The compact card is generated by `_format_compact_tool_card()` in the tool registry. It shows the description plus a brief argument summary.

## Tool Visibility and Narrowing

### Per-Manager Visibility

Each manager configures tool visibility in its `config.yaml`:

```yaml
tool_visibility:
  always_show: [find_tool, install_tool, ask_user]
  use_narrower: true
  hidden_tools:
    - get_email          # Hide raw tools; use managers
    - search_web         # Hide leaf; use web_manager
```

### Tool Narrower

When `use_narrower: true`, the `shared::tool_narrower` agent (gemini-flash) filters tools to those relevant for the current task. The narrower sees the **full** tool list (not pre-filtered by `hidden_tools`) so it can surface any tool the task needs, including leaf tools normally hidden.

When `use_narrower` is disabled, `hidden_tools` acts as the filter.

### Pinned Tools (Compiled Tasks)

Compiled task steps can specify `pinned_tools` — a list of exact tools the step needs. When pinned_tools are set, `ToolScopeService` skips all ranking, narrowing, and hiding — the manager gets exactly those tools.

```json
{
  "kind": "action",
  "executor": "emi_team_manager",
  "pinned_tools": ["write_text_file", "capture_and_describe_monitors"],
  "instruction": "..."
}
```

## Runtime System Variables

Tool arguments in compiled tasks can reference system variables resolved at execution time:

- `${now}` — current UTC ISO datetime
- `${now_local}` — current local ISO datetime
- `${today}` — today's date (YYYY-MM-DD)
- `${hours_ago_N}` — ISO datetime N hours before now (e.g. `${hours_ago_10}`)
- `${minutes_ago_N}` — ISO datetime N minutes before now
- `${artifact_N}` — output from a prior step
- `${prev_result}` — previous tool call's result within the same step

These are resolved by `_substitute_args()` and `_resolve_dynamic_time_vars()` in the tool sequence executor.

## Tool Registry

`app/assistant/lib/tool_registry/tool_registry.py`

Auto-discovers tools from the `tools/` directory. Each tool directory must export a `get_tool_class()` function.

```python
from app.assistant.ServiceLocator.service_locator import DI
tool_class = DI.tool_registry.get_tool_class("get_weather")
tool_instance = tool_class()
```

## Agent-to-Tool Flow

Agents don't call tools directly. The flow is:

1. Planner outputs `action: "get_weather"`, `action_input: {"city": "NYC"}`
2. `ToolArguments` agent fills in precise arguments from the tool's schema
3. `ToolCaller` (control node) dispatches execution
4. Tool returns `ToolResult`
5. `ToolResultHandler` stores result, routes back to planner

## Tool Access Control

Per-agent tool allowance is resolved dynamically:
1. `config.yaml` `allowed_tools` (list or "all")
2. Minus `except_tools`
3. Plus `task_allowed_tools` (dynamic, from blackboard)
4. Minus `task_except_tools`
5. Plus `dynamic_allowed_tools` (MCP tools)
6. Minus `dynamic_denied_tools`

## MCP Tools

Tools from MCP (Model Context Protocol) servers:
- Registered from cached MCP server definitions
- Namespaced as `mcp::<server_id>::<tool_name>`
- Follow the same description/argument schema contract
- Dispatched via ToolCaller like local tools

## Key Tools

### Email Tools
| Tool | Purpose |
|------|---------|
| `get_important_emails` | Fetch important inbox emails (importance >= 5, spam excluded). Agent-facing, ephemeral. |
| `get_email` | Background scheduler ingest tool. Writes to EventRepository. Not for agents. |
| `get_email_messages` | Raw Gmail query — full-fidelity, no filtering |
| `get_email_thread` | Full email conversation thread by thread_id or participant |
| `send_email` | Send via Gmail API |
| `trash_emails` | Bulk trash by sender |

### Device Tools
| Tool | Purpose |
|------|---------|
| `nest_home_control` | Nest thermostat control |
| `lights_control` | Smart light control (Kasa/TP-Link) |
| `ring_camera_control` | Ring camera control |
| `capture_and_describe_monitors` | Screenshot + LLM vision description + file write |

### File Tools
| Tool | Purpose |
|------|---------|
| `read_text_file` | Read file from repo |
| `write_text_file` | Write/overwrite file in repo |
| `append_text_file` | Append to file in repo |

### Web Tools
| Tool | Purpose |
|------|---------|
| `search_web` | Web search |
| `scrape_url` | Fetch and extract web page content |
| `peak_at_link` | Quick URL preview |
| `summarize_link` | Summarize a web page |

## How to Add a New Tool

1. Create directory: `app/assistant/lib/tools/<tool_name>/`
2. Create `tool_contract.json` with description, inputs, outputs, metadata
3. Create `tool_forms/tool_forms.py` with Pydantic argument models
4. Create the main tool file with `execute()` and `get_tool_class()`
5. Create `prompts/<tool_name>_description.j2` (one-liner for planners)
6. Create `prompts/<tool_name>_args.j2` (full argument prompt for ToolArguments)
7. The tool registry auto-discovers it — no manual registration needed
8. Reference the tool name in manager `config.yaml` `allowed_tools` to grant access

## Key Files

| File | Purpose |
|------|---------|
| `lib/tool_registry/tool_registry.py` | Auto-discovery, compact descriptions, access control |
| `lib/core_tools/base_tool/base_tool.py` | BaseTool base class |
| `lib/core_tools/tool_error_protocol.py` | Standard error format |
| `utils/pydantic_classes.py` | ToolMessage, ToolResult models |
| `control_nodes/tool_caller.py` | Canonical tool dispatcher |
| `manager_runtime/services/tool_scope_service.py` | Visibility, narrowing, pinned tools |
| `task_ir_runtime/task_ir_tool_sequence.py` | Deterministic tool sequence executor |
