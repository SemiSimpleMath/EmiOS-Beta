---
name: extending-emi-managers
description: How to add a new multi-agent manager to EmiOS. A manager wires agents and control nodes into a state-machine that runs deterministically. Use when the task involves creating a new bounded cognitive workspace (manager) for a specific domain.
license: Apache-2.0
metadata:
  author: emi-team
  version: "2.0"
  auto_inject_when:
    task_keywords:
      - "new manager"
      - "add manager"
      - "create manager"
      - "multi-agent manager"
      - "manager config"
      - "extend emi managers"
---

# Adding a new manager

A manager is a **directory containing `config.yaml`** under `app/assistant/multi_agents/`. There is
no Python to write: every manager in the repo is the same `MultiAgentManager` class, differing only
in its config. Drop the directory, restart Flask.

`ManagerRegistry.preload_all()` scans `app/assistant/multi_agents/*/` at boot and registers a
directory **iff it contains `config.yaml`**. Nothing else is required — the live
`dayflow_wake_manager/` directory contains that one file and nothing else.

> **The registry key is the DIRECTORY name, not the `name:` field inside the file.** You invoke
> `create_manager("<directory name>")`. Keep them identical to avoid confusion.

## Files to create

```
app/assistant/multi_agents/<name>/
└── config.yaml        # the whole manager
```

No `__init__.py` is needed. A directory without `config.yaml` is silently skipped — that is the
usual reason a new manager "isn't found".

## config.yaml — the verified shape

```yaml
name: my_manager                  # keep equal to the directory name
class_name: MultiAgentManager     # required; class in app/assistant/manager_classes/
description: "One line — what this manager is for."
max_cycles: 20                    # loop-selected non-ControlNode activations (default 30)
max_exit_cycles: 10               # budget for the graceful-exit loop (default 10)

role_bindings:
  delegator: room::delegator      # REQUIRED — the loop resolves the 'delegator' role every run

agents:                           # REQUIRED key (may be a short list, but must exist)
  - name: room::delegator
    class: Delegator
  - name: my_namespace::planner
    class: Agent

control_nodes:                    # REQUIRED key, and at least one named entry
  - name: my_prep_node
    class: MyPrepNode             # CamelCase class; see "the class key" below
  - name: final_answer_node
    class: FinalAnswerNode
  - name: manager_exit_node
    class: ManagerExitNode
  - name: graceful_exit_control_node
    class: GracefulExitControlNode

tools:                            # the manager's OUTER tool gate
  allowed_tools:
    - send_email
    - ask_kg
  except_tools: []                # subtracted from allowed_tools

scope_contract:                   # receiving-manager policy; see Scope below
  tools:
    allowed_tools: ["all"]
    blocked_tools:
      - install_tool
    requires_approval_tools: []

flow_config:
  strict_routing: true
  flow:                           # ROOM-SERVING managers only — see below
    normal:
      source_agent: "my_prep_node"
  state_map:                      # REQUIRED, non-empty
    "room::delegator": "my_prep_node"
    "my_prep_node": "my_namespace::planner"
    "my_namespace::planner": "final_answer_node"
    "my_namespace::planner_return_control": "final_answer_node"
    "final_answer_node": "manager_exit_node"
    "graceful_exit": "graceful_exit_control_node"
    "graceful_exit_control_node": "final_answer_node"
```

This is a straight-through shape: the example agent produces final-answer fields
or a `return_control` result. Tool selection needs an arguments stage plus
`ToolCaller`, `ToolResultHandler`, and return routing; copy a live worker flow
for that case. The placeholders must be implemented before construction.

`tools.allowed_tools` may contain the literal `"all"`, which expands to the whole tool registry.
An empty/missing `allowed_tools` logs "has no allowed tools configured" and the manager runs with none.

## `flow_config.flow` — only if a ROOM routes to this manager

`flow.<room_mode>.source_agent` names the agent that starts that mode's flow. It is read at
**ingress** by `room_ingress_service._resolve_mode_source_agent`, which maps the inbound
`room_mode` (`normal`, `planning_mode`, `task_creation_mode`, …) to that agent and seeds
`next_agent` with it. It **raises** if the mode is not configured under `flow_config.flow`, or if
its `source_agent` is missing or empty; a room may name a fallback via `policy.default_room_mode`.

A manager invoked directly — `create_manager(...)` + `invoke(...)`, like the dayflow trio — never
goes through ingress and does not need a `flow` block. Omit it unless a room reaches your manager.

## The `class:` key, and manager-local aliases

Each entry under `agents:` and `control_nodes:` carries `name:` and `class:`.
For standard agents, AgentLoader uses the named registry entry's class and config;
changing only the manager entry's `class:` does not override that agent class.

`AgentLoader._resolve_entry_from_declared_class` converts `class:` from CamelCase to snake_case and
looks **that** up in the registry. Control nodes are registered under their **filename stem**, so
`class: ReturnControlNode` resolves to `return_control_node.py`. This is how a manager gives a node a
local alias:

```yaml
  - name: return_control          # the name your state_map uses
    class: ReturnControlNode      # resolves to control_nodes/return_control_node.py
```

Use `class:`, never `type:`.

## state_map — how routing actually works

`Delegator.pick_next_agent` does exactly one thing: `state_map[last_agent]`. Each node/agent sets
`last_agent` to its own name when it finishes, so the map is "who runs after whom".

- **The first hop is keyed by the delegator's bound name** (`room::delegator` above), because
  `run_agent_loop` sets `last_agent` to the delegator before the first cycle. There is no `init:` key.
  (If `last_agent` is ever empty, the key looked up is `NO_PREVIOUS_AGENT`.)
