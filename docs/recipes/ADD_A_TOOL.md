# Recipe: Add a new tool

Tools are executable capabilities that agents invoke through `tool_caller`. Read [07_TOOLS.md](../architecture/07_TOOLS.md) for the contract.

## Layout

```
app/assistant/lib/tools/<tool_name>/        ← the directory the registry discovers
  __init__.py
  <tool_name>.py                            ← exports get_tool_class (see the three patterns)
  tool_contract.json                        ← description, inputs, outputs, arguments_prompt, metadata
  tool_forms/
    __init__.py
    tool_forms.py                           ← Pydantic models: <tool_name>_args + <tool_name>_arguments
  prompts/
    <tool_name>_description.j2              ← the ONLY .j2 a tool dir loads (planner-facing)

# Optional second directory — only if you keep the implementation separate:
app/assistant/lib/core_tools/<core_name>/
  __init__.py
  <core_name>_tool.py                       ← MyTool(BaseTool) with execute()
```

`ToolRegistry.load_prompts` loads **only** `<tool_name>_description.j2`. There are no `<tool>_args.j2` or `<tool>_select.j2` files in the tree — argument-fill guidance now lives in the contract's `arguments_prompt` string (served verbatim by `get_tool_arguments_prompt()`), not a template.

The registry auto-discovers `lib/tools/<dir>/`; you don't register manually. Whether the implementation lives inline in `lib/tools/<name>/<name>.py` or in a separate `lib/core_tools/` module is up to you — both shapes ship in the tree (see the three `get_tool_class` patterns).

## The core implementation

```python
# app/assistant/lib/core_tools/get_widget/get_widget_tool.py
from __future__ import annotations
from typing import Any, Dict

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


class GetWidgetTool(BaseTool):
    def __init__(self) -> None:
        super().__init__("get_widget")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            arguments = (tool_message.tool_data or {}).get("arguments", {}) or {}
            widget_id = str(arguments.get("widget_id") or "").strip()
            if not widget_id:
                raise ValueError("widget_id is required")
            # ... do the work ...
            return self.publish_result(ToolResult(
                result_type="get_widget",
                content=f"Got widget {widget_id}: ...",
                data={"ok": True, "widget": {...}},
            ))
        except ValueError as e:
            return self.publish_error(make_tool_error(
                error_code="get_widget_invalid",
                message=str(e),
                abort_policy="abort_tool",
                retryable=False,
            ))
        except Exception as e:
            logger.error("get_widget failed: %s", e)
            logger.debug("get_widget exception details", exc_info=True)
            return self.publish_error(make_tool_error(
                error_code="get_widget_failed",
                message=str(e),
                abort_policy="abort_tool",
                retryable=False,
            ))

    def publish_result(self, result: ToolResult) -> ToolResult:
        return result

    def publish_error(self, error_result: ToolResult) -> ToolResult:
        return error_result
```

The error protocol: ALL errors return as `ToolResult(content=..., data={"ok": False, ...})` rather than raised exceptions. Callers expect `data["ok"]` semantics.

## Multi-tool dispatch (one core, many wrappers)

If you have several closely-related tools that share core logic, write one `BaseTool` subclass with multiple `handle_<tool_name>` methods, dispatched in `execute`:

```python
def execute(self, tool_message: ToolMessage) -> ToolResult:
    tool_name = tool_message.tool_name or (tool_message.tool_data or {}).get("tool_name")
    handler = getattr(self, f"handle_{tool_name}", None)
    if handler is None:
        raise ValueError(f"Unsupported tool_name for {self.name}: {tool_name}")
    arguments = (tool_message.tool_data or {}).get("arguments", {}) or {}
    return handler(arguments, tool_message)
```

Then create one `lib/tools/<name>/` directory per name, each pointing back at the same core class. See `app/assistant/lib/core_tools/kg_mutator/kg_mutator_tool.py` for the canonical example (one core, six tool names).

## The `<tool_name>.py` — three `get_tool_class()` patterns

