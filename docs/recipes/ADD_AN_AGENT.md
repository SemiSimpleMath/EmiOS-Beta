# Recipe: Add a new agent

You want a new LLM-driven decision unit. This walks through what you create, where it goes, and how it gets discovered.

Read [01_AGENTS.md](../architecture/01_AGENTS.md) first if you don't already know the agent contract.

## Decide where it lives

Agents live under `app/assistant/agents/`. Either:

- **Top-level**: `app/assistant/agents/my_agent/` — canonical name `my_agent`
- **Namespaced**: `app/assistant/agents/<namespace>/my_agent/` — canonical name `<namespace>::my_agent`. Use a namespace when several agents share a domain (e.g., `kg_mutation::planner`, `kg_mutation::final_answer`).

## The five files

```
app/assistant/agents/my_agent/
  config.yaml
  prompts/
    system.j2
    user.j2
  agent_form.py        # optional but strongly recommended
```

Sometimes you also want:
- `prompts/description.j2` — one-line agent description shown to a manager's planner when it picks among allowed_nodes
- `input_schema.py` — Pydantic input validation

### `config.yaml`

```yaml
name: my_agent                       # canonical name; matches directory path
class_name: Agent                    # one of Agent, Planner, MultiToolAgent

llm_params:
  llm_provider: openai               # openai | gemini | anthropic
  engine: gpt-5-mini                 # exact model id
  model_tier: mini                   # mini | smart | strong (informational)
  # NOTE: GPT-5 family ignores temperature; omit it for those models

allowed_tools: []                    # list of tool names, or "all"
allowed_nodes: []                    # which other agents this one can call

system_context_items:                # keys injected into system.j2
  - resource_user_data
user_context_items:                  # keys injected into user.j2
  - date_time
  - task

action_required: true                # true if the agent must emit action+action_input
```

For more options (events, append_fields, prompt_debug, dynamic_tools, …) see the existing rich configs in `app/assistant/agents/master_room/chat_gate/config.yaml` or `app/assistant/agents/emi_team/planner/config.yaml`.

### `prompts/system.j2`

Defines the role. The agent has *no* memory across invocations except what you put here and in `user.j2`. Keep this prompt about the *role* — what the agent is and the rules it follows. Put per-call data in `user.j2`.

```jinja2
You are the Activity Tracker for {{ resource_user_data.first_name }}.
Your job: count the user's recent activities by category.

Rules:
- Read only the recent_history field.
- Output a clean count per category — never summarize qualitatively.
```

Style rules from the user's feedback memories:
- **Affirmative voice.** Don't write "do not summarize" — write "count, don't summarize." Negation language plants the trait you're trying to suppress.
- **No "rules" examples.** State the rule sharply; don't include a "Bad example: …" block. Counter-examples don't generalize; rules do.
- **No real user data in the prompt.** That's `user.j2`'s job.

### `prompts/user.j2`

Per-call context. Whatever's listed in `user_context_items` resolves into this template.

```jinja2
Today is: {{ date_time }}

Task: {{ task }}

Recent history:
{% for line in recent_history %}
- {{ line }}
{% endfor %}
```

### `agent_form.py`

The structured-output schema. Pydantic class named `AgentForm` (or the first `BaseModel` subclass).

```python
from pydantic import BaseModel, Field

class AgentForm(BaseModel):
    counts: dict[str, int] = Field(
        description="Map of activity category -> count",
    )
    notes: str = Field(
        default="",
        description="Brief observation about the data, max one sentence",
    )
```

If you skip this file, the agent registry falls back to `config.yaml`'s `structured_output` block. Inline JSON schema is harder to maintain — prefer Pydantic.

If your agent needs to be the **final answer** for a manager, follow the established envelope:

```python
class AgentForm(BaseModel):
    # ---- domain-specific outputs ----
    your_field_1: str
    your_field_2: int

    # ---- standard envelope ----
    final_answer_answer: str = Field(description="Markdown summary for humans")
    result_summary: str = Field(default="", description="≤150 char one-liner")
    final_answer_sources: list[str] = Field(default_factory=list)
    final_answer_detail_level: str = "brief"
    final_answer_data_list: list[FinalAnswerDataItem] = Field(default_factory=list)
    final_answer_task: str | None = ""
    final_answer_what_was_done: str | None = ""
    final_answer_interesting_info: str | None = ""
```

`FinalAnswerDataItem` is your own Pydantic class with `Optional[str]` fields. Look at `app/assistant/agents/kg_mutation/final_answer/agent_form.py` for the canonical example. The OpenAI structured-output path requires `additionalProperties: false` on every nested object — that's why you can't use `List[dict]`.

## Wire it into a manager

Discovery is automatic — the registry picks up your directory. But discovery doesn't *use* the agent. You make it usable by:

1. **Listing it in `allowed_nodes`** of a manager that should be able to call it. Either a hard-coded list (`allowed_nodes: [my_agent, other_agent]`) or `"all"`.

2. **Routing to it from `state_map`** if you want deterministic flow:

   ```yaml
   state_map:
     prior_agent: my_agent
     my_agent: post_agent
   ```

3. **Or as the manager's `entry_agent`** if it's the start of the loop.

## Verify

```bash
.venv\Scripts\python.exe -c "
import app.assistant.tests.test_setup
from app.assistant.ServiceLocator.service_locator import DI
agent = DI.agent_factory.create_agent('my_agent')
print(agent.name, agent.config.llm_params)
"
```

If the agent registry has issues with your config, this is where you'll see it.

For an end-to-end test, point a small standalone script at your agent — invoke it with a synthetic Message and assert on the structured output. Don't write to the live KG / DB unless you scope-protect.

## Common pitfalls

- **Forgot to add to `allowed_nodes`.** The agent loads but no manager can call it. The error: planner won't see the action you want.
- **`temperature` set on GPT-5.** The validator warns at startup but the agent still calls. Omit `temperature` for GPT-5 family.
- **`agent_form.py` has `List[dict]` somewhere.** OpenAI rejects with "additionalProperties is required to be supplied and to be false in 'final_answer_data_list.items'". Replace with a proper Pydantic class.
- **Context item declared but not used in template.** The validator warns at startup ("user_context_items declares 'X' but it's not found in user.j2"). Either reference `{{ X }}` in the template or remove the declaration.

## See also

- [01_AGENTS.md](../architecture/01_AGENTS.md) — the full agent contract
- [Add a manager](ADD_A_MANAGER.md) — wiring agents into a manager flow
- [Add a tool](ADD_A_TOOL.md) — agents emit `action`; tools are what get called
