# The brain decides what work exists; dayflow decides how (design, 2026-10-04)

Status: proposed 2026-10-04, no code. Revised the same day: the steward retires (section 4). Follows a week of observation (2026-10-02 to 10-04) and the owner's
direction the same day. Builds on `three_layer_mind` (2026-09-29: the brain recommends, dayflow acts),
`dayflow_goal_ownership_2026-09-30.md`, `what_was_done_reaches_the_brain_2026-10-03.md` and
`work_progress_for_outside_readers_2026-10-04.md`.

## The owner's words

"it makes a lot of sense if brain races all the concerns comes up with the broad tasks related to the concerns
and then tells its assistants to handle those parts. then there is no duplicate work."

"brain is big picture: We have this concern this problem has to be solved, steward this is a task i give to you
i don't care how you do it, but note i can interrupt you any time and tell you to cancel this task, modify it
because new info has come in etc."

## Why: two deciders of what work exists

Today two parts decide what work should exist, with different views:

| Decider | Decides for | Sees |
|---|---|---|
| The brain's concern planner (`subconscious/concern_planner.py`) | concerns | the concern's full record, attached work and its progress, the journal, the owner's recent chat, lookups (calendar, email, todos, the knowledge graph) |
| Dayflow's steward (`strategic_planner_wo`) | everything that reaches dayflow: concern handoffs, each approved calendar placement, allowlisted pods | its portfolio, the item in front of it, entity cards |

A concern handoff is decided twice, first by the planner, then again by the steward. Calendar placements reach
the steward by code (`subconscious/belief_occurrences.py`) and are never decided by the brain at all. What went
wrong this week came from that split:

- **Timesheets (2026-10-04).** The approved calendar belief B455, "sometimes likes to get a head start on his
  timesheets on Sunday", was placed all day and reached the steward, which invented "Set a ToDo" and, separately,
  a 5 PM heads-up. The owner had a todo already and had told chat "I'll start at 5". The todo work then spiralled
  into "assign it to the owner" and a request to authorize Google Tasks (bug list, 2026-10-04).
- **Ring battery (2026-10-04).** The concern was handed over three times in three hours; the steward caught one
  duplicate ("Do not duplicate the scheduled notification") and the second handoff rested on a misread time (the
  journal reached the planner in UTC; fixed cbfb1f4c).
- **Morning briefing (2026-10-03).** The brain opened a concern from dayflow's own in-progress retry and handed
  it back; only the steward's no_action prevented a second delivery.
- **The physical (2026-10-03).** A calendar reminder went out from a belief while the concern's record said "no
  progress checks"; the brain attached it afterwards.
- **Approved calendar items carry no instruction.** Approving a belief for the calendar says "this matters
  today", not what to do. The steward improvises an act from a statement of tendency each day (a todo, a
  heads-up, "an encouraging check-in with a funny fact").

## The contract

- **The brain owns what and why.** It keeps one list of what needs doing and decides each entry with its full
  context. It hands each entry to an assistant as a task, by id.
- **Dayflow owns how.** Code turns a decided task into a work object as given; the architect plans the steps
  within what the assistant can do; workers act; the finalizer judges each step. Dayflow does not decide whether
  the task should exist.
- **The brain may interrupt at any time.** Cancel a task, change its objective because new information came in,
  or add to it, always by the task's id. Dayflow treats that as authoritative.
- **Dayflow reports every step back by the task's id**: progress, judgments, the ending. "This cannot be done as
  asked" or "the owner said X during the work" is a report to the brain, never a silent change of plan.

## 1. The list

One record per thing that needs doing (new; stored by the brain, beside the concerns):

| Field | Holds |
|---|---|
| id | the task's identity; carried by every work object made for it and every report about it |
| what | the outcome wanted ("the doorbell battery is charged"), not a delivery ("send a notice") |
| why | the concern it serves, or the calendar item and its belief |
| decision | needed / not needed (with the reason) / done elsewhere (e.g. by chat, with the message id) |
| assigned to | dayflow, or none |
| status | open, delegated (with the work object id), done, cancelled (with the reason) |

