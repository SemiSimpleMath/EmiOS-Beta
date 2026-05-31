# Recipe: Add a new manager

You want a new orchestrator. Either fresh (rare) or — much more commonly — a new specialized manager **derived from emi_team**.

Read [02_MANAGERS.md](../architecture/02_MANAGERS.md) and [15_EMI_TEAM_AND_SCOPE.md](../architecture/15_EMI_TEAM_AND_SCOPE.md) before writing one.

## Pick the path

| Goal | What to do |
|------|------------|
| New room-driven flow | Create a `RoomManager`-class config; add a state_map of room agents. |
| New general-purpose worker for a domain | **Derive from emi_team.** Reuse its delegator + summary; write your own planner + final_answer. |
| New transactional / one-off pipeline | Don't write a manager. Write a **pipeline** ([Add a pipeline](ADD_A_PIPELINE.md)). |

This recipe walks the **emi_team-derived** path because that's what 90% of new managers should be.

## File layout

```
app/assistant/multi_agents/<my_manager_name>/
  __init__.py
  config.yaml
```

```
app/assistant/agents/<namespace>/
  planner/
    config.yaml
    prompts/system.j2
    prompts/user.j2
    agent_form.py
  final_answer/
    config.yaml
    prompts/system.j2
    prompts/user.j2
    agent_form.py
```

`<namespace>` is your domain — e.g., `kg_mutation`, `entertainment`, `devices`.

## `config.yaml` — the manager

Look at `app/assistant/multi_agents/kg_mutation_manager/config.yaml` for the canonical example. The shape:

```yaml
name: my_domain_manager
class_name: MultiAgentManager
description: One-line description of what this manager does.

# Reuse emi_team's delegator + summary; supply your own planner + final_answer.
agents:
  - emi_team::delegator
  - my_domain::planner
  - emi_team::critic
  - emi_team::summary
  - my_domain::final_answer

role_bindings:
  delegator: emi_team::delegator
  planner: my_domain::planner
  critic: emi_team::critic
  summary: emi_team::summary
  final_answer: my_domain::final_answer

entry_agent: emi_team::delegator

# Deterministic routing through the loop.
state_map:
  emi_team::delegator: my_domain::planner
  my_domain::planner: tool_caller            # control node
  tool_caller: tool_result_handler
  tool_result_handler: my_domain::planner    # back to planner unless planner said return_control
  my_domain::final_answer: manager_exit_node

max_cycles: 80

# Tool visibility — what your planner sees in its tool catalog.
tool_visibility:
  always_show: [my_typed_tool, ask_user]
  use_narrower: false           # set true to delegate filtering to shared::tool_narrower

# Tool blocklist (optional; the scope contract is the more durable guard).
blocked_tools:
  - kg_create_node             # we're a domain manager, not a writer
  - kg_create_edge

scope_contract:
  scope_id: scope::my_domain_manager
  approval:
    authority_level: 100
  resources:
    allowed_global_resources: ["all"]
  writes:
    write_kg: true              # narrows-only — caller must already grant this
    write_unified_log: true
```

The `scope_contract` is critical. Two rules to internalize:

1. **It can only narrow.** If the inbound Message says `write_kg: false` and your contract says `write_kg: true`, the framework rejects with "scope_contract attempted to expand writes.write_kg from false to true". Fix: have the *caller* seed the inbound Message's scope with the right permissions. See `app/assistant/kg_investigator/finding_executor.py::_mutation_scope` for the pattern.

2. **`requires_approval_tools` is also additive-narrowing only.** A manager can add approval gates to a tool but never lift them.

## Write your planner

The planner picks the next action. Most derived managers' planners are `Agent` (not `Planner`) because the domain-specific logic doesn't need the plan-validation overhead.

```yaml
# app/assistant/agents/my_domain/planner/config.yaml
name: my_domain::planner
class_name: Agent

llm_params:
  llm_provider: openai
  engine: gpt-5-mini
  model_tier: mini

allowed_tools:
  - my_typed_tool
  - ask_user
  - tool_caller    # special — lets planner call other tools via tool_caller dispatch

allowed_nodes: []

system_context_items:
  - resource_user_data
  - tool_descriptions

user_context_items:
  - task
  - information
  - recent_history

action_required: true
```

`prompts/system.j2`: the role + decision rules. For inspiration, read `app/assistant/agents/kg_mutation/planner/prompts/system.j2` — it has crisp decision rules like "if reversibility=='irreversible' OR confidence < 0.75 → escalate; else → execute + resolve."

`prompts/user.j2`: the per-call task and information. Very thin — usually just `{{ task }}` + `{{ information }}` + recent history.

