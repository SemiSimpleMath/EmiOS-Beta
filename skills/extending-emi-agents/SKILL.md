---
name: extending-emi-agents
description: How to add a new agent to EmiOS. An agent is an LLM decision unit with config, prompts, and optional structured output. Use when a task involves creating, registering, or scaffolding a new agent.
license: Apache-2.0
metadata:
  author: emi-team
  version: "1.0"
  auto_inject_when:
    task_keywords:
      - "new agent"
      - "add agent"
      - "create agent"
      - "scaffold agent"
      - "register agent"
      - "extend emi agents"
---

# Adding a new agent

Agents live in `app/assistant/agents/<namespace>/<name>/`. The
`AgentRegistry` discovers them on import — drop the directory,
restart Flask, and the agent is callable.

## Files to create

```
app/assistant/agents/<namespace>/<name>/
├── config.yaml          # required
├── prompts/
│   ├── system.j2        # required
│   └── user.j2          # required
└── agent_form.py        # optional — Pydantic model for structured output
```

`<namespace>` is a subdirectory grouping (e.g. `master_room`,
`knowledge_graph_add`, `dayflow_orchestrator`); the agent's full name
becomes `<namespace>::<name>`.

## config.yaml fields

```yaml
name: my_namespace::my_agent          # MUST match dir path
class_name: Agent                     # or Delegator if a router
action_required: false                # true if must produce a tool call
llm_params:
  llm_provider: "openai"              # openai | gemini | anthropic
  engine: "gpt-5-mini"                # provider-specific model id
  model_tier: "mini"                  # nano | mini | powerful
  temperature: 0.3                    # omit for GPT-5 (advisory)

allowed_tools: []                     # tool names, or [] for none
except_tools: []
allowed_nodes: []                     # downstream agents this one can call
entity_scan_keys: [task, information] # context keys scanned for KG entities
entity_card_level: 0                  # 0 = headers only, 1 = full cards

user_context_items:                   # rendered into user.j2
  - date_time
  - task
  - information

system_context_items: []              # rendered into system.j2
```

The validator at startup will fail loud if a declared context item
isn't actually referenced in the corresponding prompt — a silent typo
becomes a startup error.

## Prompts

Jinja2 templates. Each declared `*_context_items` entry must appear
in the matching template. Resources prefixed `resource_*` resolve
from the ResourceManager; bare keys come from the per-call context.

```jinja2
{# system.j2 #}
You are {{ resource_assistant_data.name }}'s helper for X. ...

{# user.j2 #}
Today: {{ date_time }}
Task: {{ task }}
Info: {{ information }}
```

Don't hardcode the user's or assistant's name — template via
`{{ resource_user_data.first_name }}` and `{{ resource_assistant_data.name }}`.

## Structured output (optional)

If `action_required: true` or you want a typed result, add
`agent_form.py` with a Pydantic v2 model:

```python
from pydantic import BaseModel, Field

class MyAgentForm(BaseModel):
    decision: str = Field(..., description="...")
    reason: str = Field(default="")
```

The `Agent` runtime auto-discovers `*_form.py` next to `config.yaml`.

## After dropping the files

1. Restart Flask (`emi.bat` / `emi.command`).
2. Watch startup logs — `Running agent registry validation...` block
   is where typos surface.
3. Invoke via `DI.agent_factory.create_agent("<namespace>::<name>")`
   from a manager flow, or wire it into a manager's `state_map`.

## Canonical examples

- Simple Agent class: `app/assistant/agents/master_room/chat_gate/`
- Agent with structured output: `app/assistant/agents/situation_auditor/`
- Delegator (routing-only): `app/assistant/agents/master_room/switchboard/`

## Notes

- Don't add `manual_toggle` or `feature_guard` unless the agent needs
  per-routine gating — those fields are for Routine entries, not
  agent configs.
- If the agent does NOT use any context items, write `[]` explicitly
  — the validator rejects null/missing.
- Tests live in `app/assistant/tests/agent_tests/`. The simplest
  test is a contract test that imports the agent and dispatches a
  sample input.
