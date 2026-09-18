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

Agents live in `app/assistant/agents/<namespace>/<name>/`. `AgentRegistry`
discovers them at boot (`load_agents()`, from `bootstrap`) by walking
`agents/` for any directory containing `config.yaml` — drop the directory,
restart Flask, and the agent is callable. A `.ignore` file in the directory
skips it.

## Files to create

```
app/assistant/agents/<namespace>/<name>/
├── config.yaml          # required
├── prompts/
│   ├── system.j2        # required — missing raises at boot
│   ├── user.j2          # required — missing raises at boot
│   └── description.j2   # optional — only for agents that other agents call by name
├── agent_form.py        # optional — structured output (exact filename)
└── input_schema.py      # optional — Pydantic model for the agent's INPUT
```

Both prompts are genuinely mandatory: `AgentRegistry._load_prompts` raises
`FileNotFoundError` if either is absent, so the agent cannot load at all.

`<namespace>` is a subdirectory grouping (e.g. `master_room`,
`knowledge_graph_add`, `dayflow_orchestrator`); the agent's full name
becomes `<namespace>::<name>`.

## config.yaml fields

```yaml
name: my_agent                        # see "naming" below — bare name is the norm
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

## Naming — the directory usually decides, but `::` overrides it

`AgentRegistry._load_all_agent_configs` builds the canonical name:

- a **bare** `name:` (no `::`) gets the namespace from the directory path
  relative to `agents/` — `agents/dayflow_orchestrator/state_mover/` with
  `name: state_mover` becomes `dayflow_orchestrator::state_mover`. Multi-level
  directories join with `::`.
- a `name:` that **already contains `::` is used verbatim**, and the directory
  path is ignored. This is deliberate — it lets a namespaced agent live in a
  flat directory (`wiki::fact_answer_judge` in `agents/wiki_fact_answer_judge/`).
  It also means a typo'd `::` name silently registers under the wrong namespace.

Duplicate canonical names are fatal: `_check_namespace_consistency` raises
`RuntimeError("Duplicate agent name")` at boot.

## What the validator does and does not catch

`agent_validator.validate_all()` runs at boot from `initialize_system`. Know
which findings stop the process and which only print:

**Raises (boot fails):**
- a declared `*_context_items` key set to explicit `null` — write `[]` instead
- a `*_context_items` value that is not a list
- duplicate agent canonical names
- LLM-params contract breaches: a top-level `model` / `engine` / `temperature` /
  `timeout` / `llm_provider` (they belong under `llm_params`), `llm_params.model`
  (use `engine`), `llm_params` set to null or a non-dict, or `llm_provider` and
  `engine` not supplied together
- a manager config referencing an unknown agent

**Warns only (easy to miss, and routinely missed):**
- a declared context item that does not appear in the matching template
- a template referencing `{{ X }}` that nothing declares — it renders empty
- a `gpt-5*` engine that also sets `temperature`

A **missing** `*_context_items` key is fine — it is treated as an empty list.
Only an explicit `null` raises.

Framework builtins never need declaring (the injector always populates them):
`date_time`, `day_of_week`, `action_count`, `room_contact_name`,
`current_speaker_name`, `skills`, `auto_injected_skill_names`, `entity_info`.

## Prompts

Jinja2 templates. Each declared `*_context_items` entry must appear
in the matching template. Resources prefixed `resource_*` resolve
from the ResourceManager; bare keys come from the per-call context.

```jinja2
{# system.j2 #}
You are {{ resource_assistant_data.name }}'s helper for X. ...

{# user.j2 #}
Task: {{ task }}
Info: {{ information }}
Today: {{ date_time }}
```

Don't hardcode the user's or assistant's name — template via
`{{ resource_user_data.first_name }}` and `{{ resource_assistant_data.name }}`.

Order the user template stable-first, volatile-last: instructions and
section structure at the top, then the task/context blocks, with the
current time (`{{ date_time }}`) at the end next to the question it
informs. Provider prompt caches reward identical prompt heads across
calls made minutes apart, and the timestamp is the one value that
changes every minute — placing it last lets everything above it ride
the cache on agents that get called in bursts.

## Structured output (optional)

If `action_required: true` or you want a typed result, add a file named exactly
`agent_form.py` next to `config.yaml`, and **name the model `AgentForm`**:

```python
from pydantic import BaseModel, Field

class AgentForm(BaseModel):
    decision: str = Field(..., description="...")
    reason: str = Field(default="")
```

`AgentRegistry._load_agent_form` loads that one filename — not `*_form.py` — and
prefers a class named exactly `AgentForm`. Any other name still works but takes
the fallback path ("first Pydantic model found") and logs a warning, so a file
with several models can bind the wrong one. Helper/nested models alongside
`AgentForm` are fine.

`agent_form.py` takes precedence over a `structured_output` block in
`config.yaml`; declaring both logs a notice and the Python wins.

## After dropping the files

1. Restart Flask (`emi.bat` / `emi.command`).
2. Watch startup logs — `Running agent registry validation...` block
   is where typos surface.
3. Invoke via `DI.agent_factory.create_agent("<namespace>::<name>")`
   from a manager flow, or wire it into a manager's `state_map`.

## Canonical examples

- Simple Agent class: `app/assistant/agents/master_room/chat_gate/`
- Agent with structured output: `app/assistant/agents/situation_auditor/`
- Delegator (routing-only): `app/assistant/agents/emi_team/delegator/`

## Notes

- Don't add `manual_toggle` or `feature_guard` unless the agent needs
  per-routine gating — those fields are for Routine entries, not
  agent configs.
- If the agent does NOT use any context items, write `[]` explicitly. An
  explicit `null` raises at boot; a missing key is silently treated as `[]`.
- Tests live in `app/assistant/tests/agent_tests/`. The simplest
  test is a contract test that imports the agent and dispatches a
  sample input.
