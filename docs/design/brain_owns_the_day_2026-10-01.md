# The brain owns the day: concerns, owners and routes (design, 2026-10-01)

Status: DESIGN for owner review. No code. Built from the owner's decisions in the 2026-10-01 session and
the evidence gathered in it. Extends the three-layer model of 2026-09-29 (subconscious notices, brain
knows and recommends, dayflow acts) and `dayflow_goal_ownership_2026-09-30.md`.

## Summary

**The brain is a hub.** Every source reports into it: chat, email, calendar, weather, health, and the
outcome of every work object. It keeps the concerns, the standing picture of what matters, and decides
what each one needs. Each need goes out to the agent that solves it: dayflow, the meal planner, beliefs, a
conversation with the owner. What those agents do, and what the owner tells them, comes back and changes
the concerns. The concerns are the shared state everything revolves around; the brain alone decides about
them, and everything else feeds them or acts for them.

Today two pipelines build almost the same picture of the day and act on it blind to each other: the
routine stage (beliefs placed against the calendar) and the brain (concerns from chat, email and work).
Most work dayflow does has no owner, so its outcome reaches nothing. This design moves the day's routine
into the brain, sends every item of the day through the concern door, gives every work object exactly one
owner set by code, and lets the brain route each concern to the consumer that acts on it.

## Why: what 2026-10-01 showed

| Observation | Evidence |
|---|---|
| The same matter is owned twice | The routine made a 5 PM flea-medication reminder from the calendar (`work_b75c9dc1f35e`, Oct 1); the brain's concern made its own work (`work_2825529a0430`, Oct 2). Neither knew the other; the dates disagree. |
| Most work has no owner | 294 work objects in 14 days: 16 cite a concern, 72 cite beliefs, about 160 cite nothing. Unowned work's judgments and endings never reach the brain: only work citing a concern produces inbox events (`concern_outbox._queue_concern_feedback` returns when `concern_refs` is empty). |
| The routine's work is owned by citation, which is unreliable | Routine items become work when the steward reads `resource_dayflow_routine.md`; belief links exist only when it cites them (about a third of the time). |
| `addressing` means the wrong thing | The noticer moves concerns into `addressing` on every pass (`persist.py:349`) without attached work: a high-severity sleep concern sits as "handled" on work that ended ten days earlier. |
| Belief feedback records uses as evidence | 62 work outcomes since the Sept 29 cutover became `support`/`qualify` evidence ("the reminder was sent"); none revised or retired a belief. A delivered reminder says the belief was used, not that it is true. |
| Context changes are not events | The brain inbox reads chat, email and work only. Weather, calendar changes and health data reach the brain only by accident. |

Fixed the same day and not repeated here: the architect read every answered ticket as a directive
(607119ea); the stranded prompt called a finished goal "not achieved" (607119ea); the brief acted
minutes after an email instead of waiting for the reply (3d22ec7b).

## Principles (owner decisions, 2026-10-01)

1. **Three layers in the brain**: gather context; say what the concerns are; reason about each concern and
   decide what to do about it. The brain decides and routes. Consumers act and plan their own work.
2. **Everything is a work object.** Habits and matters alike follow the single dayflow path.
3. **Every work object has exactly one owner of record**, set by code when the item is handed over, never
   left to an agent to cite: a concern, the beliefs it applies, or a direct request from the owner. Its
   judgments and ending flow back to that owner.
4. **A concern carries what is attached to it.** Work started from a concern is attached automatically and
   the concern changes as the work changes. The brain does not attach unrelated work after the fact; one
   ownership at creation replaces it.
5. **A concern's headline is stable; its state evolves.** "The dogs need flea medication" stays the title
   while the brief moves through "reminded", "out of medication", "contact the vet first".
6. **`addressing` means a consumer is on it**: at least one delegation is open. Nothing else sets it.
7. **"I'll handle it" is the assistant's own call**: settled, or followed up in a couple of days. No new
   status. No general guidance on follow-up timing until it is a problem.
