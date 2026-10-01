# The architect owns a work object from creation to end (design, 2026-09-30; built 2026-09-30)

Decisions: "time running out" is the stranded state (every scheduled time in the work object has passed
with nothing settling it; built as: nothing in it can still run and no time lies ahead). An owner's
directive to drop a goal also goes through the architect, as a steward end request.

## Why

2026-09-30, the morning thermostat work (work_4032b7355fcc):

- 07:00 and 07:02, two attempts to set the Nest to 75°F fail ("no linked Google/Nest account").
- 07:03, the finalizer judges `unrecoverable`, next step `ask_user`, with the question to ask.
- The architect, acting on that judgment, adds `ask_nest_access_or_manual`, timed for 10:20.
- 07:20, the steward abandons the whole work object ("the 7:00 time has passed"); the cascade
  abandons the planned question. The owner is never asked.
- For the rest of the day the steward creates and drops tonight's 70°F setpoint again and again
  (01:07, 08:28, 10:38, 13:23), each drop recorded only as "superseded, declined, or no longer
  relevant".

Both agents acted correctly by their own signal: the architect on the finalizer's judgment, the
steward on the objective's wording against the clock. Two agents hold the same work object, and the
one that ends it is not the one holding its plan. The store already fences the architect (it may
prune only on a finalizer or owner licence) and holds automatic completion while a finalizer
instruction is unconsumed (`_rollup`), but the steward's abandon has no fence.

Owner, 2026-09-30: "Steward and architect fight each other." "Why can't the architect end the work
object, I thought it owns its lifecycle?" "It may be better to keep re-using the wo anyway: just have
the architect close it, or add a new node that the user has to be notified before trying again."

## The split

| Decision | Today | After |
|---|---|---|
| Turn intake into a new work object | steward | steward |
| Fold new intake into an existing work object (new source, changed objective from intake) | steward (`revise_goal`) | steward |
| Notice two work objects are one goal | steward | steward, as an instruction to the architect |
| Lay a new work object's tasks | architect | architect |
| React to a finalizer judgment (retry, new approach, ask the owner, stop a branch) | architect | architect |
| Change the objective because its premise expired (a time passed, a result changed it) | steward, rarely | architect |
| Complete or abandon the work object | steward | architect |
| React to a failure by planning more work | steward (new work object) | architect (new tasks in the same work object) |

The steward decides what work exists. From the moment a work object exists, everything that happens to
it, to its end, is the architect's.

## The architect

- Output gains goal-level fields, applied in the same store batch as its task changes:
  `end_goal` (`done` | `abandoned`, with a reason written as the goal's epitaph) and
  `revise_objective` (new objective and success criteria, with a reason). The store keeps its checks:
  a done ending still needs its user-facing hand-offs run; an abandoned ending records the reason.
- Prompt rules (Jinja):
  - A failure the finalizer cannot recover from is handled inside the work object: when a retry
    needs the owner (access, a decision, a manual step), add a task that tells or asks the owner,
    and make the retry depend on its answer. Never retry silently after a finalizer said the owner
    is needed.
  - When the objective's premise expires but a finalizer instruction still stands, revise the
    objective to what is still needed ("set 75°F at 7:00" becomes "resolve thermostat access with
    the owner") rather than ending it.
  - End the work object when its outcome is reached, or when nothing reachable remains and the owner
    has been told; say why.
- It is woken, per work object, by:
  - a finalizer judgment with a next step (as now);
  - the work object's time running out: its scheduled tasks' times passed and no task settled it;
  - a steward instruction (duplicate, superseded by intake, owner said drop it);
  - an owner directive about the work (a ticket reply naming it).

## The steward

- Keeps: intake to new work objects; folding intake into existing ones; noticing duplicates.
- Loses `complete_work_ids` and `abandon_work_ids`. When it judges a work object should end, it writes
  an instruction with its reason (`end_instructions: [{work_id, reason}]`), which reaches the
  architect the way a finalizer judgment does.
- Its view of existing work shrinks to what deduplication needs: objective, status, and the last
  outcome in a line. The full finalizer judgments move out of its prompt (smaller, cheaper), since
  reacting to them is no longer its job.

## What does not change

- The finalizer judges each task; the brain hears the judgments and the ending of work attached to a
  concern (concern_feedback). A concern outlives any one work object.
- The runtime's repeat-failure rule (ask_user at `_REPEAT_FAILURE_LIMIT`) and the architect's pruning
  fence stay.

## Open decisions

1. "Time running out": the definition above (every scheduled time in the work object has passed with
   no task settling it) or a deadline field on the work object.
2. Whether an owner directive to drop a goal ("cancel the AC") may still end it at once through the
   steward, or always goes through the architect.
3. The fixed drop reason ("superseded, declined, or no longer relevant") goes away with the steward's
   abandon list; existing epitaphs stay as they are.
