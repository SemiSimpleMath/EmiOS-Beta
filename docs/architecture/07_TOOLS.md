# Tools

Tools are executable capabilities that agents can invoke. They handle external integrations (Google Calendar, Gmail, weather APIs), device control, internal operations, **and entire sub-agent flows** — see [Managers as tools](#managers-as-tools-wrapper-pattern) below.

## Tool Structure

Tools live in two locations:
- **Thin wrappers**: `app/assistant/lib/tools/<tool_name>/` — entry point + argument models
- **Core implementations**: `app/assistant/lib/core_tools/<tool_name>/` — actual logic

```
lib/tools/get_weather/
  get_weather.py              # Thin wrapper: imports core tool, exports get_tool_class
  tool_contract.json          # description, inputs, outputs, arguments_prompt, metadata
  tool_forms/
    tool_forms.py             # Pydantic argument models (<tool>_args + <tool>_arguments)
  prompts/
    get_weather_description.j2  # The ONLY .j2 a tool dir loads (planner-facing)
lib/core_tools/weather_tool/
  weather_tool.py             # Actual implementation with execute()
```

`ToolRegistry.load_prompts` loads **only** `<tool>_description.j2` as a Jinja
template — there are ~134 of them and zero `<tool>_args.j2` / `<tool>_select.j2`
in the tree. Argument-fill guidance that used to live in `<tool>_args.j2` now
lives in the contract's `arguments_prompt` string, served verbatim by
`get_tool_arguments_prompt()`.

### The three `get_tool_class()` patterns

`ToolRegistry.load_tool_class` execs `<tool_name>.py` and calls its
`get_tool_class()`. Three shapes coexist (all documented in the
`ToolRegistry.__init__` docstring):

1. **Self-class tool** — defines its own `BaseTool` subclass (CamelCase + `Tool`,
   e.g. `AppendTextFileTool`) and returns it. Most tools.
2. **Manager-as-tool** — wraps a `MultiAgentManager` via `ManagerInterface`; the
   class name is the dir name in snake_case (`class web_manager(BaseTool)`) so it
   lines up with the manager id. See [Managers as tools](#managers-as-tools-wrapper-pattern).
3. **Shared-core adapter** — a 3-line module pointing `get_tool_class` at a shared
   core class via `create_tool_loader` (`lib/tool_utils/shared_tool_loader.py`).
   Used when several tools delegate to one core — e.g. `create_calendar_event`,
   `delete_calendar_event`, `update_calendar_event` all share `CalendarTool`.

`load_tools` is fail-loud: any tool that won't import — or whose contract fails
the `min_authority` / `approval_min_authority` range checks — aborts boot rather
than silently vanishing from the registry.

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
| `kg_dev_manager` | `kg_dev_manager` | KG mutation / maintenance dev surface |
| `devices_manager` | `devices_manager` | Smart-home device coordination |
| `entertainment_manager` | `entertainment_manager` | Music / video / chess |
| `event_manager` | `event_manager` | Calendar + reminder coordination |
| `fast_tool_manager` | `fast_tool_manager` | Single-step tool dispatch w/o full agent loop |
| `personal_admin_manager` | `personal_admin_manager` | Email / contacts / personal-data ops |
| `playwright_manager` | `playwright_manager` | Browser automation (Playwright MCP) |
| `web_manager` | `web_manager` | Web research + the `web_*` snapshot-driven automation family |
| `work_web_manager` | `work_web_manager` | Graph-mode web worker (work-object nodes; display name "Webby (graph)") |
| `http_manager` | `http_manager` | REST/JSON API work via `http_request` |
| `bash_manager` | `bash_manager` | Shell command execution |
| `sandbox_manager` | `sandbox_manager` | Sandboxed code execution |
| `task_compile_manager` | `task_compile_manager` | Compile a task spec into an executable IR |

The `web_*` family is ~16 leaf tool dirs (`web_navigate_snapshot`,
`web_spatial_snapshot`, `web_click_ref_snapshot`, `web_fill_ref`,
`web_type_secret`, `web_visual_scout`, `web_modal_scan`, …) — a snapshot →
locate-by-ref → act loop for browser automation, normally fronted by
`web_manager` rather than exposed leaf-by-leaf.

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
- `arguments_prompt` — full argument-fill guidance (the old `<tool>_args.j2`)
- `metadata` — see below

### Contract metadata (`tool_registry._normalize_tool_metadata`)

The current taxonomy keys are **`domain` / `actions` / `selectors`**.
`category` / `verbs` / `entities` are legacy aliases — the normalizer reads
either and writes both, so old contracts keep working, but new ones use the new
names. On top of taxonomy, `_normalize_tool_metadata` resolves the access-control
and routing fields the four-layer gate reads:

| Field | Meaning |
|---|---|
| `min_authority` | L1 see+use floor (0–100). Parsed + range-checked at load; bad value aborts boot. |
| `approval_min_authority` | L2 approval threshold (0–100). When set, supersedes `approval_required` and the class `requires_approval` flag. |
| `approval_required` | Bool L2 trigger used when no `approval_min_authority` is declared. |
| `requires_auth` | List of credential namespaces (e.g. `["google"]`). |
| `requires_network` | Bool — tool reaches the network. |
| `latency_class` | `fast` / `medium` / `slow` hint. |
| `front_door` | Bool — surfaced as a primary capability. |
| `room_visibility_default` | `conditional` / `hide` — default visibility in rooms. |
| `planner_description` | Short capability line preferred by the compact card. |
| `risk_level`, `side_effects`, `cost_level` | Descriptive routing hints. |

Real examples (from the live contracts):

- **`get_weather`** — `min_authority: 10`, `approval_min_authority: 10`,
  `side_effects: read_only`. Any chat-tier scope, no approval.
- **`send_email`** — `min_authority: 90`, `approval_min_authority: 99`,
  `requires_auth: ["google"]`. Reachable by high-authority scopes; below 99 it
  fires an approval ticket (its `compute_approval_reduction` returns 95 for
  allowlisted recipients so dayflow at authority 95 can send without one).
- **`delete_calendar_event`** — `min_authority: 90`, `approval_required: true`,
  `approval_min_authority: 95`, `side_effects: destructive`, `room_visibility_default: hide`.
- **`http_request`** — `min_authority: 70`, `approval_min_authority: 70`,
  `front_door: true`. The generic REST tool; its pod-aware auth path is below.

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

## Tool Access Control (four-layer gate)

Tool access is no longer a flat allow/deny list. A tool call passes through four
gates; any one can stop it. The first two bound *which tools exist for this
caller* (the ceiling + visibility); the last two are the runtime authority and
approval walls enforced at execution by `ToolCaller`.

### Layer 0 — `allowed_tools` ceiling (manager ingress)

`ScopeAdapter.apply()` (`manager_runtime/services/scope_adapter.py`) runs at
every manager invocation and resolves the effective `scope.tools.allowed_tools`
the manager runs under. The inherited list is a **ceiling**, narrowed (never
widened) by `_apply_manager_narrowing`:

- A manager's **own surface** is its `scope_contract.tools.allowed_tools` if it
  declares one, else its config `tools.allowed_tools`.
- Being **granted** a manager grants its *whole* own surface. If the parent's
  list is `["all"]`, or names this manager, the manager's own surface stands as-is
  — **not** intersected with the parent's narrowed leaf list. This is the fix for
  sub-manager starvation: a manager reached through a narrowed parent
  (`master_room → emi_team → sandbox`) keeps its own tools instead of inheriting
  the parent's small allow-list.
- Otherwise the surface is bounded by the ceiling via
  `_intersect_allowed_tools(parent, child)` (an empty parent → `[]` = nothing,
  which is what keeps the breach wall standing).
- `blocked_tools` always **unions** down, so denylists keep propagating to children.
- A scope-level `per_manager[M]` rule (allow/block) folds into `allowed_tools` at
  narrowing time, so it binds at **execution** (`check_tool_access` reads
  `allowed_tools`), not just visibility — `allowed_tools` is the single source of
  truth.

The narrowed scope is projected onto runtime knobs (`task_allowed_tools`,
`task_except_tools`, `visible_tools`) by `_project_scope_to_runtime_data`, and
task-spec restrictions intersect on top.

### Layer 1 — visibility ceiling

`ToolPolicyResolver.get_visible_tools()`
(`agent_runtime/services/tool_policy_resolver.py`) narrows the allowed set to
what's *shown in the planner prompt*. `get_tools()` resolves config
`allowed_tools` / `except_tools`, then applies blackboard
`task_allowed_tools` / `task_except_tools` and the dynamic allow/deny lists
(the blackboard keys the narrower/MCP-install path write — `dynamic_allowed_tools`
/ `dynamic_denied_tools` are blackboard knobs, not literal contract fields).
`get_visible_tools()` then intersects with the precomputed `visible_tools`;
an empty intersection means *show nothing* — it never falls back to the full set
(agents that can be narrowed keep `find_tool` in `always_show` so they're never a
dead end). **Visibility never grants permission** — `always_show` is narrower-bypass
only and is explicitly not consulted by `check_tool_access`.

### Layer 2 — L1 authority floor (`min_authority`)

Enforced at execution. `ToolCaller` resolves the floor via
`resolve_tool_min_authority(tool_name, tool_config)` and passes it to
`check_tool_access` (`lib/tool_execution/tool_access_control.py`). A tool is
reachable only when `scope.approval.authority_level >= min_authority`:

- A first-party contract that omits `min_authority` **fails closed at 99** — a
  forgotten floor must never silently become wide-open.
- MCP / dynamic / core tools (no first-party contract) return `None` → no floor,
  bounded by the ceiling only.
- Authority `>= 100` (admin) clears every floor.

This is the wall the Telegram breach lacked: a low-authority guest could reach
`personal_admin_manager` (floor 90) because nothing checked authority at the
access layer. `check_tool_access` also still enforces the scope contract
(`allowed_tools` / `blocked_tools`) and task-level allow/deny, plus the
MCP-auto-permit policy below.

### Layer 3 — L2 approval (`approval_min_authority`)

`compute_approval_reasons` (`lib/tool_execution/tool_approval.py`) decides whether
a call needs the owner's sign-off and returns the reasons (empty = none):

- `scope.requires_approval_tools` naming the tool;
- `approval_min_authority` when `authority < approval_min_authority` (this
  supersedes `approval_required` and the class `requires_approval` flag);
- else `approval_required: true`, else `tool_class.requires_approval`.
- **Authority `>= 100` is the admin bypass** — returns `[]` for every tool.
- A tool may *soften* a specific invocation via
  `BaseTool.compute_approval_reduction(tool_message, authority)` (e.g. `send_email`
  returns 95 for allowlisted recipients), bypassing approval when the scope clears
  the softened bar.

When reasons are non-empty, `ToolCaller` calls `request_approval` →
`approval_gateway.request()`. **The gateway always homes the ticket to
`master_room`** (`_OWNER_ROOM_ID`) — approval is the owner's decision, so it
surfaces in the owner's UI with a provenance line ("Requested by … from … (authority N)"),
never inline in a guest's chat. It blocks polling the ticket state until accepted /
dismissed / expired, then `finalize_approval_ticket` marks it completed or failed
after the tool runs.

The whole flow is wired from `control_nodes/tool_caller.py::_execute_tool_call`:
resolve floor → `check_tool_access` → `compute_approval_reasons` →
(`request_approval` if any) → execute → `finalize_approval_ticket`.

`EMI_BYPASS_APPROVAL=1` short-circuits approval (test only).

## lib/tool_execution

`app/assistant/lib/tool_execution/` is the execution-control package `ToolCaller`
delegates to:

| Module | Responsibility |
|---|---|
| `tool_access_control.py` | `check_tool_access` + `resolve_tool_min_authority` (Layer 2 + scope/task gates) |
| `tool_approval.py` | `compute_approval_reasons`, `request_approval`, `finalize_approval_ticket` (Layer 3) |
| `mcp_tool_executor.py` | `execute_mcp_tool_call` — runs an MCP-backed tool and converts the response to a `ToolResult` |

## MCP Tools

Tools from MCP (Model Context Protocol) servers:
- Namespaced as `mcp::<server_id>::<tool_name>`
- Follow the same description/argument-schema contract (the contract is
  *generated* from the cached MCP `inputSchema`, not checked in)
- Dispatched via `ToolCaller` → `execute_mcp_tool_call` like local tools

The machinery (`app/assistant/lib/tool_registry/`):

- **`mcp_trust_policy.py`** — `TRUSTED_MCP_SOURCES` / `TRUSTED_MCP_SERVER_IDS` is
  the single allowlist; `is_trusted_mcp_server()` / `require_trusted_mcp_server()`
  gate discovery, registration, and install. Only servers from trusted origins
  (MCP official `time`, GitHub, Google Maps, Playwright) are usable.
- **Server directory + tool cache** — `ToolRegistry.load_mcp_servers()` loads
  curated entries from repo-root **`mcp/servers/`** (metadata only, no processes,
  no network); `load_mcp_tool_cache()` reads `mcp/tool_cache/*.json` and registers
  each tool via `_register_mcp_cached_tool` (backend `"mcp"`, generated Pydantic
  models + compact contract).
- **`mcp_install_registry.py`** — `installed_tools.json` (repo-root `mcp/`) records
  which tools were explicitly installed via the installer flow.
  `ToolRegistry.load_installed_mcp_tools(enabled_only=True)` registers exactly
  those; `ToolCaller` calls it each turn so a freshly installed tool is callable
  in the same run without a restart.
- **MCP auto-permit** — there is no `dynamic_allowed_tools` contract field. When
  `install_tool` is in the task allow-list, `check_tool_access` auto-permits
  already-installed MCP tools (`mcp::…`) in that same run — the installed-in-allowset
  policy.

## Pods as authority-banded secrets (courier)

`http_request` and the pod store implement the **courier** pattern: a tool can
relay a secret it never lets the LLM read. A secret is materialized into a pod
once, as a set of **authority-banded projections** —
`pod_store/materializers/` (`auth_bearer`, `auth_oauth`, `identity_ssn`) return
`ProjectionSpec`s at descending bands defined in `pod_store/authority.py`:

| Band | Const | Who |
|---|---|---|
| 10 | `AUTH_PUBLIC` | any agent — `redacted`, `format` |
| 50 | `AUTH_CHAT` | chat surfaces — `prefix`, `last4` |
| 70 | `AUTH_GATED` | sensitive-but-shareable — `area_code` |
| 99 | `AUTH_USER` | owner in-the-loop |
| 100 | `AUTH_COURIER` | deterministic code only — the `full` value |

The `full` projection is stored as a **pointer** (`storage_kind='env'` → env var,
or `'file'` → `data/pod_secrets/`), never as plaintext; only the low-authority
display projections are inline. The 99/100 cap is the wall: **no LLM agent can
read a 100-band projection** — `check_authority` raises `PodAuthorityError`
when the caller's `scope.approval.authority_level` is below the band, and the
materializer registry auto-discovers any new pod-type module dropped into the
directory.

The agent passes a reference, not the bytes: a header or body field is
`datapod:<kind>:<id>/<projection>`. At execution, `http_request` elevates to a
fresh courier scope via `ScopeAdapter.for_courier_call(authority_level=100)` and
resolves the reference through `pod_store/resolvers.py` — the resolved string
never enters the transcript. Authority 100 grants the approval admin-bypass so the
unseal itself doesn't fire a ticket. The reverse path (`seal_fields` /
`response_pod_kind`) seals an inbound credential (e.g. Bluesky `accessJwt`) into a
fresh pod and hands back a pod-ref the next call relays verbatim — keeping opaque
strings out of the transcript (LLM transcription corrupts them).

`http_request` is the generic REST tool (`min_authority: 70`); `http_manager`
fronts it as a manager-as-tool.

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
| `http_request` | Generic REST/JSON API tool with pod-aware (courier) auth |
| `web_*` family (~16) | Snapshot → locate-by-ref → act browser automation, fronted by `web_manager` |

## How to Add a New Tool

1. Create directory: `app/assistant/lib/tools/<tool_name>/`
2. Create `tool_contract.json` with description, inputs, outputs, `arguments_prompt`,
   and `metadata` (set `min_authority`, and `approval_min_authority` /
   `approval_required` if the tool needs sign-off — these are validated at load)
3. Create `tool_forms/tool_forms.py` with the `<tool_name>_args` and
   `<tool_name>_arguments` Pydantic models
4. Create the main tool file with `execute()` and `get_tool_class()`
5. Create `prompts/<tool_name>_description.j2` (the only `.j2` loaded; argument
   guidance goes in the contract's `arguments_prompt`, not a `_args.j2` file)
6. The tool registry auto-discovers it — no manual registration needed
7. Reference the tool name in manager `config.yaml` `allowed_tools` to grant access

## Key Files

| File | Purpose |
|------|---------|
| `lib/tool_registry/tool_registry.py` | Auto-discovery, contract normalization, compact descriptions |
| `lib/tool_registry/mcp_trust_policy.py` | MCP server allowlist |
| `lib/tool_registry/mcp_install_registry.py` | Installed-MCP-tool registry (`mcp/installed_tools.json`) |
| `lib/tool_execution/tool_access_control.py` | L1 floor + scope/task gates (`check_tool_access`) |
| `lib/tool_execution/tool_approval.py` | L2 approval decision + ticket lifecycle |
| `lib/tool_execution/mcp_tool_executor.py` | MCP-backed tool execution |
| `lib/core_tools/approval_gateway/approval_gateway.py` | Homes approval tickets to `master_room` |
| `lib/core_tools/base_tool/base_tool.py` | BaseTool base class (`compute_approval_reduction`, `describe_action`) |
| `lib/core_tools/tool_error_protocol.py` | Standard error format |
| `utils/pydantic_classes.py` | ToolMessage, ToolResult, ScopeContext models |
| `control_nodes/tool_caller.py` | Canonical tool dispatcher (wires the four-layer gate) |
| `manager_runtime/services/scope_adapter.py` | `allowed_tools` ceiling narrowing at manager ingress |
| `agent_runtime/services/tool_policy_resolver.py` | `get_tools` / `get_visible_tools` (visibility ceiling) |
| `manager_runtime/services/tool_scope_service.py` | Ranking, narrowing, pinned tools |
| `pod_store/materializers/` | Authority-banded secret projections (courier) |
| `pod_store/authority.py` | Pod authority bands + `check_authority` |
| `task_runtime/tool_executor.py` | Deterministic tool-node executor (gated by `check_tool_access`) |