- A node may **override** routing by setting `next_agent` itself; the delegator honours it and returns
  early. That is how conditional branches are expressed — never by logic in the delegator, which holds
  no policy by design.
- Keys are an open vocabulary: besides agent/node names, the loop emits synthetic states
  `graceful_exit`, `max_limit`, `error_exit`, and `<agent>_return_control`.
- **Values are closed** — see validation below.

## Validation — two validators, at two different moments

**1. At boot** — `agent_validator.validate_all()` (called from `initialize_system`) runs
`_check_manager_configs`, which reads **every `*.yaml`** under `app/assistant/multi_agents/`
(skipping `.archive`):

- **raises `RuntimeError`** when `agents:` names an agent that is not in the registry
  ("references unknown agents")
- **warns** when a declared agent is never reachable. Reachability counts: `state_map` keys and
  values, values inside any `control_nodes[].config` block, values inside any `flow_config`
  sub-section, other agents' `allowed_nodes`, and `role_bindings` values. If your agent is declared
  but reached none of those ways, either wire it or remove it.

**2. At construction** — `MultiAgentManager._validate_strict_routing_config()` runs every time the
manager is built. It raises `ValueError` (it does not warn) when:

- `flow_config.state_map` is missing, not a dict, or empty
- any src/dst is not a non-empty string
- `control_nodes` is not a list, or names no node
- **any state_map VALUE does not name a configured agent, control node, or role binding** — the most
  common mistake, caught when the manager is constructed
- a supplied `*_return_control` key's prefix is outside that same name/role set
- a dict `flow_config.tool_return` has no non-empty handler name, or that name is absent from `state_map`
- a dict `critic:` section omits any of `subject_agent` / `critic_agent` / `continue_agent`
- a dict `summary:` section omits any of `source_agent` / `summary_agent` / `resume_agent`

These checks are incomplete: role-binding targets are admitted to the name set
without proving they are loaded; missing return-control edges are not inferred
from output schemas; non-dict optional sections skip the subsection checks.
Boot's unused-agent check is a reference scan, not graph reachability.
Start verification by building the manager, then inspect loaded instances and
exercise its normal, tool-return, and exit routes:

```python
DI.manager_registry.preload_all()
DI.multi_agent_manager_factory.create_manager("my_manager")   # raises if the config is wrong
```

## Invoking it

```python
manager = DI.multi_agent_manager_factory.create_manager("my_manager")
DI.manager_invoker.invoke(manager, message)
```

`invoke` takes the **manager instance**, not its name. One fresh instance per invocation, each with
its own `Blackboard`. `request_handler` does not reset an existing instance;
shared DI services and persistent stores remain shared between managers.

## What your nodes can see

`MultiAgentManager.request_handler` copies the inbound message's `task`, `information`, and **every
key of `message.data`** onto the blackboard once, at the start. Control nodes are then activated with
a bare `Message(data_type='agent_activation', …)` that carries **no data**.

> **Read trigger data from the blackboard, never from `message.data`.** A node that reads
> `message.data` gets nothing, silently, and falls through to whatever its state_map default is. A
> dayflow time-wake ran a full planning tick for two days on exactly this mistake (2026-09-18).

Per-node config is available too: give an entry a `config:` block and read it with the node's
`_node_cfg()`; flow sections are read with `_flow_section_cfg("<section>")`.

## Budgets

`max_cycles` counts completed loop-selected non-`ControlNode` activations.
The delegator and agent/tool calls made inside nodes do not increment it.
A separate backstop (`max_cycles * 8`, minimum 40 iterations) catches node
routing loops. On exhaustion the manager attempts its graceful-exit loop
with `max_exit_cycles`. This does not cap total LLM calls or elapsed time.

## Scope

Use an explicit scope with `manager_invoker`. ScopeAdapter can also derive a
room scope or accept `data.scope_contract`; strict mode permits system
derivation for task-file/resource-contract request data and otherwise rejects
missing scope. Its errors propagate. A direct scope-less `request_handler`
call instead catches its internal scope error and enters error-exit handling.
Test mode permits scope substitution.

A parent allow-list of `["all"]`, or one naming the child manager, grants the
child's declared tool surface (`scope_contract.tools.allowed_tools`, otherwise
config `tools.allowed_tools`). Other parent lists intersect with that surface.
Denials accumulate; per-manager rules restrict further; authority and write
expansion is rejected. See [Managers](../../docs/architecture/02_MANAGERS.md)
and [Runtime Data Contract](../../docs/architecture/02b_RUNTIME_DATA_CONTRACT.md)
for result types, reserved-key filtering, and current implementation limits.

## Read vs write managers

By convention, managers that mutate persistent state (KG, settings, user data) are SEPARATE from
read-only managers. The general assistant uses `emi_team_manager`; KG mutations route through
`kg_mutation_manager`. Don't add `kg_create_node` to `emi_team_manager` — that separation is the
safety pattern.

## Canonical examples

- Straight line, one node's worth of work: `app/assistant/multi_agents/dayflow_dispatch_manager/`
- Minimal directory (config.yaml only): `app/assistant/multi_agents/dayflow_wake_manager/`
- Long deterministic pipeline: `app/assistant/multi_agents/dayflow_orchestrator_manager/`
- Generalist with sub-managers: `app/assistant/multi_agents/emi_team_manager/`

## Notes

- Tests live in `app/assistant/tests/manager_tests/<manager>/`.
- Adding a brand-new control node means adding a file to `app/assistant/control_nodes/`; it is
  discovered by filename, and its class must be the file stem in CamelCase. See
  `extending-emi-agents` for the agent side of the contract.
