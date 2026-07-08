---
name: extending-emi-managers
description: How to add a new multi-agent manager to EmiOS. A manager wires agents and control nodes into a state-machine that runs deterministically. Use when the task involves creating a new bounded cognitive workspace (manager) for a specific domain.
license: Apache-2.0
metadata:
  author: emi-team
  version: "1.0"
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

Managers live in `app/assistant/multi_agents/<name>/`. The
`ManagerInvoker` discovers them on import — drop the directory,
restart Flask, invoke via `DI.manager_invoker.invoke(<name>, message)`.

## Files to create

```
app/assistant/multi_agents/<name>/
├── __init__.py            # required (can be empty)
└── manager_config.yaml    # required — agents + state map + permissions
```

## manager_config.yaml shape

```yaml
name: my_manager
class_name: MultiAgentManager

allowed_tools:                     # tools any agent in this manager can call
  - send_email
  - get_calendar_events
blocked_tools: []                  # explicit denials (overrides allowed)
hidden_tools: []                   # callable but invisible to planner

agents:                            # list of agents this manager uses
  - name: my_namespace::planner
  - name: my_namespace::critic
  - name: shared::summary_writer

control_nodes:                     # deterministic non-LLM nodes
  - name: tool_caller
    type: tool_caller
  - name: critic_post_node
    type: critic_post_node
    config:
      critic_agent: "my_namespace::critic"

flow_config:
  state_map:                       # who-runs-after-whom
    init:                       my_namespace::planner
    my_namespace::planner:      tool_caller
    tool_caller:                critic_post_node
    critic_post_node:           my_namespace::planner
    # ...

  summary:                         # which agent writes the wrap-up
    agent: shared::summary_writer

role_bindings:                     # named roles other agents reference
  delegator: my_namespace::planner

scope_contract:
  type: narrow_only                # permissions can narrow, never expand
  permissions:
    can_mutate_kg: false
    can_send_email: true
```

## Agents declared but not in flow_config

The validator warns if an agent is in `agents:` but not reachable
via `state_map`, `role_bindings`, control-node configs, or another
agent's `allowed_nodes`. Either reach it or remove it.

## Read vs write managers

By convention, managers that mutate persistent state (KG, settings,
user data) are SEPARATE from read-only managers. The general
assistant uses `emi_team_manager`; KG mutations route through
`kg_mutation_manager`. Don't add `kg_create_node` to
`emi_team_manager.allowed_tools` — that's the safety pattern.

## After dropping the files

1. Restart Flask.
2. Watch validation output — `_check_manager_configs` will reject
   unknown agent names or unreachable agent declarations.
3. Wire the manager from a router (e.g. `master_room::switchboard`)
   or call directly via `DI.manager_invoker.invoke("my_manager", msg)`.

## Canonical examples

- Generalist with many sub-managers: `app/assistant/multi_agents/emi_team_manager/`
- Domain-specific (admin tasks): `app/assistant/multi_agents/personal_admin_manager/`
- Read-only KG access: the `ask_kg` leaf tool (LLM-RAG over the knowledge graph)
- Approval-gated browser automation: `app/assistant/multi_agents/playwright_manager/`

## Notes

- Room-bound managers are still `MultiAgentManager`; room modes
  (`task_creation_mode` / `doc_creation_mode`) are selected at ingress,
  which seeds `next_agent` with the mode's source agent.
- Control nodes available out of the box: `tool_caller`,
  `critic_post_node`, `final_answer_node`, `summary_post_node`,
  `approval_node`, `return_control_node`. Register custom nodes by
  adding them to `app/assistant/control_nodes/`.
- Manager-level `allowed_tools` is the OUTER gate. Per-agent
  `allowed_tools` (in agent config) further narrows. A tool must
  pass BOTH to be callable.
- Tests live in `app/assistant/tests/manager_tests/<manager>/`.
  Each manager typically has a `<manager>_test.py` smoke script.
