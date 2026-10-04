# The day's items become concerns (design, 2026-10-02)

Status: shape agreed 2026-10-02. Built 2026-10-02: points 1–5 (placements; owners through the door; standing
concerns; handoff of occurrences; the steward reads the routine as context) and the concern planner for matter concerns
(`subconscious/concern_planner.py`, `concern_planner_manager`; it replaces the brief's readiness for them).
Not built: the check that a node's wake matches its occurrence's time.
Superseded 2026-10-03 for points 2–4 (owners through the door, standing concerns, occurrence handoffs by
concern): beliefs are not concerns (`what_was_done_reaches_the_brain_2026-10-03.md`). Placements and the
concern planner stay.
Step 2 (with 3) of `brain_owns_the_day_2026-10-01.md`.

## The problem

Dayflow comes up with its own tasks and nothing connects them to the concerns. The steward reads the
day's routine as text and makes work from its lines; that work has no owner, so its outcome reaches
nothing. 2026-10-02: 28 work objects created, 0 citing a concern. The Seija email went out at 05:21 and
the routine kept saying "not confirmed" all morning; the steward then re-made the email work five times.

Work started from a concern does not have this problem: the brain hands the concern over as an inbox item
and `work_intake.goal_update` copies the concern id from the item into the work's `concern_refs`, by code.
Every judgment and the ending reach the concern (the annual-physical concern is the live example).

## The aim

One unified set of concerns for the day. The routine's items become concerns and are merged with the
concerns from every other source, so each item of the day has exactly one owner, and dayflow only makes
work from a concern it was handed.

## The shape

1. **The model places beliefs; code builds the day.** For each belief that applies today the model returns
   its key and the time of day it applies, once per time (`B149, 9:00 PM`; `B149, 7:00 AM`). Code writes
   each row (belief key, time, statement) to the database and builds the day's calendar from the rows, as
   it places calendar events. (The 2026-10-01 per-slot form misplaced 9 PM items into 9 AM in 5–13 of 60
   replays; this shape, 0 of 100 on the same inputs.)
2. **The day's actionable items become concern candidates**: what should happen, its time today, the
   beliefs it rests on. Context beliefs stay context.
3. **The concern door merges them** with the open concerns, as it merges every other source today:
   - the same matter as an open concern: merged into it (the 5 PM flea dose into the flea concern; the
     Seija link into a Seija concern, so a second send has an owner that already holds "sent at 05:21");
   - a recurring habit: one standing concern per habit (the evening AC, the liquid cutoff), reused every
     day, never resolved, each day's outcome recorded on it;
   - otherwise a new concern.
4. **Handed over by code.** Each of today's concerns is handed to dayflow when it is created, as concerns
   are now, unless it is for another day with no bearing on today. The handoff carries the item's time;
   work made from it is tagged to the concern in the database by code.
5. **The steward stops making work from routine text.** The ROUTINE stays in its prompt as context for
   timing and wording.

## The reasoning layer: a concern planner

Everything the brain has today makes one decision from a packet code prepared, with no tools: the gate
routes events, the brain step notes and resolves, the door merges, the brief summarises and decides act
now / hold / no action. Nothing investigates a concern or works out what a change means for what was
already done.

The concern planner does: an agent with tools that reasons about one concern and breaks what it needs into
actionable tasks. It does not carry them out.

- **Reads**: the concern and everything under it (all attached work and every outcome), email threads, the
  calendar, reminders, active work, the knowledge graph. Looking things up is its own job, so the tasks it
  writes are already informed ("the 16th clashes with the son's game").
- **Decides**: edits, holds or dismisses the concern; writes tasks, each with the outcome wanted, the
  consumer, why, and which earlier work it corrects; asks the owner.
- **Runs** when a concern changes, when its hold passes, or when attached work reports back; never on
  every concern every pass.
- **Its tasks are hints.** Each goes to its consumer as a handoff carrying the concern's brief and the
  reason, as concern handoffs do now. The steward still decides what work exists (work, defer, decline,
  with a reason); work it makes is tagged to the concern by code, and every outcome lands back on it.

Example: the book club moves from the 9th to the 16th. The planner reads what is attached (food ordered
for the 9th, #123; eight invitations; directions; a reminder for the 9th), checks the calendar for the
16th, and writes: ask the caterer whether #123 can move or must be cancelled; tell the eight invitees the
new date; move the reminder. Outside contact still needs the owner's go.

## Checks

- Every work object created from today's routine carries a concern.
- The Seija case: one concern holds the approval, the send and "sent at 05:21"; no second send work.
- The 5 PM flea dose and the open flea concern are one concern; the date disagreement is on it.
- The book club (planner): a recorded postponement yields one task per affected piece of earlier work,
  each naming the work it corrects, and a calendar check the planner did itself.

## Open questions for the owner

1. **Which placed beliefs are actionable** (a device action, a reminder, a message) and which are context:
   the model marks it on each pair, or the belief's own kind decides?
2. **Standing concerns**: one per habit (each with one time), or one per area (bedtime) holding several?
3. **Cost**: the door call per pass only when there are new candidates; repeats of yesterday's habits
   match their standing concern. Brief rewrites for standing concerns only when an occurrence fails or a
   belief behind it changes?
4. **The plan as data (1a)**: superseded by the owner's shape (2026-10-02): the model returns
   `(belief_key, time)` pairs, a belief once per time it applies today; code stores id, time and statement
   and builds the calendar. 100 replays of the 06:19 inputs: no misplacement.
5. **Concern planner scope**: decided 2026-10-02: matter concerns only; a standing concern keeps its
   brief's per-occurrence readiness rule. The planner writes bullet points under the concern ("things that
   should be done"); consumers interpret them.
6. **Concern planner order**: after steps 2 and 3, when every item of the day has a concern holding its
   history?
