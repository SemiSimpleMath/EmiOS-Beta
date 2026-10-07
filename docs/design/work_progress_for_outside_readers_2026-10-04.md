# Work says where it stands, in words an outside reader understands (design, 2026-10-04)

Status: built 2026-10-04, committed as 241ca486 (commit time 2026-10-03 20:58 -0700) and live. Open
questions 1 and 2 decided as proposed (owner, 2026-10-04: "go with your proposals"). Results and the
changes made while building are under "Built" at the end.

## The problem

Owner, 2026-10-04: "So they don't quite speak the same language and it is ambiguous. because if it says not
sent. then i too unless i knew that the dayflow works it until either closed or abandoned with reason. so
either whats written into the wo has to be clearer to an outside reader (prefer) or it has to be explained
in a skill or prompt." The owner prefers the record: the work states what it has done, what it will still
do, and how it ends, so any reader understands it without knowing how dayflow works.

Dayflow's own agents (architect, steward, worker, finalizer) read a work object through
`shared/work/portfolio.j2`. That view shows every main task's status, dependencies, waits and finalizer
summary. Readers outside dayflow see much less:

| Reader | What it sees of a work object today |
|---|---|
| Concern record, read by the brief writer, the concern planner and dayflow's agents (`shared/work/concern_brief.j2`, `concern_brief.attempts`) | objective, a bare status word (`active`), each finalizer judgment's prose, the ending |
| The gate and the brain step, through a work event (`concern_feedback._event_text`, built in Python) | the one judgment or ending, the objective, the concerns it was made for |
| The brain step's "already in motion" (`work_links.active_work`) | title, objective, when it started |

None of them sees the steps still to come. A finalizer judgment describes one task, so "the draft remains
unsent for the separate delivery step" is true of the compile task. A reader outside dayflow takes it as a
statement about the whole work.

## The case (2026-10-03, from the records)

| UTC | What happened |
|---|---|
| 14:28 | The calendar placed the morning briefing (B94). Dayflow made work_9b2c0e852a91 with two tasks: compile, then deliver. |
| 14:36 | The compile task was judged `retry`. The work cites no concern, so the gate routed the report `new_matter`. |
| 14:37 | The brain opened "Complete and deliver the owner's October 3 morning briefing" (66321843) and attached the work to it. |
| 15:13 | Compile judged `achieved`: "The October 3 briefing draft is complete and remains unsent for the separate delivery step." |
| 15:14 | The concern planner read the concern: work `active`, one judgment saying "unsent". It decided `act_now`: "the delivery step is only proposed and the record says it remains unsent". It handed the concern to dayflow. |
| 15:16 | Dayflow's own delivery task was judged `achieved`. The work ended `done`. |
| 15:28 | The steward reviewed the handoff `no_action`: "Do not duplicate delivery." |

The planner's prompt already says that work in progress is no reason to start more. It could not tell
that the work was in progress, because nothing it read said the delivery was coming.

## The design

### 1. A progress view of every work object, computed by code

`work_progress(wo)` is computed from the work object's main tasks (`WorkObject.is_work_unit`), the same set
the architect sees. It returns plain data:

- **state**: in progress; ended done; or ended abandoned, with the architect's reason.
- **done**: each closed task with its title, verdict and when it was judged.
- **still to come**: each open task with its title and what it waits for, in the outside reader's terms:

  | Task state in the store | Said as |
  |---|---|
  | `actionable`, or `proposed` with its gates clear | ready now |
  | `proposed`/`waiting` with a future `wake_at` | scheduled for <local time> |
  | `proposed` behind an unfinished dependency | after "<that task's title>" |
  | `dispatched` (not an ask) | being done now |
  | `dispatched` with `wake_kind=user_reply` | waiting for the owner's reply to "<the ticket's question>", asked <local time> |
  | `done` (result recorded, not judged yet) | done, being checked |
  | `failed` | did not succeed; the assistant is deciding the next attempt |
  | a pending architect revision on the work | the assistant is re-planning this work |

  The mapping is made complete against `TRANSITIONS` at build time: a status with no phrase raises an
  error and is never silently skipped.
- **how it ends**: a fixed sentence rendered with the state: "The assistant carries this work until every
  step is done, or abandons it with a reason." This states how work behaves. It sits in the record beside
  the work, not in any agent's prompt.

Provenance below the main tasks (worker-made descendants) stays out, as it does for the architect.

### 2. One template, every outside reader

`shared/work/work_progress.j2` renders the view, for example:

```
- Deliver the owner's morning briefing for October 3 (work_9b2c0e852a91)
  In progress. The assistant carries this work until every step is done, or abandons it with a reason.
  Done: Compile the October 3 morning briefing (achieved 8:13 AM).
  Still to come: Deliver the morning briefing to the owner (ready now).
```

Ended: `Ended 8:16 AM: done, every step achieved.` or `Abandoned 9:15 PM: <reason>.`