`ToolRegistry.load_tool_class` execs `<tool_name>.py` and calls its `get_tool_class()`. Three shapes coexist (all valid — documented in the `ToolRegistry` docstring):

**1. Self-class tool (most tools).** Define the `BaseTool` subclass right here (CamelCase, e.g. `GetWidget`) and return it. The whole implementation can live in this file — see `lib/tools/get_weather/get_weather.py` and `lib/tools/send_email/send_email.py`.

```python
# app/assistant/lib/tools/get_widget/get_widget.py
class GetWidget(BaseTool): ...
def get_tool_class():
    return GetWidget
```

**2. Shared-core adapter.** When several tools delegate to one core class, the wrapper is three lines using `create_tool_loader` (`lib/tool_utils/shared_tool_loader.py`). Real example: `create_calendar_event` / `delete_calendar_event` / `update_calendar_event` all point at `CalendarTool`; the six `kg_*` mutators all point at `KGMutatorTool`.

```python
# app/assistant/lib/tools/create_calendar_event/create_calendar_event.py
from app.assistant.lib.core_tools.calendar_tool.calendar_tool import CalendarTool
from app.assistant.lib.tool_utils.shared_tool_loader import create_tool_loader

get_tool_class = create_tool_loader(CalendarTool)
```

**3. Manager-as-tool.** Wrap a `MultiAgentManager` via `ManagerInterface`; the class name is the dir name in snake_case so it lines up with the manager id. See [07_TOOLS.md](../architecture/07_TOOLS.md#managers-as-tools-wrapper-pattern).

`load_tools` is fail-loud: any tool that won't import, or whose contract fails the `min_authority` / `approval_min_authority` range checks, aborts boot.

## `tool_contract.json`

`arguments_prompt` is a **top-level** key (not under `metadata`) — it's the full argument-fill guidance the `ToolArguments` agent reads. The metadata taxonomy keys are **`domain` / `actions` / `selectors`** (the old `category` / `verbs` / `entities` are legacy aliases the normalizer still reads, but use the new names). `metadata` also carries the access-control fields the four-layer gate reads:

```json
{
  "name": "get_widget",
  "description": "Fetch a widget by id from the widget store.",
  "inputs": [
    {"name": "widget_id", "type": "string", "required": true, "description": "Stable widget id"}
  ],
  "outputs": [
    {"path": "content", "type": "text", "description": "Human-readable result."},
    {"path": "data", "type": "object", "description": "{ok, widget}"}
  ],
  "arguments_prompt": "Always include `widget_id`. The id is the canonical identifier; do not pass `name` or `slug`.",
  "metadata": {
    "min_authority": 10,
    "approval_min_authority": 10,
    "domain": "data",
    "actions": ["get", "fetch", "read"],
    "selectors": ["widget"],
    "risk_level": "low",
    "side_effects": "read_only",
    "requires_auth": [],
    "requires_network": false,
    "cost_level": "low",
    "latency_class": "fast"
  }
}
```

Access-control fields (validated at load — a bad value aborts boot):

- **`min_authority`** (0–100) — the L1 see+use floor. A first-party contract that omits it **fails closed at 99**, so always set it. A read-only tool like `get_weather` uses `10`; a destructive one like `send_email` uses `90`.
- **`approval_min_authority`** (0–100) — the L2 approval threshold. When the caller's authority is below it, the call fires an approval ticket. Set it (or `approval_required: true`) only if the tool needs the owner's sign-off; `delete_calendar_event` uses `95`, `send_email` uses `99`.
- **`requires_auth`** — list of credential namespaces (e.g. `["google"]`); **`requires_network`** — bool.

See `lib/tools/get_weather/tool_contract.json` (no-auth read, floor 10) and `lib/tools/send_email/tool_contract.json` (floor 90, approval 99, `requires_auth: ["google"]`) for live examples.

## `tool_forms/tool_forms.py`

Two models, snake_case, named `<tool_name>_args` (the arguments themselves) and `<tool_name>_arguments` (a wrapper carrying `tool_name` + the args). This matches every shipped tool — see `lib/tools/get_weather/tool_forms/tool_forms.py`.

```python
from pydantic import BaseModel, Field


class get_widget_args(BaseModel):
    widget_id: str = Field(description="Stable widget id")


class get_widget_arguments(BaseModel):
    tool_name: str
    arguments: get_widget_args
```

## Prompt fragment (one file)

`prompts/get_widget_description.j2` is the **only** `.j2` a tool directory loads — a one-liner for planner tool selection:

```jinja2
get_widget — fetch a widget by id (returns widget fields)
```

Do not create `_args.j2` or `_select.j2`; the registry doesn't load them. The full argument-fill guidance that used to live in `_args.j2` now goes in the contract's `arguments_prompt` string.

## The four-layer access gate

A tool call passes through four gates; any one can stop it. The first two bound *which tools exist for this caller*; the last two are enforced at execution by `ToolCaller`. Know which layer your contract values feed:

1. **`allowed_tools` ceiling** (manager ingress) — `ScopeAdapter` resolves the manager's effective tool surface, narrowing the inherited list (never widening). This is what a manager's `tools.allowed_tools` / `scope_contract.tools.allowed_tools` / `blocked_tools` set.
2. **Visibility ceiling** — `ToolPolicyResolver.get_visible_tools()` narrows the allowed set to what's shown in the planner prompt. `tool_visibility.always_show` / `hidden_tools` and the narrower live here. **Visibility never grants permission.**
3. **L1 authority floor** — `min_authority` from your contract. The call is reachable only when `scope.authority_level >= min_authority`. Omitting it fails closed at 99.
4. **L2 approval** — `approval_min_authority` / `approval_required` from your contract. Below the threshold, `ToolCaller` fires an approval ticket (always homed to `master_room`) before running the tool.

Full detail in [07_TOOLS.md](../architecture/07_TOOLS.md#tool-access-control-four-layer-gate).

## Wire it into a manager

Granting a tool and showing a tool are different layers:

- **Grant (the gate that matters):** add `"get_widget"` to the manager's `tools.allowed_tools` (and, if the manager declares one, `scope_contract.tools.allowed_tools`). This is the ceiling — without it the tool is unreachable no matter what else you set.
- **Show:** add it to `tool_visibility.always_show`, or leave it un-hidden so the narrower can surface it. Visibility only affects what the planner sees; it never grants reachability.
- **Authority:** confirm the caller's scope clears your `min_authority`. A high-floor tool is invisible-by-floor to low-authority surfaces even when allowed.

## Test it

```bash
.venv\Scripts\python.exe -c "
import app.assistant.tests.test_setup
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import ToolMessage
tool = DI.tool_registry.get_tool_class('get_widget')()
msg = ToolMessage(tool_name='get_widget', tool_data={'arguments': {'widget_id': 'w_1'}})
print(tool.execute(msg))
"
```

## Common pitfalls

- **Put argument guidance in a `<tool>_args.j2`.** The registry never loads it. Argument-fill guidance goes in the contract's top-level `arguments_prompt` string.
- **Omitted `min_authority`.** A first-party contract with no `min_authority` fails closed at 99 — effectively admin-only. Always set the floor.
- **Multiple wrappers without a `tool_name` dispatch.** If you reuse one core class (shared-core adapter), the `execute` method must switch on `tool_message.tool_name` (`handle_<tool_name>` dispatch — see `lib/core_tools/kg_mutator/kg_mutator_tool.py`). Otherwise every wrapper runs the same default handler.
- **Tool returns a raised exception instead of a ToolResult.** The caller expects a ToolResult (`data["ok"]=False` on failure); raising kills the manager loop. Wrap everything in try/except and return `make_tool_error(...)`.
- **Approval not honored.** Set `metadata.approval_min_authority` (or `approval_required: true`) in the contract, and confirm the caller's scope authority is below it — the gate only fires when authority < threshold.

## See also

- [07_TOOLS.md](../architecture/07_TOOLS.md) — the tool contract, registry, scope filtering
- [Add an agent](ADD_AN_AGENT.md) — the agents that will call your tool