8. **Consumers get the concern and the outcome wanted, not the method.** Dayflow plans from a concern; the
   meal planner folds a concern into its recommendations. Watch for over-reach before adding constraints.

## The model

### Concerns: two kinds

- **Matter concerns**: something that needs attention until it is settled (a lost library book, a vet
  appointment, the annual physical). One-off day items merge into these or open one. Resolved when settled.
- **Standing concerns**: a recurring part of the owner's life (getting to bed on time, the evening dog
  walk). Recurring habits map to one standing concern, never one per day. Never resolved; each day's
  outcome accumulates on it. This is what lets the nightly "stop coding" outcome reach the sleep concern.

### Two stages

- **Stage A, intake**: each new item (an email, a chat message, a day item) goes through the concern door,
  which already judges duplicates: merged into the concern that covers it, or minted.
- **Stage B, impact**: each new fact is checked against the open concerns it could change, including by
  consequence, not only by mention. A hurricane warning changes the standing "evening dog walk" concern for
  tonight. The gate routes today on direct bearing; this stage asks "what does this change?".

### Routes

The brain's decision for a concern, replacing today's single `act_now` route to dayflow:

| Route | Consumer | Example |
|---|---|---|
| delegate | dayflow (the action arm) | buy a better pillow; ask the vet for more medication |
| delegate | the meal planner | "my eating has been poor" |
| delegate | beliefs | "I just need to go to bed sooner" (the owner's stated intention) |
| converse | a conversation with the owner | an intangible concern (bad sleep) needs understanding first |
| monitor | the back bench | wait for a reply, a date, or a related event |
| settled | none | the brain resolves it |

A concern can carry several open delegations, one per consumer, tracked on it the way `attached_work` is
tracked today. Which consumers exist and what each handles: consumers declare it, concern `domain_tags`
narrow the field, and the brain decides (deterministic proposes, the model decides).

### The day pass: the routine moves into the brain

The routine stage (`pipelines/dayflow/steps/dayflow_routine_stage.py`: belief selection by topic, the
deterministic hour grid, the routine writer) becomes the brain's day pass. It reads the same inputs plus
the open concerns and their attached work, and sorts every item of the day:

- **habit**: a standing behaviour from beliefs at a clock time; owner: its standing concern (or, while
  none exists, the belief ids);
- **matter**: something needing attention; owner: the matter concern it merges into or opens;
- **background**: context only.

A matter a concern already owns appears in the routine only as a reference to that concern. The output
file `resource_dayflow_routine.md` stays (nine agents and two code paths read it as the day overview); each
timed item now carries its owner id.

### Feedback in both directions

- **Work to owner** (exists for concerns): judgments, the owner's replies and the ending reach the owner.
  For beliefs, an outcome that only shows the belief was applied records a **use**, not evidence. Evidence
  is recorded only when the outcome bears on whether the belief is true: the owner contradicted it, the
  result disproved it, or the owner qualified it. Revise and retire stay available.
- **Concern to work** (missing): when a concern changes after its work is planned (stage B), the change
  must reach the attached work, for example as an instruction to the architect to re-plan, the way the
  steward's end and re-plan requests reach it today.

### The conversation mode

For concerns that need understanding before action (bad sleep): the owner's chat is taken over by a brain
proxy that knows the concern deeply, to gather information or make a plan with the owner. It only updates
the concern: what was said, learned and intended is logged on it. Routing happens afterwards, from the
concern's record, by the same routes. Fits the room mode machinery (`mode_manager_overrides`,
`room_binding_session_service`, `room_mode_exit_service`).

## Today versus this design

