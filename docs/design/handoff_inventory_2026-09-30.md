# Handoff inventory: where agent knowledge is lost (2026-09-30)

Read-only inventory of every dayflow and worker agent's output: where it goes, who reads it, and whether
the reader gets the stored record or only the previous agent's prose. Prompted by the September 30
thermostat trace (`scratch/northstar_ac_2026-09-30.md`), where every break was a reader holding a
retelling instead of the record. No code was changed.

Detail, with a row per output field and file:line for every claim:
- [planning_tick.md](handoff_inventory_2026-09-30/planning_tick.md): triage, steward, architect, state mover, action selector, switchboard, wake pass.
- [dispatch_and_worker.md](handoff_inventory_2026-09-30/dispatch_and_worker.md): dispatch room, worker planner, critic, summary, final answer, receipts, finalizer input.
- [returns_concerns_asks.md](handoff_inventory_2026-09-30/returns_concerns_asks.md): brain, brief, concern and belief feedback, tickets, what steward and architect see of finished work.

Items marked VERIFIED were re-read by hand after the inventory; the rest are as the reports state them
(CONFIRMED there means the path was read end to end; SUSPECTED says what was not checked).

## The shape of the problem

There is one record of what happened on a node: its result evidence, its execution receipts and the
user's replies. **Only the finalizer reads it** (`node_result` has a single caller,
`work_finalizer_node.py:146`, VERIFIED). Every agent after the finalizer — architect, steward, brain,
brief writer, belief outcome agent — sees the finalizer's outcome prose, the goal's end reason, and the
contact list. The architect is told that prose comes from a finalizer "having read the full result"
(`instructions.j2:9`, VERIFIED).

And the record the finalizer reads is itself mostly one prose answer: the node keeps only
`final_answer_answer` (`result_recorder.py:31-41, 77`, VERIFIED), written by an agent that never sees
the planner's findings (`history_formatter.py:13-19` excludes `planner_result`, VERIFIED; final_answer's
context is task, information and recent history).

So knowledge passes through two narrow points: **final_answer → finalizer**, and **finalizer → everyone
after**. A mistake at either point is believed by every later reader, which is the thermostat chain.

## Ranked: hops that decide an action, a verdict or an end of work

1. **The node's result is the final answer's text alone.** Sources, what-was-done, data list, result
   summary and the raw envelope are dropped (`result_recorder.py:61-89`). The `["user_instruction"]`
   citation on the fabricated Nest failure never reached a judge. VERIFIED.
2. **The final answer cannot see the planner's result.** Findings go into evidence nodes and
   `planner_result` messages; neither reaches final_answer. The return_control `action_input` is read by
   nobody, and the two prompts contradict each other about it (final_answer `system.j2:9-11` vs work
   planner `agent_form.py:45`). VERIFIED for the history filter.
3. **Planner findings never appear in "ITS FULL RESULT".** They are written without `dispatch_epoch`
   (`WorkPlanner.py:123-131`, VERIFIED) and `node_result` keeps only current-attempt evidence
   (`work_portfolio.py:57-60`, VERIFIED). They show only in provenance, with no attempt label.
4. **The finalizer's outcome is the only account of a node for everyone after it.** The architect ends
   goals, prunes and writes asks from it; the steward abandons and refuses re-creation from it; the brain
   resolves concerns from it (the "work" evidence on a concern is that prose); the brief writer cites it
   as a sourced `known` fact; on escalation the runtime copies it into the question to the user
   (`store.py:557`). VERIFIED for the architect and node_result; rest per report.
5. **On a retry the worker sees neither the verdict nor the receipts.** `worker.j2` renders no
   `task.finalizer`, failure count or epoch; `execution_state.j2` renders no `recent_results`
   (VERIFIED for the finalizer). This, not the action selector, is why the 07:02 retry looked finished:
   the dispatch room receives an empty task and information and rebuilds both from the node
   (`work_session.py:207-217`, VERIFIED). The northstar trace's break 2 is corrected accordingly.
