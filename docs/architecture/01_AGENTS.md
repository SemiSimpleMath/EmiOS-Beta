# Agents

An **Agent** is a self-contained LLM-powered decision unit. Each agent processes inputs via an LLM and produces structured outputs that drive the system's behavior.

## Agent Contract

Base class: `app/assistant/agent_classes/Agent.py`

Key interface:
- `action_handler(message: Message) -> ToolResult` — main entry point
- `construct_prompt(message)` — builds system + user prompts via Jinja2
- `call_llm(messages, response_format)` — structured LLM call
- `process_llm_result(llm_output)` — validate, apply to blackboard, handle flow

## Directory Structure

Each agent lives under `app/assistant/agents/`:

```
agents/
  agent_name/
    config.yaml           # Agent metadata and configuration
    prompts/
      system.j2           # System prompt (Jinja2 template)
      user.j2             # User prompt (Jinja2 template)
      description.j2      # Optional: agent description
    agent_form.py         # Optional: Pydantic model for structured output
    input_schema.py       # Optional: Pydantic model for input validation
```

Nested agents use `::` naming convention:
```
agents/
  dayflow_orchestrator/
    action_selector/      # Canonical name: dayflow_orchestrator::action_selector
      config.yaml
      prompts/
      agent_form.py
```

## config.yaml

```yaml
name: agent_name                    # Canonical name (may include ::)
class_name: Agent                   # Base class: Agent | Planner | MultiToolAgent
color: "green"                      # UI color hint

llm_params:
  llm_provider: openai              # Provider: openai | gemini | anthropic
  engine: gpt-5-mini                # Model name
  model_tier: mini                  # Tier: mini | smart | strong
  temperature: 0.1                  # Sampling temperature

allowed_tools: [tool1, tool2]       # List or "all"
except_tools: [tool3]               # Exclude from allowed_tools
allowed_nodes: [agent1, agent2]     # Child agents this agent can route to (list or "all")
except_nodes: [agent3]              # Exclude from allowed_nodes

system_context_items:               # Keys injected into system.j2 template
  - resource_user_data
  - tool_descriptions
user_context_items:                 # Keys injected into user.j2 template
  - date_time
  - task
  - recent_history

structured_output:                  # JSON schema (overridden by agent_form.py if present)
  type: object
  properties: {}

action_required: true               # Whether action/action_input must appear in output

global_output_keys: [field1]        # Output fields written to global scope (vs local)
append_fields: [field1]             # Output fields that append instead of replace

events: [event_name]                # Event handlers to register

prompt_debug:                       # Debug logging flags
  system: true
  user: true
  results: true
```

## agent_form.py

Defines the structured output schema using Pydantic. Takes precedence over `config.yaml`'s `structured_output`.

```python
from pydantic import BaseModel, Field
from typing import List, Optional

class AgentForm(BaseModel):
    chat_response: str = Field(..., description="Direct reply to user")
    handoff_tf: bool = Field(default=False, description="Route to switchboard")
    switchboard_task: str = Field(default="", description="Task for switchboard")
```

The registry dynamically imports this file and uses the `AgentForm` class (or the first `BaseModel` subclass) as `response_format` in the LLM call.

## Prompt Templates

Templates are Jinja2. Variables come from context injection:

**system.j2** — defines the agent's role and rules:
```jinja2
You are the Activity Tracker for {{ resource_user_data.first_name }}.
Your job: update activity counts based on recent chat excerpts.
```

**user.j2** — provides the current request context:
```jinja2
Today is: {{ date_time }}
Current activity counts:
{% for activity, count in current_activity_counts.items() %}
- {{ activity }}: {{ count }}
{% endfor %}
```

### Context Injection

Context items listed in `system_context_items` and `user_context_items` are resolved by `ContextInjector`:
- Resource keys (e.g., `resource_user_data`) are loaded from the resource manager
- Special keys like `tool_descriptions`, `date_time` are computed dynamically
- Entity injection (`EntityInjector`) handles KG entity data in user prompts

## Tool Access Control

Agents don't directly call tools. They output `action` + `action_input` which the flow controller and control nodes (ToolCaller) dispatch.

Tool allowance resolution:
1. Start with `allowed_tools` from config (list or "all")
2. Subtract `except_tools`
3. Overlay `task_allowed_tools` (dynamic, from blackboard)
4. Overlay `task_except_tools` (dynamic)
5. Add `dynamic_allowed_tools` (MCP tools)
6. Subtract `dynamic_denied_tools`

## Agent Variants

| Class | Purpose |
|-------|---------|
| `Agent` | Standard single-turn LLM decision |
| `Planner` | Multi-step planning with plan validation |
| `MultiToolAgent` | DAG-based multi-tool execution |

## How to Add a New Agent

1. Create directory: `app/assistant/agents/<name>/` (or nested under a namespace)
2. Write `config.yaml` with name, class_name, llm_params, allowed tools/nodes, context items
3. Write `prompts/system.j2` (role definition) and `prompts/user.j2` (request context)
4. Optionally write `agent_form.py` with Pydantic `AgentForm` class
5. Reference the agent in a manager's `state_map` or `allowed_nodes` to wire it into a flow
6. The `AgentRegistry` auto-discovers it on startup — no registration code needed

## Key Files

| File | Purpose |
|------|---------|
| `agent_classes/Agent.py` | Base class — execution logic |
| `agent_classes/Planner.py` | Multi-step planning variant |
| `agent_classes/MultiToolAgent.py` | DAG execution variant |
| `agent_registry/agent_registry.py` | Auto-discovery, config/prompt/form loading |
| `agent_registry/agent_factory.py` | Instantiation with components |
| `agent_runtime/services/prompt_builder.py` | Jinja2 template rendering |
| `agent_runtime/services/context_injector.py` | Context variable resolution |
| `agent_runtime/services/llm_client.py` | LLM interface |
| `agent_runtime/services/flow_controller.py` | Action routing |