| Piece | Today | Needed |
|---|---|---|
| Concern door (merge or mint) | Exists, for brain and noticer intake | Also for every day item |
| Work started from a concern is attached | Exists, by code (`work_intake`) | Same for every handoff |
| Routine work owner | Steward citation, about a third | Set by code at handoff |
| Unowned work reaches the brain | No | Not needed once every work object has an owner |
| Standing concerns | No | New concern kind; state never "resolved" |
| Stage B impact | Gate routes direct bearing; chat, email, work only | Consequence reasoning; weather, calendar and health as events |
| Routes | `act_now` to dayflow | delegate (several consumers), converse, monitor, settled |
| Concern to attached work | No | Re-plan instruction to the architect |
| `addressing` | Set by the noticer at will | Derived from open delegations |
| Belief feedback | Every outcome as evidence | Uses by default; evidence only when it bears on truth |
| Conversation mode | No | Brain proxy in a chat mode |

## Staged plan

Each step works on its own and keeps the system running.

1. **Move the producer.** The routine stage becomes the brain's day pass with the same inputs and the same
   output file, plus the open concerns in view. Verify: the file's readers are unchanged; a replay of a
   recorded day gives an equivalent routine.
   **1a DONE 2026-10-01** (owner: "the day with its beliefs lives in a database, you render it with a
   standard function"): `subconscious/day_pass.py` runs as the last stage of every brain run, with a
   10-minute heartbeat on the wake; the plan is stored per slot (`subconscious/day_plan.py`) and
   `resource_dayflow_routine` is rendered from it on every read; the writer returns guidance per slot by
   the slot's own key. The markdown file, the window re-trim and its regex are gone.
   **1b DONE 2026-10-02**: the writer reads the open concerns (what each is, what settles it, the
   brain's decision, its own work in progress); a matter a concern owns appears as a pointer to it
   (`[concern:<id>]`, `concern_ids` on the line), never as a reminder or action of its own. Live replay:
   the briefing no longer carries its own "schedule the annual physical" prompt while that concern holds.
   **Replaced 2026-10-02**: the per-slot writer filed 9 PM items under 9 AM in about one call in ten
   (`bug_list_2026-09-18.md`). The writer now only places beliefs, `(belief_key, time)`; code builds the
   day and renders each belief's statement. The concern pointers go with it: concerns become the owners
   of the day's items in step 2 (`day_items_have_owners_2026-10-02.md`).
2. **Owners for every item.** Day items pass through the concern door; standing concerns are introduced;
   the routine file shows each item's owner. Verify: the Oct 1 flea case yields one owner, and the date
   conflict reaches the owner as a question.
3. **Hand over by code.** Timed items reach dayflow as intake carrying their owner, like concern handoffs;
   the steward stops making owner-less work from routine lines. Belief feedback switches to uses. Verify:
   every new work object has an owner; outcomes land on it.
4. **Stage B and the reverse path.** Weather, calendar changes and health become brain events; impact
   reasoning updates affected concerns; a changed concern re-plans its attached work. Verify: a recorded
   storm warning changes tonight's dog-walk work.
5. **Routes and the conversation mode.** Consumer declarations, several delegations per concern, and the
   brain proxy in chat.

Independent of the plan and ready now: `addressing` follows attached work only (the noticer stops setting
it; concerns stuck in `addressing` return to `active`), recorded in `bug_list_2026-09-18.md`.

## Open questions

1. **Day-pass cadence**: on calendar changes, mornings and belief changes, or also on brain events?
2. **Cost**: every day item through the concern door is a model call; repeats of yesterday's items could
   match deterministically by the concern they mapped to. Measure the routine stage plus the overlapping
   brain work before and after.
3. **Standing concern state**: a name and rules for a concern that is never resolved.
4. **Consumer declarations**: push from the brain, subscriptions, or the proposed combination; what the
   meal planner's inbound path is.
5. **Belief uses after the cutover**: where uses are stored in the intake store, and whether evidence
   counts feed a belief's weight (if so, 62 delivery records have inflated beliefs).
6. **The noticer's role** once the brain sorts the day and does impact: keep only the long view and
   propose, as agreed on 2026-09-29; retire its `pending_questions` once the conversation mode exists.
7. **Build order**: proposed above as context, owners, handover, impact, then routes and conversation.