The list is what removes duplicates: a new decision about a concern or a calendar item first reads that item's
entries, so a changed plan adds only what is missing and never re-sends what is delegated.

## 2. What the brain decides from

- **Concerns**: as today's concern planner does (its "things that should be done" become entries).
- **Today's approved calendar items**: the day pass already places them; instead of handing each placement to the
  steward, the brain decides per item whether today needs anything, given what it knows (the owner's chat, the
  todo list, the calendar, work already running). "Sometimes likes a head start on timesheets", with a todo in
  place and "I'll start at 5" in chat, is "not needed".
- **What the owner did directly**: a chat request becomes an entry already done (with the message id), so the
  brain never delegates it again. The chat side of this (chat reporting what it did) is in the work-engine note
  (`work_engine_2026-10-03.md`, build step 1).

### What the assistant does with each calendar item

Approving a belief for the calendar today carries no instruction. The morning review would also ask what the
assistant does with it: **remind** (when), **do it** (the act), or **just know it** (context for the day, no act).
That answer is the item's standing instruction; the brain's daily decision is only whether today needs it. B455
would be "just know it", or at most one reminder at 5 PM.

## 3. Delegation and interruption, on existing plumbing

| Order | Today | With this design |
|---|---|---|
| Delegate | `subconscious/concern_handoff.py` writes an intake item; the steward judges it | code creates the work object from the task (`work_store` `create_work_object`): objective = the outcome, success criteria = its done-when, the task id and provenance in its constraints; the architect is woken for it |
| Cancel | the steward can ask the architect to end a work object (`end_requests` → `instruct_goal`) | the brain issues the same `instruct_goal` by task id with its reason; the architect ends the work |
| Modify | the architect can revise the objective (`revise_objective`) | the brain issues the new objective by task id; the architect revises and re-plans |
| Report | every judgment and ending reaches the brain inbox (`concern_feedback`) | the same receipts, routed by task id to the entry, not by meaning |

A cancel takes effect between steps. A tool call already running finishes or times out; the execution-ownership
rule revokes further calls and does not kill threads (`EXECUTION_OWNERSHIP.md`). The brain hears what ran and
what did not.

## 4. What changes, per part

| Part | Today | Then |
|---|---|---|
| Brain | concerns, notes, the concern planner's plans | also the list; decides concern pieces and today's calendar items; issues cancel and modify by id |
| Concern planner | writes to-dos; hands over to the steward | becomes the brain's delegation step |
| Day pass / occurrence handoffs | each placement handed to the steward by code | placements go to the brain's decision, not to dayflow |
| Steward | decides what work exists; reviews intake; maps the owner's feedback to work; keeps work within capabilities; invents acts | retires (below) |
| Architect, workers, finalizer | as now | as now; reports carry the task id |
| Morning review | which beliefs go on the calendar | also what the assistant does with each |

### The steward retires

Each of the steward's jobs (`strategic_planner_wo`, `prompts/system.j2`) has a new home:

