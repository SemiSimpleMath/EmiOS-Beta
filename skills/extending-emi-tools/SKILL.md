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
├── __init__.py          # required (can be empty)
├── tool_contract.json   # required — schema, metadata, prompt copy
├── <name>.py            # required — the execute() function
├── prompts/
│   └── <name>_args.j2   # optional — extra arg-formulation guidance
└── tool_forms/
    └── <name>_form.py   # optional — Pydantic args model
```

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
    "domain": "email | calendar | smart_home | web | kg | …",
    "actions": ["send", "read", "delete", …],
    "selectors": ["recipient", "thread", …],
    "risk_level": "low | medium | high",
    "side_effects": "none | read | write",
    "requires_auth": ["google" | "ring" | …],
    "requires_network": true,
    "cost_level": "low | medium | high",
    "latency_class": "fast | moderate | slow"
  }
}
```

The `metadata` block drives tool gating and approval-prompt selection
in managers. `risk_level: high` + `side_effects: write` triggers the
approval modal automatically.

## execute() function

`<name>.py` exposes the entry point:

```python
from app.assistant.utils.pydantic_classes import Message

def execute(args: dict, *, message: Message, scope_context=None) -> dict:
    """
    Args:
      args         : validated tool arguments (already conforms to schema)
      message      : the Message that triggered this call
      scope_context: ScopeContext for entity / room / pod resolution
    Returns:
      dict with at minimum 'content' (str). May include 'data' (dict)
      for structured outputs that downstream agents read.
    """
    ...
    return {"content": "did the thing", "data": {"thing": "result"}}
```

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

- `risk_level: high` requires explicit user approval each call by
  default. Override at the agent level by listing the tool in
  `auto_approve_tools` (rarely the right answer).
- Use `scope_context.owner_id` for any KG access — the scope is the
  source of truth for which user/room owns a query.
- Tools should fail loud (raise) on unrecoverable errors. The agent
  runtime catches and reports them. Don't swallow exceptions and
  return `"content": "error: ..."`.
- Tests in `app/assistant/tests/tool_tests/`. The
  `tool_test_runner.py` helper exercises a tool with synthetic args.
