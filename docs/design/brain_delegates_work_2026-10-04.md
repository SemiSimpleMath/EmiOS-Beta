# The brain decides what work exists; dayflow decides how (design, 2026-10-04)

Status: proposed 2026-10-04. Revised the same day: the steward retires (section 4); a control node builds each
work object shell from the brain's list by code and routes modifications and cancels by entry id (section 3,
"The list-item intake node"). Build steps 1 and 2 built
2026-10-04 for calendar items, SHADOW ONLY: `subconscious::day_decider` (`subconscious/day_decision.py`) decides each
placement on every brain wake when its inputs changed, records it in `brain_day_decisions`, hands nothing to
dayflow; `python -m app.assistant.subconscious.day_decision <day>` sets it beside what dayflow did. Replayed on the
recorded 2026-10-04 morning (`scratch/replay/day_decider_replay.py`): timesheets not needed 6/6, the 6 PM
family-time heads-up 3/3, lights off after bedtime one task on the inactivity signal; every answer valid.
"Who acts" stays in shadow (the live concern decider is unchanged).
The list itself built 2026-10-04 (shadow): `subconscious/brain_list.py` keeps entries with stable ids and every
change (created, modified, cancelled, with its cause) in `brain_list_entries` / `brain_list_changes`; calendar entries
are keyed by code to their placement and the day decider sees each placement's current entry; after each live
concern plan, `subconscious::concern_list_decider` (`subconscious/concern_list_shadow.py`) keeps, modifies or
cancels the concern's entries by id and adds what is missing, from the same record and lookups the live decider
read. Replayed: the Ring repeat handoff of 2026-10-04 becomes "keep" in both runs. Open: "who acts" read two
ways (the person who does the work, or the one who reminds); one run planned reminders sent to the owner's son
directly; entries per concern vary from one to five. Follows a week of observation (2026-10-02 to 10-04) and the owner's
direction the same day. Builds on `three_layer_mind` (2026-09-29: the brain recommends, dayflow acts),
`dayflow_goal_ownership_2026-09-30.md`, `what_was_done_reaches_the_brain_2026-10-03.md` and
`work_progress_for_outside_readers_2026-10-04.md`.

**Current state (recorded 2026-10-07; the status paragraph above is the 2026-10-04 record).** No longer shadow:
the concern lane is live since 2026-10-05T16:31Z and the calendar lane from day 2026-10-06 (8ea18f73); the steward
is retired (d02e72e2), all three by `configs/brain_lanes.json`. `subconscious/day_decision.py` became
`subconscious/day_tasks.py` (agent `subconscious::day_tasks`, fca75318); `subconscious::concern_decider`,
`subconscious::concern_list_decider` and `subconscious/concern_list_shadow.py` were removed, replaced by
`subconscious::concern_tasks` inside the concern planner (ee2e2372). Retiring the steward did not change the
planning tick's state map: intake triage and the steward's prep node still run, and the prep node routes past the
steward to the list-item intake node (section "The steward's retirement, behind a switch").

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
- **Dayflow owns how.** A control node turns a decided task into a work object shell as given (section 3); the
  architect plans the steps within what the assistant can do; workers act; the finalizer judges each step.
  Dayflow does not decide whether the task should exist.
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
| who acts | the assistant, the owner, or another person by name; for someone else's job, `what` is the assistant's part in it (tell, remind, ask or check) |
| when | when it makes sense to do it (the evening before a deadline, not 3 AM), or now |
| why | the concern it serves, or the calendar item and its belief |
| decision | needed / not needed (with the reason) / done elsewhere (e.g. by chat, with the message id) |
| assigned to | dayflow, or none |
| status | open, delegated (with the work object id), done, failed, cancelled (with the reason) |

The list is what removes duplicates: a new decision about a concern or a calendar item first reads that item's
entries, so a changed plan adds only what is missing and never re-sends what is delegated.