`agent_form.py`: the planner's structured output. Typically `action`, `action_input`, `result_summary`, `must_revise_plan` — but the exact fields depend on your domain. Look at the kg_mutation planner form for a minimal example.

## Write your final_answer

The final_answer compiles the manager's terminal report. Critical: it carries the **standard envelope** so `manager_exit_node` can extract a result for the caller.

```python
# app/assistant/agents/my_domain/final_answer/agent_form.py
from typing import List, Optional
from pydantic import BaseModel, Field


class FinalAnswerDataItem(BaseModel):
    data_type: Optional[str] = None
    key: Optional[str] = None
    value: Optional[str] = None
    url: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    source: Optional[str] = None
    timestamp: Optional[str] = None


class AgentForm(BaseModel):
    # ---- domain-specific structured outcome ----
    outcome: str = Field(description="One of: applied, escalated, no_action, error")
    op_applied: Optional[str] = None
    revision_log_id: Optional[str] = None

    # ---- standard envelope (consumed by manager_exit_node) ----
    final_answer_answer: str = Field(description="Markdown summary for humans")
    result_summary: str = Field(default="", description="≤150 chars")
    final_answer_sources: List[str] = Field(default_factory=list)
    final_answer_detail_level: str = "brief"
    final_answer_data_list: List[FinalAnswerDataItem] = Field(default_factory=list)
    final_answer_task: Optional[str] = ""
    final_answer_what_was_done: Optional[str] = ""
    final_answer_interesting_info: Optional[str] = ""
```

Critically: **never use `List[dict]`** in a final_answer form. OpenAI rejects with "additionalProperties is required to be supplied and to be false". Always declare a Pydantic class.

`prompts/system.j2`: rules for compiling the final answer from the loop's accumulated state. Read `app/assistant/agents/kg_mutation/final_answer/prompts/system.j2` for the canonical example.

## Wire your manager into a caller

A manager doesn't fire on its own — something has to call it. Two patterns:

**Pattern A: another manager's `tool_caller` invokes you as an agent.**
```yaml
# in a parent manager's config.yaml
allowed_nodes: [my_domain_manager, ...]
```
Then the parent's planner emits `action: my_domain_manager` and `tool_caller` invokes you.

**Pattern B: a Python entry point invokes you directly.**
```python
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import (
    Message, ScopeContext, ScopeApprovalPolicy, ScopeResourcePolicy, ScopeWritePolicy,
)

mgr = DI.multi_agent_manager_factory.create_manager("my_domain_manager")
msg = Message(
    task="What you want done",
    information="Background context",
    scope_context=ScopeContext(
        scope_id="scope::my_caller",
        owner_id="primary_user",
        actor_id="my_caller",
        surface="system",
        approval=ScopeApprovalPolicy(authority_level=100),
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
        writes=ScopeWritePolicy(write_kg=True, write_unified_log=True),
    ),
)
DI.manager_invoker.invoke(mgr, msg)
```

The scope_context here grants the permissions the manager needs. The narrowing rule applies.

## Verify

```bash
.venv\Scripts\python.exe -c "
import app.assistant.tests.test_setup
from app.assistant.ServiceLocator.service_locator import DI
mgr = DI.multi_agent_manager_factory.create_manager('my_domain_manager')
print('manager loaded:', mgr.name)
print('agents:', [a for a in mgr.agents])
"
```

If a state_map references a missing agent or control node, the validator surfaces it on app startup.

## Common pitfalls

- **Forgot to seed write_kg in the caller's scope.** Manager rejects with "scope_contract attempted to expand writes.write_kg from false to true". Fix the caller, not the manager.
- **Reused `emi_team::final_answer` instead of writing your own.** Works but you lose the per-domain structured outcome. Write your own; carry the envelope.
- **state_map references control nodes by wrong name.** `tool_caller` is correct (singular), not `tool_caller_node`.
- **Manager runs forever.** Either `max_cycles` is too high, or your planner never returns `return_control`. Check the planner's loop-exit conditions.
- **Specialized planner uses `class_name: Planner`.** Usually wrong — `Planner` adds plan-validation that conflicts with most decision-rule planners. Use `class_name: Agent`.

## See also

- [02_MANAGERS.md](../architecture/02_MANAGERS.md) — the manager contract
- [15_EMI_TEAM_AND_SCOPE.md](../architecture/15_EMI_TEAM_AND_SCOPE.md) — derivation pattern + scope rules
- [04_CONTROL_NODES.md](../architecture/04_CONTROL_NODES.md) — what tool_caller does in your loop
- [Add an agent](ADD_AN_AGENT.md) — the planner + final_answer details
