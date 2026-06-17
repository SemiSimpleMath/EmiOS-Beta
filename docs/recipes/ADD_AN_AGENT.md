# Recipe: Add a new agent

You want a new LLM-driven decision unit. This walks through what you create, where it goes, and how it gets discovered.

Read [01_AGENTS.md](../architecture/01_AGENTS.md) first if you don't already know the agent contract.

## Decide where it lives

Agents live under `app/assistant/agents/`. Either:

- **Top-level**: `app/assistant/agents/my_agent/` — canonical name `my_agent`
- **Namespaced**: `app/assistant/agents/<namespace>/my_agent/` — canonical name `<namespace>::my_agent`. Use a namespace when several agents share a domain (e.g., `kg_mutation::planner`, `kg_mutation::final_answer`).

## The files

```
app/assistant/agents/my_agent/
  config.yaml          # required
  prompts/
    system.j2          # required — registry raises FileNotFoundError if missing
    user.j2            # required — same
  agent_form.py        # optional but strongly recommended (structured output)
```

Sometimes you also want:
- `prompts/description.j2` — agent description rendered into another agent's `allowed_nodes` listing (the `{name, description}` pairs a delegator/planner sees)
- `input_schema.py` — Pydantic input validation (first `BaseModel` subclass is taken)
- `.ignore` — an empty marker file; its presence skips this agent at load

### `config.yaml`

Only `name`, `class_name`, and the two prompts are mandatory; everything else is optional. The fields the loader (`agent_registry.py`) and runtime services actually read:

```yaml
name: my_agent                       # canonical name; if it contains :: it's taken verbatim,
                                     # otherwise the namespace is derived from the directory path
class_name: Agent                    # class file in agent_classes/ (see "Pick a class_name" below)
action_required: true                # if true, output MUST carry a valid action + action_input

llm_params:
  llm_provider: openai               # openai | gemini | anthropic
  engine: gpt-5.1                    # exact model id (NOT a top-level `model:` key)
  model_tier: smart                  # tier hint (nano | mini | smart | strong) — informational
  # temperature: 0.1                 # OPTIONAL and lives ONLY here, never top-level.
  #                                  # GPT-5 family ignores temperature — omit it for those models.

# --- Tool / node policy (a permission CEILING, not a grant) ---
allowed_tools: []                    # list of tool names, or "all"
except_tools: []                     # subtract from allowed_tools
allowed_nodes: []                    # which other agents this one may call (list or "all")
except_nodes: []

# --- Prompt / context ---
system_context_items:                # keys injected into system.j2
  - resource_user_data
user_context_items:                  # keys injected into user.j2
  - date_time
  - task
required_context_items: []           # hard guard: refuse to run if any of these resolves empty
strict_template: false               # true => StrictUndefined (an undefined var raises)

# --- Skills (optional) ---
skills: []                           # static skills always loaded (each gated by requires_scope)
accept_auto_skills: true             # allow the SkillInjector to auto-inject skills by trigger
```

There is **no** top-level `model:` or `temperature:` — model selection lives entirely under `llm_params` (`llm_provider` + `engine` + `model_tier`). There is also no `allowed_resources` field; resource access is governed by scope policy plus the `resource_*` context keys.

For more options (entity-card injection, keyword-resource injection, `append_fields`, `global_output_keys`, `events`, `prompt_debug`, …) see [01_AGENTS.md](../architecture/01_AGENTS.md#configyaml) and the rich configs in `app/assistant/agents/master_room/chat_gate/config.yaml` (a `gemini` chat agent, `action_required: false`) and `app/assistant/agents/emi_team/planner/config.yaml` (an `openai` `Planner` with `skills:` + `accept_auto_skills: false`).

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

The structured-output schema. `agent_registry._load_agent_form` imports the file under a unique per-path module name (so Pydantic forward-refs resolve), then selects the class named **`AgentForm`** if present, otherwise the **first `BaseModel` subclass** found.

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

If both exist, `agent_form.py` **takes precedence** and a warning is logged. If you skip the file entirely, the registry falls back to `config.yaml`'s `structured_output` JSON-schema block — harder to maintain, so prefer Pydantic.

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

## Pick a `class_name`

`class_name` names a file in `app/assistant/agent_classes/`; the registry loads the class of the same name. Most new agents are `Agent`. The available classes (see the table in [01_AGENTS.md](../architecture/01_AGENTS.md#agent-variants)):

`Agent` (standard single-turn decision), `Planner` (multi-step planning with plan validation), `MultiToolAgent` (DAG multi-tool execution), `Delegator` (a manager's routing/entry agent), `OneShotAgent`, `ToolArguments` / `ToolArgumentsPlaywright`, the `Playwright*` browser roles, `SummaryAgent`, and the event-driven `EmiResultHandler` / `EmiReminderHandler` (registered via `events:`).

## Wire it into a manager

Discovery is automatic — `AgentRegistry.load_agents()` rglobs `agents/` on startup and picks up any directory with a `config.yaml`. But discovery doesn't *use* the agent. You make it usable by:

1. **Listing it in a manager's `agents` block** so the manager instantiates it:

   ```yaml
   agents:
     - name: my_namespace::my_agent
       class: Agent
   ```

2. **Routing to it from `flow_config.state_map`** for deterministic flow (note `state_map` lives *under* `flow_config`, not at top level):

   ```yaml
   flow_config:
     state_map:
       prior_agent: my_namespace::my_agent
       my_namespace::my_agent: post_agent
   ```

3. **Or making it the entry agent** — the entry is the manager's `role_bindings.delegator` (there is no `entry_agent` field). See [Add a manager](ADD_A_MANAGER.md).

If another agent (a planner/delegator) should be able to *delegate* to it, also add it to that agent's `allowed_nodes`.

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

- **Forgot to add to a manager's `agents` block (or to a delegating agent's `allowed_nodes`).** The agent loads (auto-discovery always finds it) but no manager instantiates or routes to it, and no planner can name it as an action.
- **`temperature` set on GPT-5.** The validator warns at startup but the agent still calls. Omit `temperature` for GPT-5 family.
- **`agent_form.py` has `List[dict]` somewhere.** OpenAI rejects with "additionalProperties is required to be supplied and to be false in 'final_answer_data_list.items'". Replace with a proper Pydantic class.
- **Context item declared but not used in template.** The validator warns at startup ("user_context_items declares 'X' but it's not found in user.j2"). Either reference `{{ X }}` in the template or remove the declaration.

## See also

- [01_AGENTS.md](../architecture/01_AGENTS.md) — the full agent contract
- [Add a manager](ADD_A_MANAGER.md) — wiring agents into a manager flow
- [Add a tool](ADD_A_TOOL.md) — agents emit `action`; tools are what get called