**Each entry tracks its work by id.** The real reason for all of this (owner, 2026-10-04): the work dayflow does
gets back to the brain, and every concern is updated. An entry moves open → delegated (with its work object) →
done, failed or cancelled, set by code from the work store's receipts, matched by the entry id the work object
carries (never by the gate's judgment), with the finalizer's account in its own words. Whoever decides the entry
again sees that status: "the family-time heads-up: delegated to work_X, sent at 6:00 PM, judged achieved", or "the
lights: abandoned, bedtime could not be confirmed". Concerns keep hearing of all work as today: every judgment and
ending is a brain inbox event, routed by the gate, and work citing a concern updates its attached work by code.

**Calendar entries are keyed by belief and day, with the time as a field** (`cal:<day>:<belief>`), so a
placement the day pass moves to another time modifies its entry rather than making a second one.

## 2. What the brain decides from

- **Concerns**: as today's concern planner does (its "things that should be done" become entries).
- **Today's approved calendar items**: the day pass already places them; instead of handing each placement to the
  steward, the brain decides per item whether today needs anything, given what it knows (the owner's chat, the
  todo list, the calendar, work already running). "Sometimes likes a head start on timesheets", with a todo in
  place and "I'll start at 5" in chat, is "not needed".
- **What the owner did directly**: a chat request becomes an entry already done (with the message id), so the
  brain never delegates it again. The chat side of this (chat reporting what it did) is in the work-engine note
  (`work_engine_2026-10-03.md`, build step 1). Today the brain learns of a task chat took up only by accident: on
  2026-10-04 the chat gate tracked "I will start the timesheets at 5PM" as a task, and the brain's replay read
  "already in flight" from the internal marker that leaked into the chat as the assistant's reply (bug list). The
  fact was true and settled the timesheets item; the leak is a display bug. So chat's tracked tasks reach the brain
  as records before, or together with, the fix that stops the marker leaking.

### What every brain decider sees: the situation

Owner, 2026-10-04: the agent that turns the day's calendar into tasks "better get all the context in the world so
it can make good decisions. also brain better get the calendar too and if user is afk and users health and all of
that." It takes over the steward's job for the calendar, so it sees at least what the steward saw, plus the chat
and the todo list the steward never had.

Code gathers one situation per brain pass, and every brain agent that decides gets the same block (a shared
partial): the day's tasks agent (`subconscious::day_tasks`, below), the brain step, the concern decider and the
list decider. Every source exists today; the steward reads most of them. (As of 2026-10-07 only day_tasks reads it:
`agents/subconscious/day_tasks/prompts/user.j2` is the one prompt that includes `shared/brain/situation.j2`.)

| Part | Source |
|---|---|
| now, the weekday, what kind of day (a workday or not, a holiday) | the clock; today's calendar |
| dates that matter today (birthdays, anniversaries) | `resource_user_data` (the owner and the people in it) |
| today's calendar events, and the next 30 days | `resource_expected_calendar`; the brain step's calendar read |
| day theme and current status | `resource_daily_context_generator_output` |
| at the keyboard or away, and for how long | `resource_afk_statistics_output` |
| location, weather, health | `location_summary`, `resource_weather`, `health_status_summary` |
| today's chat; the todo list | the master room's log; the todo sync |
| work: everything in motion with its progress, what finished or was dropped today with the owner's reasons | the work store (`work_progress`; the steward's completed and dropped views) |
| reminders already sent (the cron wellness nudges among them); active tickets | `scheduled_reminder_history`; the ticket manager |
| open concerns; the owner's standing directives | the concern register; `resource_orchestrator_user_prefs` |

What each sees today (2026-10-04):

| Input | Steward | Day decider (shadow) | Brain step | Concern decider |
|---|---|---|---|---|
| the day's calendar items | intake + the ROUTINE | yes | no | no |
| today's calendar events | yes | no | 30 days | what its planner looks up |
| day theme, status | yes | no | no | no |
| away from the keyboard, location, weather, health | yes | no | no | no |
| reminder history, active tickets, recent nudges | yes | no | live reminders | what its planner looks up |
| work in motion | all | not from the calendar | all | what its planner looks up |
| completed and dropped work, recent outcomes | yes | no | linked and similar past work | the concern's attempts |
| entity cards | yes | no | knowledge-graph entities | no |
| today's chat | no | yes | the matter's conversations | no |
| the todo list | no | yes | no | what its planner looks up |
| directives, the steward's purpose | yes | yes | no | no |

Without the schedule and day theme the day's tasks agent cannot tell a workday from a day off; without the reminder
history it repeats the cron wellness nudges the owner's overlay says not to duplicate; without what finished today
it cannot tell that a heads-up already went out.

Once the brain is the only decider of calendar work, calendar-made work comes from its own list entries: the day's
tasks agent sees each entry with its work's progress (sent, failed, waiting), as a concern sees its attempts. In
shadow it is shown no calendar-made work, since that work would not exist.

**Name.** The day decider is renamed `subconscious::day_tasks` (owner, 2026-10-04): it turns the day's calendar
into the day's tasks, and it is part of the brain, run in the brain's pass with the brain's other agents.

