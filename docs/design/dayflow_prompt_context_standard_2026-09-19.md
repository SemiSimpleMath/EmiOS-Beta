# Dayflow agent context contract

Implemented locally, 2026-09-19. Python prepares structured data; Jinja owns prompt
wording and formatting. No global Message/blackboard precedence change was made.

## What each agent receives

| Reader | Work context |
|---|---|
| Architect | Full goal and success criteria; main assignments, their statuses, dependencies and wake conditions; full finalizer outcome/recommendation/question; pending versus applied revision instructions; action ledger. |
| Steward | The same strategic portfolio, plus recent completion/drop reasons, durable admitted intake and situational context. |
| Worker | Its assigned main task, full owned provenance and reusable dependency outputs. A resumed turn refreshes this view before deciding. |
| Finalizer | Full task directive, current attempt's result, owned provenance and strategic portfolio. |

Architect and steward never receive raw worker checklist/evidence records as main
assignments. Worker descendants remain provenance; finalizer summaries communicate
their significance to strategic agents.

## Implementation

- `dayflow_orchestrator/work_context.py` prepares dictionaries and renders shared
  templates under `agents/shared/work/` with StrictUndefined.
- Existing agent system/user templates include those views through their established
  task/information/portfolio inputs. Architect calls retain an isolated blackboard.
- Worker pre-node and resumed WorkPlanner calls share the same view. Required read or
  render failures clear stale context and raise.
- Main task statuses distinguish recorded results (`done`), accepted completion
  (`closed`), failure awaiting judgment, and judged failure with a revision route.
- Failure counters change only in the atomic finalizer judgment operation, once per
  dispatch attempt. Context preparation does not change graph state.
- New/changed goals retain source summaries and pod handles, and success criteria.
  Source content is rendered by `goal_content.j2`; intake closes after durable handoff.

## Inspect the real rendered prompts

[Complex architect and steward example](examples/dayflow_complex_work_prompts.md)
contains synthetic data rendered through the current production Jinja environment.
The architect input is captured through its real replan control node with LLM calls
and writes mocked. The example includes all main-task statuses, worked nodes,
finalizer comments, dependencies, pending revisions and a user approval condition.

`test_work_prompt_projections.py` checks complete directives, provenance exclusion,
worker history, stale-context failure and the actual architect/steward templates.
Set `EMI_WRITE_PROMPT_EXAMPLE=1` when running its rendering test to refresh the example.

The dormant WorkFinalAnswer class remains outside this contract (DF26). Remaining
legacy helpers and release decisions are recorded in the bug ledger.
