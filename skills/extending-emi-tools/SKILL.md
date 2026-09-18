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

Tools live in `app/assistant/lib/tools/<name>/`. `ToolRegistry.load_tools()`
scans that directory at boot (from `bootstrap`) — drop the directory, restart
Flask, and agents that include the tool in their `allowed_tools` can call it.

## Files to create

```
app/assistant/lib/tools/<name>/
├── <name>.py                          # required — exposes get_tool_class()
├── tool_contract.json                 # effectively required — see below
├── prompts/
│   └── <name>_description.j2          # dir required; template strongly advised
└── tool_forms/
    └── tool_forms.py                  # required — `<name>_args` + `<name>_arguments` Pydantic models
```

The registry loads all four pieces by these exact names. Argument-fill
guidance lives in the contract's `arguments_prompt` string (the args
agent reads it when filling a call).

**Exactly how strict each piece is** — the difference matters when something
doesn't show up:

| Piece | If absent |
|---|---|
| `<name>.py` | the directory is **skipped silently** (debug log only) — the usual reason a new tool "isn't registered" |
| `get_tool_class()` inside it | raises → boot fails |
| `tool_forms/tool_forms.py`, or either model | raises → boot fails |
| `<name>_args` / `<name>_arguments` not `BaseModel` subclasses | raises → boot fails |
| `prompts/` directory | raises → boot fails |
| `prompts/<name>_description.j2` | tolerated: logs an error and the planner sees "No description available." |
| `tool_contract.json` | tolerated, **but `min_authority` then fails closed at 99**, so no scope below 100 can see or call the tool. 141 of the 142 tools in the repo ship one. |
| a malformed `tool_contract.json` | raises → boot fails (deliberate: swallowing it would silently strip the authority gate) |

Two directory-level escapes: a `.disabled` file in the tool directory skips it,
and directories starting with `__` are ignored.

> **A tool that fails to load stops the process.** `load_tools` collects failures
> and raises `RuntimeError("refusing to start with a partial registry")`. A
> security-gated tool must never silently vanish while boot reports success.

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
    "planner_description": "One short line — this IS the planner's capability line.",
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
  see+use floor, enforced at BOTH gates — `tool_scope_service._filter_to_ceiling`
  drops the tool from the visible list, and `check_tool_access` refuses it at
  execution ("requires authority N; this scope has M"). A first-party contract that
  omits the field **fails closed at 99**. MCP and contract-less core/dynamic tools
  are exempt (no floor at all — the `allowed_tools` ceiling is their only gate), so
  the 99 can never strand them.
- `approval_min_authority` (0-100): scopes below it get an approval
  ticket before the tool executes; at or above, it runs directly
  (`tool_execution/tool_approval.py`). `approval_required: true` is the blunt
  always-ask variant, consulted only when no threshold is set. Per-room
  `scope.requires_approval_tools` lists add approval on top regardless.

**`domain`, `actions` and `selectors` are NOT informational** — they decide whether a
planner ever SEES your tool, which matters as much as the gates above:

- `KeywordMetadataToolRanker` scores every tool against the task. An exact match on
  `domain` is the heaviest signal in the table (weight 10 — above an exact tool-name
  match at 8); `actions` hits score 4 and `selectors` 3, and all three also feed the
  general text match. A low-scoring tool sinks down the visible list.
- `tool_domain_filter.filter_candidates` runs a **deterministic pre-filter** on
  `domain` + `actions` before the LLM narrower — and on a tight, confident match
  (inferred domains, ≤10 candidates, no unmigrated contracts) it **short-circuits the
  narrower entirely**, so those two fields alone choose the visible set.

Get them wrong and the tool is invisible to the planner that needed it, with no error
anywhere. Note both accept legacy names: `category`/`verbs`/`entities` are read as
fallbacks for `domain`/`actions`/`selectors`.

Genuinely informational today (parsed and carried, but no reader found outside the
registry): `risk_level`, `side_effects`, `requires_auth`, `requires_network`,
`cost_level`, `latency_class`, `front_door`, `room_visibility_default`. Fill them
honestly for humans and audits, and express gating intent through the enforced fields.

### What the planner actually sees — and the caps that truncate it

`get_tool_descriptions()` renders a **card**, not your full description:

- **capability line** = `metadata.planner_description` if set, truncated at **200
  characters**; otherwise the first sentence of `description`, truncated at **140**
- **one line per input**: `· name (req|opt): hint`, where the hint is the first
  sentence of that input's `description`, truncated at **120**

So `planner_description` and `inputs[].description` are the two fields that decide
whether a planner picks your tool and fills it correctly in one pass. Curate them.
`arguments_prompt` is deliberately **not** in the card — it is served separately to
the args agent when a call needs filling.

An entry in `inputs` missing either `name` or `type` is **silently dropped** during
normalization, so a typo'd field simply never reaches the planner.

## The tool class

`<name>.py` exposes `get_tool_class()`. Three patterns are in use and all are
valid:

1. **Self-class tool** (most tools) — define a `BaseTool` subclass in the file and
   return it. Class name is CamelCase with a `Tool` suffix.
2. **Manager-as-tool** — wrap a multi-agent manager via `ManagerInterface`; the
   class is named in snake_case to match the directory and manager id (e.g.
   `class web_manager(BaseTool)`). Every `*_manager/` tool directory is this shape.
3. **Shared-core adapter** — a three-line module pointing at a shared core class:
   `get_tool_class = create_tool_loader(CalendarTool)`. Used when several tools
   delegate to one core (the calendar create/update/delete trio).

The common case, returning a `BaseTool` subclass whose `execute` takes a
`ToolMessage` and returns a `ToolResult`:

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

1. Restart Flask. If your tool is malformed, **boot fails** with
   "refusing to start with a partial registry" naming it — that is the real test.
   A successful load logs `Registered tool: <name>`.
2. If the tool is simply absent from the registry with no error, the directory was
   skipped: check that `<name>.py` exists and is named after the directory.
3. Add the tool name to whichever agents should be able to call it
   (their `config.yaml` `allowed_tools:` list) — and remember the manager's own
   `tools.allowed_tools` is the outer gate.
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