**Built 2026-10-04 (build step 3a, shadow, needs a restart).** `subconscious/situation.py` gathers the situation
with no model call, through the readers already in use: the resource manager (schedule, day theme and status,
weather, health), `get_current_presence_status`, the location manager's summary, the key-dates builder, the brain
step's 30-day calendar, today's finished chat (the gate's quiet rule), the todo sync, `work_links` for work in motion
and for work that ended in the last 18 hours (the brain's past-work view: the finalizer's prose, never its verdict),
`scheduled_reminder_history` and the steward's active tickets. `shared/brain/situation.j2` shows it, every time
local. Only day_tasks reads it so far (in shadow without calendar-made work). Its fingerprint leaves out what moves
with the clock alone (minutes away, the temperature, the reminder window). Live run 2026-10-04 at 10:47 PM: about
7,500 tokens of situation, 11,900 input tokens per day_tasks call; the answer valid; it read the 8:30 AC change from
the schedule and the owner at the keyboard during the wind-down. Found on the way (bug list): the calendar tool
showed every agent UTC times (fixed); calendar-made work citing only a belief was not recognized (fixed); the
key-dates builder speaks for the romance proposer. The table `brain_day_decisions` keeps its name.

**Decide once, change only on something new (built 2026-10-05, shadow, needs a restart).** Live on the morning of
2026-10-05, day_tasks re-decided all 28 placements on every run (11 runs from 5:00 to 7:31 AM) and code compared the
text: 43 modifications, of which about 30 were rewordings ("the assistant" become "assistant"; covers dropped in one
run and restored in the next; a reminder that grew a clause each run) and about 6 were re-judgments with nothing
new ("The existing rationale incorrectly says…"). Now, as the steward and the architect: an item not yet on the list
is decided; an item on the list keeps its entry when the answer leaves it out; a change goes through (2026-10-05
to 2026-10-08 a change had to name in `new_since` a fact newer than the entry or was refused; removed when the gate
refused a correct re-decision 40 times in a day: the decider now reads each entry with its reason and its work's
ending instead); a placement done elsewhere or more than
an hour past is not offered again; an entry is cancelled only when its placement leaves the day's plan. `who_acts` is
gone from day_tasks (an entry is the assistant's own task). Three runs on the live inputs against a copy of the day's
42 entries: no list change in any (one decision restated with the same meaning, kept).

**The brain's view of the day (built 2026-10-05, build step 3b in part, shadow, needs a restart).** day_tasks reads,
besides the situation, every open concern in full as the brain's agents read it (`concern_planner.concern_text`:
record, brief, attempts, journal) with the concern's entries on the list, and what the brain read and decided today
(`day_tasks.matters_today`: each event in its words with the brain's decision and reason, notes, concerns raised). A
concern's entry (E1..) and a brain decision (B1..) are facts a change can cite. In the brain's pass the concern side
now runs before day_tasks, so an entry it makes reaches day_tasks in the same pass; a concern-side failure is raised
after day_tasks has run. Live, 13 open concerns: about 39,000 input tokens a call (from 11,000). Three runs against a
copy of the day's list: no change; once the fence refused a re-judgment with nothing new and the corrected answer kept
everything. Not built from 3b: entries tracking their work's status (it needs live delegation), calendar entries keyed
by belief and day (it changes stored ids: the owner's go), and the merge into `concern_tasks` (at cutover).

**The cutover machinery (built 2026-10-05, behind switches that start off, needs a restart).** (Switched since:
concern lane live from 2026-10-05T16:31Z, calendar lane from day 2026-10-06 (8ea18f73), steward retired 2026-10-06
(d02e72e2); `configs/brain_lanes.json`.)
- Calendar entries are keyed by belief and day (`cal:<day>:<belief>`; owner's go): one item per belief per day with
  every time the plan places it, so a moved placement changes its entry instead of cancelling one and making
  another. Today's shadow rows kept their old ids; the next run cancels them and makes the new ones.
- `subconscious::concern_tasks` replaces the concern decider and the shadow list decider (removed): inside the
  concern planner it reads the concern, what was looked up and the concern's entries, and answers act now, hold or
  no action with only the changes to its entries; the rest are kept. The plan keeps its shape (`todo` = the open
  entries). Replayed on Ring (the 3:31 repeat changes nothing, both runs), Canvas and Reflections (tell, ask and
  remind the owner; no homework, no submission for the son).
- The architect's prompt has a section on goals from the brain's list (plan to every constraint, the source wins
  over the entry, the same outcome another way, revise the objective with the reason when it cannot be reached) and
  the steward's capability and safety section as a shared partial (`shared/work/capability_limits.j2`; the steward's
  prompt renders byte-identical). A new goal answered with no steps and no ending gets one correction and is then an
  error (it would come back every tick).
- The list-item intake node (`dayflow_orchestrator/brain_list_intake.py`, `control_nodes/brain_list_intake_node.py`,
  between the steward's persist node and the architect) acts on the list's changes per lane in
  `configs/brain_lanes.json` (off, dry_run, live), recorded in `brain_list_deliveries`. A live calendar lane stops
  `belief_occurrences` handing that day to the steward; a live concern lane stops `concern_handoff`. The brain pokes
  dayflow when the list changed and a lane is live.
- The deciders see each entry's work live from the work store by its entry id (`brain_list_intake.work_of`: status,
  why it ended, each task's outcome in the finalizer's words). No status is copied onto the entry.

### What day_tasks reads: the brain's view of the day

The owner's worry (2026-10-04): it "sees less than brain". It is the brain deciding, so it sees what the brain
knows. The steward never got email as intake after 2026-09-30 (only inside a concern handoff); what it had beyond
the day decider was the portfolio, outcomes, the situation and the handed-over concerns. day_tasks reads, from what
is stored:

| Part | Source | Why |
|---|---|---|
| every open concern as the brain knows it | each concern's brief (what, known facts with sources, the owner's wishes, open questions, recommendation), its list entries and its attempts | the matters behind today's calendar items ("the physical is booked", "he will handle it himself") |
| what the brain read and decided today | today's `brain_matters`: each event (message, email, work report) with the brain's decision and reason, the concern notes it wrote, the new concerns | the owner's chat and email as the brain understood them, and what it set aside and why |
| all the work dayflow is doing | every active work object with its progress; what finished or was dropped today with its account | what is already in motion or done, wherever it came from |
| the day's list | every entry from both deciders, with its status | what was already decided and delegated today |
| the situation | the block above | when and whether now is right |

These records, not the brain's day story (`story_day`): the story is written for the owner, a summary of a summary;
the briefs and the matter decisions are the brain's own working knowledge (original context over summaries).

In shadow, day_tasks is still shown no calendar-made work: that work comes from the steward, and showing it would
let day_tasks copy the steward and empty the comparison. Live, everything calendar-made comes from its own entries,
and it sees all work.

### Two task deciders, one list

The brain decides what work exists in two places, each item in exactly one:

| | Decides | Runs |
|---|---|---|
| `day_tasks` | today's calendar items: a task or none, what the assistant does, when | when the day's inputs change |
| `concern_tasks` | one concern: its entries kept, changed, cancelled or added, or a hold until a time | when that concern's record changes or its hold passes |

`concern_tasks` is the concern planner's two halves at cutover: the read-only planner looks up what the concern
names (its email threads, calendar entries, reminders, todos, earlier work, pods, the knowledge graph; usually two
to five lookups), then one decider writes the concern's entries. In shadow the live `concern_decider` and the
`concern_list_decider` both run per concern; they merge into one.

Per item that is one decider of what, then the architect for how; today a concern's need is decided twice (the
concern decider, then the steward). Two deciders writing one list can still fire one act twice. The AC change of
2026-10-04 showed it: the shadow day decider moved its AC entry to 8:30 within 8 seconds of the owner's message;
the live brain opened a concern and corrected the 9 PM work ten minutes later. With both live, that is two work
objects. The protections:

1. **Both see the whole list**: every entry of the day from either decider, with its status. An act already on the
   list is covered by pointing at that entry, never by writing another.
2. **The other's entries count as new input.** A new or changed entry from the concern side runs day_tasks again,
   and the reverse; a decider's own entries do not count, so a run never triggers itself. So "we're eating out
   tonight" (a concern and its work) makes day_tasks drop the 6 PM family-time heads-up.
3. **A report on an entry's work runs its decider again.** A failed or unanswered task is new information; whether
   to try again or let it go is the decision remade. A change in its own decisions alone still does not.
4. **Chat is read when the conversation is finished.** day_tasks reads chat by the gate's rule (a room's messages
   once the owner has been quiet in it for `brain_inbox.QUIET`), so it never acts on half a conversation ("8:30…
   actually leave it at 9") and both deciders act on the same finished one.
5. **One order in the brain's pass**: the concern side first (brain step, `concern_tasks`), then day_tasks, which
   sees the entries the concern side made in that pass. The AC case: the concern's entry modifies the AC entry by
   id, and day_tasks sees it done and keeps it; one work object, revised once.
6. **Done outside the list.** An act done in chat (switchboard → `emi_team`) is seen only by its words until chat
   reports what it did as a record (work-engine note, build step 1); something the owner did himself only through
   what he said in chat or a ticket reply; the cron reminders through the reminder history in the situation.

### What the assistant does with each calendar item

Approving a belief for the calendar today carries no instruction. The morning review would also ask what the
assistant does with it: **remind** (when), **do it** (the act), or **just know it** (context for the day, no act).
That answer is the item's standing instruction; the brain's daily decision is only whether today needs it. B455
would be "just know it", or at most one reminder at 5 PM.

### Who acts, and why it belongs to the brain

The concern decider's prompt says "you say what should be done, never how or by whom", which made sense while the
steward interpreted its bullets. Replayed straight to the architect (2026-10-04,
`scratch/replay/brain_feeds_architect.py`), the brain's outcome "Complete Integrated Math IIIA LT 4 Practice by
October 9" became, in all three runs, a plan for the assistant to do a child's homework: nobody can tell from
that line whose job it is. The brain knows (whose school, the owner's own words), so the rule becomes "never
how": each entry names who acts and, for someone else's job, the assistant's part and when.

### What travels with a task

Maximum content, never a summary (owner, 2026-10-04: "The concern has to travel with it maximum content"; "it has
to list the email it came from all the details that went into the task ask"):
- everything the brain saw when it decided: the concern's record and journal (times local), the work on it with
  its progress, and everything it looked up (calendar, email, todos, work);
- every source email and message the concern's evidence names, verbatim, with its sender, subject and the local
  time it arrived.

### Constraints travel, and the architect adjusts the ask

Owner, 2026-10-04: the architect must get the most context possible, so it knows what is happening and why it is
being asked, and makes common-sense adjustments when the ask is odd in a way the brain cannot know: "brain might not
know all subagent capabilities so architect needs to figure out its own way to solve the issue." His example:
"[a child] needs school supplies", without "by September 9", gets a plan for slow delivery that arrives September 12.

1. **The entry carries its constraints, not only its outcome.** "The child has her school supplies" is incomplete;
   "by September 9" is part of the outcome. Each entry states its deadline and every hard constraint (a budget, a
   place, whose account) in its outcome and done-when, which become the work object's objective and success
   criteria; the finalizer then judges a delivery on the 12th as not achieved. The brain's job; the replay suite
   checks it.
2. **The architect reads the sources, not only the entry.** When the brain leaves a constraint out, it is still in
   the school's email, which travels verbatim with the work. The architect gets the entry, its why, the concern's
   whole record, the sources and the brain's situation (section 2: calendar, away from the keyboard, health and the
   rest), and plans to every constraint it finds in them. Today it gets the steward's one-line objective and a
   short rationale.
3. **The architect adjusts the ask with common sense, and says so.** The brain knows the assistant's abilities
   only roughly; how is the architect's:
   - **Same outcome, a better way.** An ask that is odd for the tools at hand gets another route to the same
     outcome (pickup or a store's stock instead of shipping that arrives too late). No permission needed: this is
     the freedom to change tack ("When the work changes course", section 3).
   - **The outcome is not reachable as asked.** It plans the nearest reachable part and reports to the brain by the
     entry's id ("cannot guarantee delivery by September 9; told the owner the options"), never quietly settling for
     less.
   - **The entry and its sources disagree** (the entry says the 12th, the email says the 9th): the source wins, and
     the architect reports the difference to the brain.

   The architect's prompt today tells it to fold the owner's replies in and not second-guess them, and says nothing
   about checking an ask's constraints against its sources. This becomes a new section of that prompt, written with
   the capability and safety section that moves in from the steward (build step 4).

### Replay, round 2 (2026-10-04)

`scratch/replay/brain_decides_with_whom.py` re-ran five recorded act_now decider calls with the "who acts" rule,
then gave the architect the brain's whole input and the sources verbatim (2 runs each):

| Case | Brain before | Brain with "who acts" | Architect |
|---|---|---|---|
| Workshop | act now: resolve the quiz conflict | no action: the owner chose to handle it | nothing to plan |
| Canvas, first | "Complete Math LT 4 by Oct 9" and others, no who | the assistant tells the owner the deadlines, asks him to confirm they are his son's, reminds him to check each | tell, ask, reminders the evening before each deadline; no homework plan |
| Canvas, after "Handle myself" | "Have the study guide submitted…" | the assistant tells the owner what is unconfirmed and leaves it to him | one message, no repeated clarification |
| Reflections | get the decision; ensure it is submitted | the owner asks his son today; the assistant reminds him of the form and deadline if his son enters | 2 steps instead of 4–6 that drafted and submitted the entry for him |

(The Ring case is not counted: the replay's template carried that day's completed notice.) Round 1, without "who
acts" and with the brief only, over-reached on Canvas in every run and planned the Reflections submission for
the owner's son.

### Replay of a day's calendar (2026-10-04)

`scratch/replay/brain_decides_the_day.py`: at 07:30 local, just after the owner told chat "I will start the
timesheets at 5PM or so", a prototype brain decided each of the 31 calendar placements dayflow received that day,
given the owner's todo list, his chat so far, the work in motion that did not come from the calendar, and the open
concerns (3 runs each).

- **Timesheets** (B455, all day and 5 PM): no task in every run, against dayflow's todo work, its spiral and a 5 PM
  heads-up.
- **Without the steward's purpose** the brain treated "family time at 6" as context and dropped the heads-up: the
  context-switch reminders the steward existed to give (owner: "we are losing context reminders"). With the
  steward's sections 7, 9 and 10 in its prompt (`--stewardship`), the 6:00 PM heads-up came back in every run,
  covering the dinner and family-time placements; the bedtime items became one 10:30 PM reminder ("stop coding,
  use the bathroom, and leave the computer for bed"); the timesheets item still made no task. So the purpose moves
  to the brain; calendar beliefs do not need to become concerns.
- **Open for the owner**: the 10 PM liquid cutoff (the brain dropped it in 2 of 3 runs: "his usual last glass
  around 9 PM is already before the 10 PM cutoff"), the combined bedtime reminder, the schoolwork item.
- **Found in the input**: the day pass placed the same belief several times as it re-ran (31 placements for about
  20 beliefs: lights after bedtime three times, timesheets and bathroom twice); some runs left items undecided, so
  the real agent checks that every item is answered (one correction round).

## 3. Delegation and interruption, on existing plumbing

| Order | Today | With this design |
|---|---|---|
| Delegate | `subconscious/concern_handoff.py` writes an intake item; the steward judges it; its persist node (`work_persist.persist_steward_output`) creates the goal-only work object; the architect lays its graph in the same tick | the list-item intake node (below) creates the shell from the entry by code; the architect lays its graph in the same tick, as today |
| Cancel | the steward can ask the architect to end a work object (`end_requests` → `instruct_goal`) | the intake node issues the same `instruct_goal` to the work object carrying the entry id, with the brain's reason; the architect ends the work |
| Modify | the architect can revise the objective (`revise_objective`) | the intake node revises the work object carrying the entry id (`revise_goal`, as the steward's persist does today) and flags it for re-planning; the architect re-plans |
| Report | every judgment and ending reaches the brain inbox (`concern_feedback`) | the same receipts, routed by entry id to the entry, not by meaning |

### The list-item intake node

A control node in the planning tick, in place of intake triage, the steward and its persist node:
list-item intake node (Python) → architect → state mover → … as today. (As built, 2026-10-05: it was added after
the steward's persist node, and nothing was removed from the state map; with the steward retired, triage and the
steward's prep node still run and the prep node routes straight to it. See "The steward's retirement, behind a
switch".) It judges nothing; the brain's list already
says what changed. Each change on the list (`brain_list_changes`) is typed `created`, `modified` or `cancelled`
and names the entry's stable id (a calendar entry keyed by code, `cal:<day>:<belief>`; a concern entry by the id
it got at creation). The handoff carries the kind of change with the id, and the node acts on the kind:

- **created**: build the shell from the entry, as `persist_steward_output` builds one today from the steward's
  spec, with no model call. Objective and title = the entry's outcome; success criteria = its done-when;
  `source_intake` = the entry and what it serves (the concern, or the belief and its placement), with the source
  emails and messages; `concern_refs` / `belief_refs` copied by code as `work_intake` does; the entry id, so reports,
  cancels and modifications route by it. The architect then lays the graph (its create mode).
- **modified**: find the work object carrying the entry id; revise its objective and success criteria; flag it for
  re-planning. No shell is built; the architect re-plans (its re-plan mode).
- **cancelled**: find the work object carrying the entry id and record an end instruction with the brain's reason
  (`instruct_goal`); the architect ends it.

The architect judges one case itself: a new entry that overlaps work that did not come from the list (a chat act,
work running from before the cutover). It sees the portfolio and ends the new shell (`end_goal`) with the reason
("covered by work_X"), which reaches the brain as a report on the entry. That costs an abandoned shell, in the rare
overlap case only.

Where care is needed:

1. **The brain modifies instead of re-creating.** A `new` entry for something already on the list under other
   words becomes a second work object, faithfully. That is the brain's duplicate risk now; the replay suite guards
   it (the Ring repeat handoff came out "keep" in both runs: one case).
2. **A modified entry whose work object already ended** (the AC moved from 9 PM to 8:30 after the 9 PM work
   closed) goes to the brain as a report, never to new work by itself. An entry whose work is done is closed; it
   runs again only by an explicit decision to do it again, with the reason. Otherwise a reworded entry after its
   6 PM heads-up went out would send it twice.
3. **Changes that are not changes.** A changed reason alone is no change (`brain_list.upsert`), so it does not wake
   the architect.
4. **A placement the day pass drops** is not a cancel: a re-run of the day pass that leaves a belief out would
   otherwise end its work in the middle of it. The entry is kept as it is (`brain_list.apply_calendar`); only a
   decision ends an entry. Until 2026-10-05 the code cancelled it, against this note: the clock-in reminder was in
   2 of 9 runs that day. A dropped placement is not offered to day_tasks again, so its entry stands until the
   belief returns to the plan or its work ends.
5. **Timing.** A modification or cancel takes effect between steps; a step already running finishes first
   (`EXECUTION_OWNERSHIP.md`).

A cancel takes effect between steps. A tool call already running finishes or times out; the execution-ownership
rule revokes further calls and does not kill threads (`EXECUTION_OWNERSHIP.md`). The brain hears what ran and
what did not.

### When the work changes course

The owner (2026-10-04): "I would not want the brain to be overactive in over ruling [the architect] when [it]
changes tacts as long as it is still serving overall goal."

- **The architect keeps its freedom to change tack**, rewording the objective included (`revise_objective`, from
  `dayflow_goal_ownership_2026-09-30.md`), whenever a judgment shows a better route to the same outcome. It needs
  no permission.
- **The brain is told, not asked.** Today an objective change sends no receipt (the work store's receipts are
  attached, judged, done and abandoned; `work_objects/concern_outbox.py`), so the brain learns of it only at the
  next judgment. A `revised` receipt carries the new objective and the reason to the item, by its id. It is
  information, not a checkpoint.
- **The brain steps in only when the work no longer serves the item's outcome or the concern behind it** (the
  first replay's homework plan, where "the owner knows the deadlines" became "do the homework"), never because it
  would have done it differently. At each report its question is "is this still serving?", and the default is to
  leave the work alone.
- **New facts are not overruling.** When something changes the outcome itself (the owner said he would handle it,
  the event was cancelled), the brain modifies or cancels the item by id: its own job on new information, not a
  correction of dayflow's plan.

Drift inside a step that is already running is out of reach: the brain's checkpoints are the step boundaries,
which is when the reports arrive.

## 4. What changes, per part

| Part | Today | Then |
|---|---|---|
| Brain | concerns, notes, the concern planner's plans | also the list; decides concern pieces and today's calendar items; issues cancel and modify by id |
| Concern planner | writes to-dos; hands over to the steward | becomes `concern_tasks`: its lookups, then one decider writes the concern's entries (section 2, "Two task deciders, one list") |
| Day decider (shadow) | decides today's placements, recorded only | becomes `day_tasks`, reading the brain's view of the day |
| Day pass / occurrence handoffs | each placement handed to the steward by code | placements go to the brain's decision, not to dayflow |
| Steward | decides what work exists; reviews intake; maps the owner's feedback to work; keeps work within capabilities; invents acts | retires (below) |
| Planning tick | intake triage → steward → its persist node → architect → … | the list-item intake node → architect → … (section 3); as built: intake triage → the steward's prep node → the list-item intake node → architect → …, the steward skipped by its switch |
| Architect, workers, finalizer | as now | as now; reports carry the task id |
| Morning review | which beliefs go on the calendar | also what the assistant does with each |

### The steward retires

Retiring means unwiring, not deleting (owner, 2026-10-04: keep the agent "for a while because its prompt and schema
are valuable to have for reference"). When no lane uses it, the steward leaves the planning tick's state map, and
`agents/dayflow_orchestrator/strategic_planner_wo/` (config, prompts, form) and its prep and persist nodes stay on disk,
unwired, as `work_repair` did after 2026-09-16. (As built, 2026-10-06: the steward was retired by
`configs/brain_lanes.json` `steward.mode` `retired` (d02e72e2), not by leaving the state map; the map is unchanged
and the steward's prep node skips it.)

Each of the steward's jobs (`strategic_planner_wo`, `prompts/system.j2`) has a new home:

| The steward's job today | Where it goes |
|---|---|
| Decide whether work should exist; review intake as no action or deferred, with a reason (`intake_review.py`) | the brain's list |
| Avoid duplicates by reading the portfolio | the brain's list: each item is decided once, against its own entries |
| Map the owner's feedback to existing work ("request its end or CHANGE it") | the brain's cancel and modify by task id |
| Check for missing high-impact information | the architect, which can already plan a step that asks the owner |
| Stewardship and when to interrupt (sections 7 and 10 of its prompt): help the day go well, reduce friction, protect focus, **smooth transitions**, prevent likely misses, support routines; a heads-up belongs on the boundary ("family time is starting" at 6:00 PM); interrupt for an imminent context switch, an important reminder or deadline; the smallest helpful action, including none | **the brain**, which decides what the day needs |
| The owner's standing directives (`resource_orchestrator_user_prefs`, section 9): the executor capabilities, the notification and device-control rules, the escalation policy, the personal overlay (wellness nudges are separate cron reminders, not to be duplicated) | **the brain** (and the architect, which already reads them since 2026-09-18) |
| Capability and safety limits (section 5 of its prompt): work only within real system capabilities; out of bounds unless the owner asks (enrolling, forms, purchases and subscriptions, sign-ups, choosing doctors or financial products, submitting official documents, unknown portals, personal details); "reduce it to the nearest safe reachable action: summarize, notify, draft, or ask the user"; the assistant is the automation | **the architect**, as a shared partial both read during the transition (the architect's prompt has none of it today) |

The steward's capability judgment is real work. On 2026-10-04 the brain asked for "the doorbell battery is
charged" and the steward wrote "physically removing and charging it is not an available tool action; this work
is a notification". With the steward gone, the architect does that translation as part of planning how. When an
outcome is not reachable at all, the architect says so as a report to the brain ("the assistant cannot charge a
battery; it can notify and check"), never by quietly substituting a different goal. The brain decides what to do
with that, and gets a short summary of the assistant's capabilities (the same partial, abbreviated) so it rarely
asks for what cannot be done; the detail stays with the architect and the workers, who know their tools.

The summary also says what the assistant can sense, since a wait needs a signal (owner, 2026-10-04):
- **Away from the keyboard (AFK)**, usable as a bedtime signal at night ("if i go afk around 10pm+ then i guess i am
  in bed"); not reliable during a fullscreen game (the AFK blind spot found 2026-09-17).
- The bedroom camera is left out until it is in use.

AFK changes become an input like the camera's: they reach the brain's inbox, and a step waiting on "inactive after
bedtime" can be woken by them. Today event waits match only intake (emails, pods), so "turn off the lights after
he has gone to bed" (B488, whose own wording names the signal) waited on a condition nothing could report and was
abandoned twice on 2026-10-04, "bedtime could not be confirmed". The work-engine note lists sensor events as an
open gap; this is its daily case.

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

## 6. The day's record and the day's schedule, without loops (owner, 2026-10-05)

**The loop found on 2026-10-05.** The day pass writer placed the monthly timesheet beliefs on Monday, October 5
(the 3rd business day). The daily context tracker read the day plan (under "Routine (day-specific)") and, at
9:48 AM, wrote into its day theme "monthly timesheets are due by 5 PM", then at 10:06 "on this first business
day of the month"; nothing else in its prompt mentioned either. It carries its previous output forward, so the
sentence stayed all day, and the writer, which read the day theme, cited it over the computed date facts. The
same theme reached the master chat gate, the steward, the architect and the brain's situation. Fixed that day:
the tracker no longer reads the plan, the writer no longer reads the theme (bug list, 2026-10-05).

**The rule.** No reader takes back, as evidence, something it produced itself, and no model interprets the
brain's decisions into statements about the day.

**Two layers:**

1. **The day's record: evidence only** (`resource_expected_calendar`, unchanged in name and meaning). The
   tracker starts from Google Calendar and adjusts it from chat, tickets and the AFK and desktop telemetry: the
   quick trip to the store, the sick child picked up early. Every item carries its source. The tracker never
   reads the brain's plan or list. Its milestones and current status are records too. The brain reads this layer
   (the situation, the day pass, day_tasks) to know what is actually happening.
2. **The day's schedule: the record plus the brain's list, joined by code** (a new resource). The calendar
   beliefs that affect the day come from day_tasks' list entries, the brain's decision, not from the day plan's
   placements, which are what it decides on. Code adds each open entry at its time, tagged with its entry id
   (`cal:<day>:<belief_key>`), its words as written. Readers that want the general schedule of the day (chat,
   daily insights, dayflow) move to it one at a time, each deliberately; `resource_expected_calendar` keeps its
   meaning (owner, 2026-10-05: "new resource").

The brain never reads layer 2: it has its list directly, so its own decisions never come back to it as facts.
No model stands between the list and the schedule, so "due on the first business day" cannot harden into "today
is the first business day". What really happens still returns as evidence: "submitted the timesheets" in chat
becomes the tracker's milestone, which the brain reads as something that happened.

**The record refeeds by design** (owner, 2026-10-05: "the record refeeds so it can edit it based on evidence"):
each tracker run reads its last schedule back as the baseline and edits it from new evidence. So an item, once
in, is treated as established, and the guard belongs at entry. On 2026-10-05 "Monthly timesheets 3:00 PM -
5:00 PM", source "inferred", entered with nothing behind it while the tracker read the plan, and stayed all day.

**Provenance is an id, checked by code** (owner, 2026-10-05: "provenance is a chat item, belief, what brain said").
Today `source` is free text (`user_chat` names no message, `inferred` no telemetry). Every item in either layer
carries the id of what it rests on:

| Where it came from | Its provenance | Layer |
|---|---|---|
| Google Calendar | `calendar_item_id` (already carried) | record |
| Chat | the message id | record |
| A ticket reply | the ticket id (already carried) | record |
| Telemetry | the AFK or activity segment it rests on | record |
| A belief or the brain's decision | the list entry id (`cal:<day>:<belief_key>`, which names the belief; a concern entry's id names its concern) | schedule |

Code resolves each id before an item enters: an item whose provenance does not resolve to a real event, message,
ticket, segment or list entry is refused, so an item citing nothing cannot enter at all. The prompt rule of
2026-10-05 (an inferred item rests on telemetry; a future item is never inferred) is the interim guard until then.
Beliefs and the brain's decisions keep entering only through the code join into the schedule, never through the
tracker, so the record's provenance stays calendar, chat, ticket and telemetry.

**The day theme moves to the brain** (owner, 2026-10-05: "brain is better for the day theme"). "What is
dominating the day" is interpretation, the brain's job; the tracker keeps the records. Each reader of the theme
(the master chat gate, the steward while it lives, the architect, cron tickets, vibe check, the entertainment
advisor, the conversation starter, the Slack room scope, daily insights, the brain's situation) is moved or
dropped deliberately. A design comes before the code: where the brain writes it, and when.

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
4. Why the day pass places one belief several times in a day, and whether placements become one per belief and
   time before the brain reads them.

## Build order

1. The delegation decision as an agent, replayed on 2026-10-01 to 10-04 against the owner's expected answers
   (concern pieces and one day's calendar replayed, see above), with the steward's purpose and the owner's
   directives in its prompt, "who acts", and every item answered.
2. Shadow mode in the brain's pass, with its table and the daily comparison.
3. "What the assistant does with it" in the morning review.
3a. The situation for every brain decider (section 2, "What every brain decider sees"), and the rename of
   `subconscious::day_decider` to `subconscious::day_tasks`; replayed against the shadow decisions before and after.
3b. day_tasks reads the brain's view of the day (section 2); the list entries track their work by id; calendar
   entries keyed by belief and day; the protections of "Two task deciders, one list". At cutover the concern
   decider and the shadow list decider merge into `concern_tasks`.
4. The architect's adjusting rules (section 2, "Constraints travel, and the architect adjusts the ask") and the
   capability and safety section as a shared partial in the architect's prompt (and abbreviated for the
   brain, with what can be sensed), replayed on recorded architect calls before the first lane cuts over.
   AFK changes as an input (the brain's inbox and event waits). Chat's tracked tasks reach the brain as records,
   before the leaked-marker fix.
5. The calendar lane cut over behind a switch, the list-item intake node creating its work objects; then concern
   handoffs, with cancel and modify by entry id; then pod and camera intake into the brain's inbox. The steward
   retires.
6. The day's schedule as a new resource, joined by code from the record and the brain's list (section 6); its
   readers moved one at a time. Then the day theme moves to the brain, by its own design.

## Contract hardening before the calendar lane's first day (2026-10-05)

An external review of the cutover found the contract between the brain's decisions and dayflow's execution weaker
than its division of roles; verified and fixed (bug list, "Brain → dayflow contract"):
- A closed concern ends its work: its open entries are cancelled on the next brain pass, with its resolution.
- An entry's change reaches its work whatever field changed, and the re-plan it asks for is durable: a `replan`
  instruction on the work object, committed with the revised objective, consumed by the architect's revision.
- Every re-plan reads what the goal was made from (the brief), not only the objective line.
- A decision counts as made only once its entries are on the list (calendar run, concern plan).
- On a live calendar day the day's tasks see calendar-made work: it is the brain's own.
Still open: one decision context across the list (a concern's decider sees only its own entries), a changed
placement as citable evidence, and a recovery path for failed matters and plans.

## The steward's retirement, behind a switch (2026-10-05)

`configs/brain_lanes.json` `steward.mode`: `on` | `retired`, read every planning tick. Retired, the steward's prep node
keeps what the rest of the tick reads (pending concern and belief feedback delivered, the portfolio, recent work and
reminder history for the architect), skips the steward's own context and model call, hands the architect an empty
steward result and routes straight to the list-item intake node. Admitted intake left with no reviewer is logged as an
ERROR every tick (0 pending at preparation). Owner: switch after the calendar lane's first live day (2026-10-06) is
observed. (Switched 2026-10-06: `steward.mode` `retired`, d02e72e2.) What the steward still did that nothing replaces: noticing duplicate goals across the portfolio and asking
for their end; the brain's deciders do not yet share one view of the list (review finding 5).
Owner's principle, same day: all input flows to the brain, the brain decides, consumers carry it out, every part of the
brain learns the outcome; no part duplicates work or misses that work became moot. Remaining departures: the steward
(this switch), dayflow reading chat and email as wake context itself, dayflow_cron_tickets deciding wellness reminders,
intake triage (the steward's front door; idle once nothing reaches it; it still runs in every planning tick).

## The day's tasks' own entries (2026-10-06)

On the calendar lane's first live day the decider, able only to decide belief placements, put acts no belief asked
for into unrelated beliefs' entries three times (a clock-in check into the encouragement belief, a backup-dinner
question into family time, then family time "not needed" because its own work "covers" it). Owner: the decider should
notice the moment as the steward did ("ah, time to send a reminder, a context switch is coming up").
- Own entries (kind `day`, id `day:<day>:<hex>`, brain_list.apply_day_own): what today needs that no item asks for,
  each citing what prompted it: a schedule line (K), chat (M), work (W), a reminder (R), a concern (C) or its entry (E),
  the brain's decision (B), the standing directives (D). The calendar lane delivers them by their day.
- A change or a cancel of an own entry cites what shows it; since 2026-10-08 nothing requires the cited fact to be
  newer than the entry (day_tasks.own_problems).
- Work in the situation is marked with the entry it was made from (day_tasks.mark_list_work), so a decider never takes
  an entry's own work for a separate fact that covers it.
Next (owner, 2026-10-06): AFK/presence transitions as brain events, which an own entry can cite.

## Presence at the computer is a brain event (2026-10-07)

Owner, 2026-10-06: "Noticing AFK change is the brains job since its an event. Something needs to reason about this
afk status change, figure out what to update." Owner, 2026-10-07: an absence of 10 minutes or more is the event.
- brain_inbox.ingest_presence reads the AFK monitor's keyboard spells (afk_db active_segments; an absence is the gap
  between two): `away` once the latest spell ended PRESENCE_AWAY or more ago (at its end), `back` when a spell began
  PRESENCE_AWAY or more after the last one (at its start, with the minutes away). Idempotent on the spell id; never
  reaches before the first run's hour, so the past day's history does not arrive as news.
- The brain wakes for both: the AFK monitor's afk_state_changed ("active") pokes it on a return, and
  brain_inbox.next_presence_due sets the wake for the moment an absence reaches PRESENCE_AWAY.
- The gate routes presence like a camera sighting (to concerns whose timing it bears on, new_matter, or none), the
  brain records what it changes about a concern's timing; neither treats it as saying where the owner went.
- Shorter absences show only in the situation's presence line, which the day's tasks (`subconscious::day_tasks`)
  read; the gate, the brain step and the concern planner do not read the situation.
Known limit: the gate routes against concerns, not the day's placements; the day's tasks read presence from the
situation, so an absence bearing only on a placement (bedtime lights-off) reaches it there, not as a matter.
