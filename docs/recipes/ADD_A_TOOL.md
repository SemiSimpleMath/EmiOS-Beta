# Recipe: Add a new tool

Tools are executable capabilities that agents invoke through `tool_caller`. Read [07_TOOLS.md](../architecture/07_TOOLS.md) for the contract.

## Two-directory layout

```
app/assistant/lib/tools/<tool_name>/        ← thin wrapper agents see
  __init__.py
  <tool_name>.py                            ← exports get_tool_class
  tool_contract.json                        ← metadata: description, inputs, outputs
  tool_forms/
    __init__.py
    tool_forms.py                           ← Pydantic argument models
  prompts/
    <tool_name>_description.j2              ← one-line for planner selection
    <tool_name>_args.j2                     ← full prompt for ToolArguments agent
    <tool_name>_select.j2                   ← brief description for tool selection context

app/assistant/lib/core_tools/<tool_name>/   ← actual implementation
  __init__.py
  <tool_name>_tool.py                       ← MyTool(BaseTool) with execute()
```

The tool registry auto-discovers `lib/tools/<dir>/`. You don't register manually.

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

## The thin wrapper

```python
# app/assistant/lib/tools/get_widget/get_widget.py
from app.assistant.lib.core_tools.get_widget.get_widget_tool import GetWidgetTool

def get_tool_class():
    return GetWidgetTool
```

That's literally it for the wrapper Python file. The registry calls `get_tool_class()`.

## `tool_contract.json`

```json
{
  "name": "get_widget",
  "description": "Fetch a widget by id from the widget store.",
  "inputs": [
    {"name": "widget_id", "type": "string", "required": true, "description": "Stable widget id"}
  ],
  "outputs": {
    "ok": "bool",
    "widget": "object — widget fields"
  },
  "arguments_prompt": "Always include `widget_id`. The id is the canonical identifier; do not pass `name` or `slug`.",
  "metadata": {
    "category": "data",
    "verbs": ["get", "fetch", "read"],
    "entities": ["widget"],
    "risk_level": "low",
    "side_effects": "none",
    "cost_level": "cheap"
  }
}
```

## `tool_forms/tool_forms.py`

```python
from pydantic import BaseModel, Field

class GetWidgetArgs(BaseModel):
    widget_id: str = Field(description="Stable widget id")
```

## Prompt fragments

`prompts/get_widget_description.j2` — one-line for planner tool selection:
```jinja2
get_widget — fetch a widget by id (returns widget fields)
```

`prompts/get_widget_args.j2` — full argument prompt for the ToolArguments agent:
```jinja2
Tool: get_widget
Purpose: fetch a widget by id from the widget store.

Arguments:
- widget_id (required, string): the widget's canonical id

Always include widget_id. The id is the canonical identifier — do not pass name or slug.
```

`prompts/get_widget_select.j2` — brief tool-selection description (often identical to description):
```jinja2
get_widget — read a widget by id
```

## Wire it into a manager

Two ways:
- **Per-agent allowlist**: add `"get_widget"` to that agent's `allowed_tools` list.
- **Manager-level visibility**: in the manager's `tool_visibility` block, leave it un-hidden so the narrower can suggest it.

Most tools are added via per-agent allowlists.

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

- **Forgot the `prompts/<tool>_args.j2`.** The ToolArguments agent has nothing to read; tool calls fail at argument-fill time, not at execution.
- **Multiple wrappers without a `tool_name` dispatch.** If you reuse one core class, the `execute` method needs to switch on `tool_message.tool_name`. Otherwise every wrapper runs the same default handler.
- **Tool returns raised exception instead of ToolResult.** The caller expects a ToolResult with `data["ok"]=False`; raising kills the manager loop. Wrap everything in a try/except and call `make_tool_error`.
- **`requires_approval` not honored.** If your tool is risky, set `metadata.requires_approval` in tool_contract.json and confirm the manager's scope grants the right authority level.

## See also

- [07_TOOLS.md](../architecture/07_TOOLS.md) — the tool contract, registry, scope filtering
- [Add an agent](ADD_AN_AGENT.md) — the agents that will call your tool
