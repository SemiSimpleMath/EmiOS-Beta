---
name: extending-emi-tools
description: How to add a new tool to EmiOS. A tool is an executable capability with a JSON contract and an execute function. Use when the task involves creating, registering, or scaffolding a new tool agents can call.
license: Apache-2.0
metadata:
  author: emi-team
  version: "1.0"
  auto_inject_when:
    task_keywords:
      - "new tool"
      - "add tool"
      - "create tool"
      - "scaffold tool"
      - "register tool"
      - "extend emi tools"
---

# Adding a new tool

Tools live in `app/assistant/lib/tools/<name>/`. The `ToolRegistry`
discovers them on import — drop the directory, restart Flask, and
agents that include the tool in their `allowed_tools` can call it.

## Files to create

```
app/assistant/lib/tools/<name>/
├── <name>.py                          # required — exposes get_tool_class()
├── tool_contract.json                 # required — schema, metadata, planner copy
├── prompts/
│   └── <name>_description.j2          # required — planner-facing description
└── tool_forms/
    └── tool_forms.py                  # required — `<name>_args` + `<name>_arguments` Pydantic models
```

The registry loads all four pieces by these exact names. Argument-fill
guidance lives in the contract's `arguments_prompt` string (the args
agent reads it when filling a call).

## tool_contract.json shape

```json
{
  "name": "my_tool",
  "description": "One-line description of what the tool does. The planner agent reads this to decide when to call you.",
  "inputs": [
    { "name": "subject", "type": "string", "required": true,
      "description": "..." }
  ],
  "outputs": [
    { "path": "content", "type": "text",
      "description": "Human-readable result." },
    { "path": "data.thing", "type": "string",
      "description": "Structured field downstream agents read." }
  ],
  "arguments_prompt": "Detailed guidance for how to fill in the args. Examples are gold here.",
  "metadata": {
    "min_authority": 90,
    "approval_min_authority": 95,
    "domain": "email | calendar | smart_home | web | kg | …",
    "actions": ["send", "read", "delete", …],
    "selectors": ["recipient", "thread", …],
    "risk_level": "low | medium | high | critical",
    "side_effects": "read_only | write | external_action | destructive",
    "requires_auth": ["google" | "ring" | …],
    "requires_network": true,
    "cost_level": "low | medium | high",
    "latency_class": "fast | moderate | slow"
  }
}
```

Two metadata fields are ENFORCED at dispatch — set them deliberately:

- `min_authority` (0-100, declare it on every first-party tool): the
  see+use floor. Scopes below it never see the tool in their allowlists
  (tool_scope_service drops it at scope-build) and dispatch refuses it.
  A contract that omits the field fails closed at 99.
- `approval_min_authority` (0-100): scopes below it get an approval
  ticket before the tool executes; at or above, it runs directly.
  `approval_required: true` is the blunt always-ask variant. Per-room
  `scope.requires_approval_tools` lists add approval on top regardless.

The remaining fields (domain, actions, selectors, risk_level,
side_effects, requires_auth, requires_network, cost_level,
latency_class) are INFORMATIONAL today — they document the tool for
humans and audits; no runtime gate reads them. Fill them honestly, and
express any gating intent through the two enforced fields above.

## The tool class

`<name>.py` exposes `get_tool_class()` returning a `BaseTool` subclass
whose `execute` takes a `ToolMessage` and returns a `ToolResult`:

```python
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult


class MyTool(BaseTool):
    def __init__(self):
        super().__init__("my_tool")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments", {})
        scope = tool_message.scope_context   # entity / room / pod resolution
        ...
        return ToolResult(result_type="success", content="did the thing",
                          data={"thing": "result"})


def get_tool_class():
    return MyTool
```

Errors return through the typed protocol so the planner can react:
`make_tool_error(error_code=..., message=..., abort_policy="abort_tool",
retryable=...)` from `tool_error_protocol` — `abort_tool` lets the
planner recover; `abort_task` aborts the whole manager run (reserved
for fail-closed policy blocks).

`tool_forms/tool_forms.py` declares the matching Pydantic pair —
`<name>_args` (the fields) and `<name>_arguments` (`tool_name` +
`arguments: <name>_args`) — which the args agent fills and the
dispatcher validates.

If the tool needs DB access, use `get_db_manager().read_session()` /
`.transaction()`. If it needs an LLM, route through the agent system
— don't call providers directly.

## After dropping the files

1. Restart Flask.
2. Verify in logs: `ToolRegistry: loaded N tools` should include yours.
3. Add the tool name to whichever agents should be able to call it
   (their `config.yaml` `allowed_tools:` list).
4. The tool will appear in `/dev/agent-workbench` if dev tools are on.

## Canonical examples

- Simple read tool: `app/assistant/lib/tools/get_weather/`
- Write tool with approval gate: `app/assistant/lib/tools/send_email/`
- Network + auth: `app/assistant/lib/tools/get_email_messages/`
- Pod-aware tool: `app/assistant/lib/tools/pod_search/`

## Notes

- Approval is driven by the contract's `approval_min_authority` /
  `approval_required` plus per-room `scope.requires_approval_tools`
  (see the metadata section above). For argument-aware softening —
  "this specific invocation is safer than the default" — override
  `BaseTool.compute_approval_reduction` (send_email's recipient
  allowlist is the canonical example).
- Use `scope_context.owner_id` for any KG access — the scope is the
  source of truth for which user/room owns a query.
- Tools should fail loud (raise) on unrecoverable errors. The agent
  runtime catches and reports them. Don't swallow exceptions and
  return `"content": "error: ..."`.
- Tests in `app/assistant/tests/tool_tests/`. The
  `tool_test_runner.py` helper exercises a tool with synthetic args.
