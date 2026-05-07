# context_enricher_manager — archived 2026-05-06

**DO NOT DELETE.** Snapshot of the orphan `context_enricher_manager` wrapper.

## Why archived

The manager was scaffolding-only: a thin wrapper (1 config.yaml, 1051 bytes) that defined a 4-state linear flow (`shared::delegator → context_enricher::planner → shared::final_answer → manager_exit_node`) with no tools and no branching. It was the "manager-as-tool" entry point that would let any planner invoke context enrichment via `manager_interface.execute("context_enricher_manager")`.

**Nobody invokes it.** No `create_manager("context_enricher_manager")`, no `delegator_agents` reference, no `policy.json` binding, no `routine` runner. The only caller was a (gitignored) standalone test runner.

## What's actually live

The work the manager was supposed to wrap is being done **directly**, bypassing the manager:

- **`context_enricher_prep_node.py:154`** — creates the agent via DI and calls `action_handler()` inline:
  ```python
  agent = DI.agent_factory.create_agent("context_enricher::planner")
  ```
- **Dayflow chain** — `triage_persist_node → context_enricher_prep_node → context_enricher_persist_node → strategic_planner_prep_node`. The prep node fetches KG context, calls the agent, the persist node writes annotations onto items.

So the **agent** (`context_enricher::planner` at `app/assistant/agents/context_enricher/planner/`) is alive and well — used directly by the prep node. The **manager wrapper** is orphan scaffolding that was built early under a planner-tool design and never wired up after dayflow chose direct invocation.

## What's archived

- `app/assistant/multi_agents/context_enricher_manager/config.yaml` (the manager wrapper)

That's it.

## What stays live (untouched)

- **Agent**: `app/assistant/agents/context_enricher/planner/` — invoked directly by the prep node
- **Control nodes**: `context_enricher_prep_node.py`, `context_enricher_persist_node.py` — wired into dayflow's state map
- **Dayflow chain**: untouched
- **Standalone tests**: `app/assistant/tests/manager_tests/context_enricher/` is gitignored — local-only; not part of the archive

## Restoration

`git mv` the manager config back, then it's a 1-line restore. Note: with the agent still alive, restoring the manager just adds the manager-as-tool entry point back. If you also want to actually USE the manager (rather than the direct path), wire it into a planner's `allowed_tools` somewhere.
