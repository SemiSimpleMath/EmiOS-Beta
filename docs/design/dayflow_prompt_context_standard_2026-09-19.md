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

## External source context and wakes (2026-09-20)

External source context is prepared from intake metadata, with exact pod references,
source IDs, timestamps, sender/subject, message/thread IDs where available, and a
labeled summary/excerpt. Worker and finalizer views render this automatically via
`shared/work/task_context.j2` and `source_context.j2`; the strategic portfolio omits
excerpts but retains attribution, pod references and wake interpretations. These
are work-level facts available to every main task, including replacement tasks.
No pod fetch is added to prompt preparation or ordinary chat. The worker fetches
the canonical pod when supplied excerpts do not establish what its action requires.

External-wake preparation supplies full directives and all eligible external intake
candidates in the current intake view, without the former 30-item prefix limit.
The state mover returns `source_item_id`; persistence resolves it against that
prepared snapshot, never model-supplied attribution or pod IDs. The gate release
and a separate owned evidence record commit in one batch fenced by the work version
captured BEFORE the model call. The original task directive is unchanged. Evidence
stores the matched condition, original source metadata, and separately labeled model
interpretation. It is not a schedulable task or a tool result. Unknown sources and
changed snapshots leave the gate intact for a fresh assessment. This conservative
work-version fence can defer a second wake in the same work object until the next
pass if the first wake already changed that work object.

The model still judges semantic matching: a linked reply alone does not establish
approval. Prompt guidance requires a clear match and distinguishes source text from
interpretation. Existing historical records without structured sources are not
backfilled, and existing intake eligibility/age windows still apply.

## Contextual notification responses (2026-09-20)

The existing `ticket_builder::composer` designs 1–3 response choices in its standard
agent form and Jinja prompt; no additional agent call is introduced. Each choice has
a short label (at most 3 words / 24 characters; no question marks or line breaks), a
meaning, and a scope. Distinct labels and count/length rules are validated in Python;
semantic suitability is the composer's responsibility. Familiar labels are preferred.

The exact choices are saved in `trigger_context.response_choices` and exposed by
`Ticket.to_dict`, so push and poll/reload share the same UI. The popup renders escaped
labels and a text field. Click a choice with optional text, or press Enter to submit
text without a choice. Submitted IDs resolve against server-stored choices; the client
cannot substitute a label or meaning. Existing tickets keep their legacy layouts.
Tool-approval tickets continue to use their separate Allow/Deny authorization path.

Contextual tickets require action_type=none and no status effects: a response is
judgment input, never automatic permission to execute a ticket action. Receipt,
intention, user-reported completion, scoped yes/no, approval, decline, taking over,
and deferral remain distinct meanings. Typed text is saved separately and takes
precedence over the button. OK to a lunch reminder says only that it was received;
no agent should infer eating, availability or commitment, or ask again just to
eliminate that uncertainty. A Later choice expresses deferral; finalizer/planner
judgment decides what follow-up, if any, is useful rather than guessing a snooze time.

The contextual response write is atomic. Exact repeated latest replies are idempotent;
different replies to an already-responded contextual ticket append timestamped follow-up
history without resetting its execution/lifecycle state. Expired unanswered tickets
reject late replies explicitly. The current user_text holds the latest written answer
(or button label); user_response_parsed preserves exact label, meaning, scope, text,
and response history. Normal/recovered tool results and planner views render that
history with shared Jinja. Legacy same-state writes now reject different supplied
fields instead of silently returning success. They do not acquire follow-up semantics.

No historical ticket migration is required. Follow-ups enter recent-response planning
context; they do not rewrite finalizer judgments already committed before the follow-up.