6. **Receipts lose what was asked and which call it was.** No arguments column; detail cut at 2000
   characters (a JSON envelope can be cut mid-object); every nested call keyed to the main node; only 20
   receipts per work object across all nodes and attempts, so an earlier attempt's applying call can drop
   out of the finalizer's view (`execution_store.py:17-20, 29-30, 113`; `store.py:327-328`).
7. **The architect's answers are thrown away.** `architect_summary` is read by nothing (VERIFIED), yet
   the prompts route three decisions into it: why it keeps a goal against a steward end request, a chain
   blocker, a pruned contact. The end request is then marked consumed, so the steward sees neither the
   request nor the answer and can ask again every pass.
8. **The action selector routes on its own rewrite.** Ticket-vs-worker is decided by the switchboard on
   `switchboard_task`/`switchboard_information` in the planning tick, but on the node record in the wake
   pass (`chat_task_router_node.py:107-108` vs `work_node_wake_router_node.py:148-149`). Same node, same
   agent, different evidence.
9. **The architect plans from prose at creation and on re-plan.** At creation the situation portfolio
   was built before the goal existed, so its own concern brief and attempts are missing; source excerpts
   are hidden; it has no tools to open the pods. On re-plan it gets no steward rationale and no reason for
   a steward flag (`replan_work_ids` is ids only).
10. **A state mover hold leaves no reason.** `hold_reason` is deliberately not stored
    (`state_mover_persist_node.py:339-341`); a hold looks exactly like an architect's time wake, so the
    next state mover can hold again without knowing why.
11. **The summary agent replaces raw tool results in the planner's history** with a mini model's
    compression or hides them; `pin_ids` protects nothing; its prompt wrongly says the finalizer reads
    what survives.
12. **User replies reach the brain, brief and work agents as one lossy line** (`_reply_line`: newest
    entry only, no meaning or scope); the belief outcome agent sees only the single newest reply in the
    work object.

## Records that exist and are shown to nobody who acts on them

Concern `success_criteria`, judgment `recommendation` and `owner_reply` (stored on the concern, not
rendered); `objective_revisions` reasons; `wait_reason`; consumed `architect_instructions` and
`stranded_review`; the full tool results (temp files, pruned after 72 h / 500 files); finalizer
judgments of completed goals in the steward's done log; triage's `uncovered` note after conversion; the
brief's `why` after conversion; `worker_data['dependencies']` (computed, rendered by neither template).

## Fields written and read by nobody

`architect_summary`, `revise_objective.reason`, `nodes[].wait_reason`, `hold_reason`,
`state_mover_summary`, `evaluation_summary`, `triage_summary`, triage `reason`, action selector
`reasoning`, switchboard `reason`, finalizer `abandon_node_ids`, composer `reasoning`, planner `plan` and
return_control `action_input`, final answer `sources`/`data_list`/`what_was_done`/`result_summary`,
noticer `escalated_concerns` (SUSPECTED).

## Always-empty inputs

The steward's RECENT OUTCOMES (needs `dispatched_to`, which nothing writes) and RECENT NUDGES (written
only by an unreachable path). Triage judges duplicates against the retired item lane and never sees work
objects.

## Other defects found on the way

- The planner history's tool-request and agent-request patterns never match: raw strings with `\\s`
  (`app/assistant/utils/history_formatting.py:171-180`, VERIFIED). Every call in the planner's history is
  labelled with its result type instead of the tool name.
- `worker.j2:9` has mojibake ("â€”") in the checklist header.
- The steward's ticket-reply loader swallows errors and shows "no replies"
  (`context_sources.get_responded_tickets_categorized`).
- `_abandoned_line` says it shows the user's last reply and does not.
- The work planner prompt still describes a `[User replied: …]` block that replies no longer produce
  (SUSPECTED).
- `WorkArchitectNode` swallows every exception; the router may send "On it." per dispatch (SUSPECTED).

## Not covered

Noticer prompts and context builder; the ticket builder's summary nodes; readers of belief evidence
text; whether agent-call audit records keep the unread summaries.