It is included by:
- `concern_brief.j2`, for each piece of work attached to a concern, above that work's judgments;
- the work event the gate and the brain read: `concern_feedback._event_text` moves into a template
  (`shared/work/work_report.j2`). The judgment or ending comes first, then the work's progress at that
  moment. Agent-facing work text is rendered by the shared templates (the 2026-09-19 standard); this
  event text was the exception;
- the brain step's and the brief's "already in motion" (`work_links.active_work`).

### 3. Read live; at the moment of the event for the event

- Concern views and "already in motion" compute the progress from the work store when they render. The
  concern record keeps what it keeps today (judgments, ending). Progress is not copied into the register:
  a step moving from queued to dispatched does not change the concern's record. It does not make the brief
  stale, and it does not cost a brief or a plan.
- A work event carries the progress at the moment of its change. The judged and ended receipts gain a
  `progress` field computed in the same transaction (`work_objects/concern_outbox.py`). The event text is
  stored in the brain inbox as it was read.

No new table, no new receipt kind, no change to stored concern data. The outbox payload gains one field.
The receipts are transient and deleted on delivery.

### 4. Work no concern holds

Calendar work, steward work from pods and, later, chat work reach the gate with no concern. With its
progress in the event, an in-progress report shows it is covered: "Still to come: Deliver … (ready now)".
The gate's prompt keeps its rule that routine work going as planned is routed `none`. Nothing else
in the gate changes here; the record now lets it tell what is going as planned. Whether an ended work
whose need is still unmet may become a new matter stays the brain's decision, as today.

(Superseded 2026-10-05/06: dayflow's pod intake was retired (0d43a54c; camera frames go to the brain inbox) and
the steward was retired (d02e72e2, `configs/brain_lanes.json` `steward.mode`), so there is no new "steward work from
pods"; calendar and concern work now come from the brain's list, see `brain_delegates_work_2026-10-04.md`.)

## What this does not change

- The finalizer's verdicts and prose still judge one task.
- The workshop case (2026-10-03: one expired ticket became the concern's rule never to ask again) runs the
  other way, from a dayflow decision to a concern's rule. It is in the bug list and needs its own decision.
- Ownership of record per work object (owner, 2026-10-01: a concern, the beliefs, or a request of the
  owner's) is not built here. The progress view is the part of it this case needed.

## Verification

1. A unit test per row of the mapping table, plus one asserting that every spine status has a phrase.
2. Replay the 2026-10-03 briefing: render the concern as the planner read it at 15:14, with the progress
   view. Run `subconscious::concern_decider` on it five times and expect `no_action` each time, against
   `act_now` on today's view.
3. Replay the 14:36 retry event through the gate with the progress view and expect `none`.
4. Read the rendered concern view, the event and "already in motion" for one live work object of each
   shape: scheduled, waiting on a reply, failed and re-planning, ended.

## Open questions

1. Should the gate drop in-progress reports of work no concern holds before the model call, by code
   (they are routed `none` when nothing else bears on them)? Proposed: no. A report can bear on another
   concern ("work done for one concern often answers another"), and the gate reads it with that in view.
2. Does "Still to come" list all open main tasks, or only the next ones whose gates are clear? Proposed:
   all, in the architect's order. Work objects have few main tasks.

## Built (2026-10-04)

- `work_objects/progress.py` (`work_progress`), `shared/work/work_progress.j2`, `shared/work/work_report.j2`.
- Readers: `concern_brief.attempts` (concern record for the brief writer, planner, handoff and dayflow
  agents), `shared/brain/concern.j2`, `work_links.active_work` ("already in motion"), the work events
  (`concern_feedback._event_text`, `report_earlier_endings`), and `read_work_object`.
- `read_work_object` was a reader the note missed. The 15:14 planner had looked the work up itself and seen
  "[proposed] subtask: Deliver morning briefing"; its decision reads "the delivery step is only proposed".
  The tool now shows "WHERE THE WORK STANDS NOW" above its record dump.
- Ended work with steps still open (394 legacy main tasks on 2026-10-04, from before the finalizer closed
  every task) lists them as "Not finished when the work ended", never as still to come.
- Wording, measured: the first wording ("Still to come: … ready now") moved the 15:14 decider only from 6/20
  to 10/20 `no_action`. Saying who does the steps moved it to 17/20: "Still to come in this work (the
  assistant does each step itself, as part of this work): … ready now; the assistant does it next".
  The gate on the 14:36 retry: 5/5 `none` with progress (recorded prompt: 4/5 on replay; it routed
  `new_matter` live). Scripts: `scratch/replay/work_progress_replay.py`, `work_progress_n20.py`.
- `work_progress` runs over every work object in a copy of the live store without an error (1,620 on
  2026-10-04).
- Tests: `app/assistant/tests/dayflow/test_work_progress.py`; full suite 2,988 passed.