| The steward's job today | Where it goes |
|---|---|
| Decide whether work should exist; review intake as no action or deferred, with a reason (`intake_review.py`) | the brain's list |
| Avoid duplicates by reading the portfolio | the brain's list: each item is decided once, against its own entries |
| Map the owner's feedback to existing work ("request its end or CHANGE it") | the brain's cancel and modify by task id |
| Check for missing high-impact information | the architect, which can already plan a step that asks the owner |
| Capability and safety limits (section 5 of its prompt): work only within real system capabilities; out of bounds unless the owner asks (enrolling, forms, purchases and subscriptions, sign-ups, choosing doctors or financial products, submitting official documents, unknown portals, personal details); "reduce it to the nearest safe reachable action: summarize, notify, draft, or ask the user"; the assistant is the automation | **the architect**, as a shared partial both read during the transition (the architect's prompt has none of it today) |

The steward's capability judgment is real work. On 2026-10-04 the brain asked for "the doorbell battery is
charged" and the steward wrote "physically removing and charging it is not an available tool action; this work
is a notification". With the steward gone, the architect does that translation as part of planning how. When an
outcome is not reachable at all, the architect says so as a report to the brain ("the assistant cannot charge a
battery; it can notify and check"), never by quietly substituting a different goal. The brain decides what to do
with that, and gets a short summary of the assistant's capabilities (the same partial, abbreviated) so it rarely
asks for what cannot be done; the detail stays with the architect and the workers, who know their tools.

**Intake that does not come from the brain** reaches the steward today: camera pods and allowlisted pods (memory:
steward intake = concern handoffs + camera pods). It moves to the brain's inbox like every other source. The
brain decides on camera events too (owner, 2026-10-04: "brain will decide on camera too. its a valid delta
input"); there is no path around it. A camera event is read at once, as an email is (`brain_inbox.ready`), not
held for a quiet room.

This removes a model stage from every piece of work: today a task passes two judges (the brain's planner, then
the steward) before anything is planned.

## 5. Testing without disturbing the current system

1. **Replay, offline.** Build the brain's delegation decision as its own agent and run it on the recorded inputs
   of 2026-10-01 to 10-04. Expected answers, set by the owner: timesheets, no task; Ring, one task with a notice
   and a check and no second handoff; the briefing, no new concern from a retry; the workshop, let go (minor); the
   physical, one task, marked done when chat booked it. Nothing live changes.
2. **Shadow, live.** The delegation step runs in the brain's pass and writes to its own table; nothing is handed
   to dayflow. A daily comparison with what dayflow actually did:
   - agrees: the brain's task matches work dayflow made;
   - dayflow-only: work the brain would not have created (the duplicates and invented acts this removes);
   - brain-only: what the brain thinks is needed and dayflow never did;
   - would-have-cancelled: running work the brain would have stopped.

   The owner judges only the disagreements; each judgment becomes a replay case. The limit: shadow mode measures
   whether decisions differ, not what would have happened, so the owner's judgment is the signal, not a score.
3. **Cut over one lane at a time, behind a switch.** Calendar items first (most of the duplicates), then concern
   handoffs, then pod and camera intake. A config flag returns each lane to the steward. The steward retires
   when no lane uses it; its capability section has moved to the architect by then (it is needed from the
   first lane on).

## Risks

- **One decider is one point of failure.** A wrong decision by the brain now shapes everything downstream. The
  replay suite is the guard; every change to the delegation step runs against it first.
- **The brain's loop grows.** Today it takes one matter at a time; deciding the day's calendar items is a batch,
  and the list has to be read before every decision. Its cost and latency need measuring in the shadow phase.
- **Timing.** The brain waits for a quiet chat before reading it, so a decision can wait minutes. Camera events
  and emails are read at once; the time from a camera event to the brain's decision needs measuring in the
  shadow phase, since every camera alert now waits for that decision.
- **The brain must not become a second architect.** Entries state outcomes; how is dayflow's.

## Open questions

1. Is the list a new table beside the concerns, or entries inside each concern's record (with calendar items as
   their own kind)? Calendar items are not concerns (2026-10-03), which argues for a table.
2. When does the brain decide the day's calendar items: once at the day boundary, at each wake, or both (once,
   then again when something bears on an item)?
3. How is "what the assistant does with each calendar item" asked in the morning review, and for the 45 approved
   beliefs already on the calendar, once in a batch?

## Build order

1. The delegation decision as an agent, replayed on 2026-10-01 to 10-04 against the owner's expected answers.
2. Shadow mode in the brain's pass, with its table and the daily comparison.
3. "What the assistant does with it" in the morning review.
4. The capability and safety section as a shared partial in the architect's prompt (and abbreviated for the
   brain), replayed on recorded architect calls before the first lane cuts over.
5. The calendar lane cut over behind a switch, code creating its work objects; then concern handoffs, with cancel
   and modify by task id; then pod and camera intake into the brain's inbox. The steward retires.
