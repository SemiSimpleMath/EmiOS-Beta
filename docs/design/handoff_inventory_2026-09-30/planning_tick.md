# Handoff inventory — planning tick + wake pass (read-only, 2026-09-30)

Scope: `dayflow_orchestrator_manager` and `dayflow_wake_manager` state maps, agents intake_triage,
strategic_planner_wo (steward), work_architect, state_mover, action_selector, switchboard, their
prep/persist/router/materializer/dispatch/wake nodes, shared/work templates, work_context.py,
work_portfolio.py, work_persist.py, work_intake.py, work_architect_apply.py, work_session.py
(up to the dayflow_dispatch_manager handoff). All files listed were read in full except
post_room_finalize_node.py lines 900-1091 (function headers only; the whole item-lane block is
already recorded as unreachable in docs/design/bug_list_2026-09-18.md around line 382-397).

Form legend: RECORD = reader renders the stored record (node / payload / source / ticket row);
PROSE-ONLY = reader gets only the producing LLM's free text; BOTH; UNREAD.

How agent output travels: every agent_form field lands on the manager blackboard under its own
name (AgentResultApplier; docs/architecture/02b_RUNTIME_DATA_CONTRACT.md:13-19). The architect
is invoked directly by WorkArchitectNode with its own blackboard; its output is only what that
node reads off `result.data`.

---------------------------------------------------------------------------------------------

## 1. Field table

### intake_triage (agent_form.py)

| producer.field | written to | read by | form | what the record would carry that prose drops |
|---|---|---|---|---|
| triage_summary | blackboard `triage_summary` | nobody (grep of app/: only the form and test repros) | UNREAD | — (pass summary) |
| artifact_decisions[].artifact_id, .decision | triage_spawn_guard_node.py:33,107-135 -> item `state`=artifact/suppressed, `state_reason`=`triage_<decision>`; persisted triage_persist_node.py:63 (batch) and :86 | steward prep loads admitted intake (strategic_planner_wo_prep_node.py:177-185) | RECORD (state code) | — |
| artifact_decisions[].reason | nowhere — spawn guard reads only decision + uncovered; state_reason is the coded decision (triage_spawn_guard_node.py:134) | nobody | UNREAD | Which existing work a REJECT_DUPLICATE was matched against, why a pod was REJECT_NO_ACTION. A wrong reject is unauditable. |
| artifact_decisions[].uncovered | meta.`triage_uncovered` (triage_spawn_guard_node.py:119-121), persisted with the item (triage_persist_node.py:55-63) | steward user.j2:85-86 | PROSE-ONLY (triage's own words) | Not copied into `constraints.source_intake` (work_intake.intake_source, work_intake.py:103-118 has no such key), so the architect, state_mover, worker and finalizer never see that the source was only partly new. Survives only if the steward restates it in objective/rationale. |

Triage's own input (not an output, but a knowledge gap on this hop): user.j2:37-86 renders only
the legacy item lane (`active_plan_synopses`, items in important_open/actionable/waiting/watching,
recently resolved items; intake_triage_prep_node.py:99-144). No work object, portfolio or work
node reaches triage, yet it decides REJECT_DUPLICATE ("every part already covered by existing
tasks or plans", system.j2:5). CONFIRMED.

### strategic_planner_wo — steward (agent_form.py)

| producer.field | written to | read by | form | record content the prose drops |
|---|---|---|---|---|
| intake_reviews[] (item_id, outcome, reason, reconsider_at) | prepare_reviews (intake_review.py:29-67) -> write_intake_reviews: item `evaluator_review`, `state_reason`=`evaluator_<outcome>:<reason>` (dayflow_item_writer.py:466-471); concern journal via record_intake_outcome (strategic_planner_wo_persist_node.py:99-108) | steward itself next pass for defer (user.j2:76-78); brain via concern journal (SUSPECTED — brain side not traced) | PROSE-ONLY (steward reason) | — (the reason is the record) |
| evaluation_summary | blackboard only | nobody | UNREAD | Already noted in bug_list line ~3523 for the abandon case. Any per-object judgment not expressed in a structured field is lost. |
| new_or_changed[].work_id / objective | create_work_object / revise_goal (work_persist.py:60-69, 78-81); goal.content = shared/work/goal_content.j2 (objective + success criteria + intake summaries + pod ids); title = objective[:80] | architect CREATE task (work_architect_node.py:262-266 uses stored goal.content); portfolio `goal:` for steward/architect (work_context.work_data:249); state_mover `WORK OBJECTIVE` (work_context.state_mover_candidate:308); worker/finalizer (worker.j2:7) | PROSE (steward's restatement) + RECORD of intake summary/pod id | Intake body excerpt (see §3); triage `uncovered`. |
| new_or_changed[].rationale | `constraints.rationale` (work_persist.py:67); `created[].rationale` on blackboard `steward_persist_result` (:75, persist node :63) | architect CREATE only, via architect_context.j2 (work_architect_node.py:260-261; recovery path _undecomposed_goals :47) | PROSE-ONLY | NOT shown on any RE-PLAN: replan passes `information=info` without rationale (work_architect_node.py:292-296) and work_data has no rationale key (work_context.py:249-261). The NATURE brief ("reminder about the user's own activity / work you carry out") is gone for every later revision. |
| new_or_changed[].success_criteria | constraints + goal content (work_persist.py:62-68; work_intake.goal_update:151-155) | portfolio (portfolio.j2:4), worker (worker.j2:8), finalizer | RECORD (stored criteria) | — |
| new_or_changed[].based_on | source_records -> `constraints.source_intake` (+ concern_refs / belief_refs) (work_persist.py:45-68); items closed `converted_to_work_object` (persist node :92-95) | portfolio SOURCE CONTEXT (portfolio.j2:12, compact), state_mover waits (state_mover_prep_node.py:212; user.j2:57), worker/finalizer task_context.j2 (full) | RECORD | Compact view hides `excerpt` (source_context.j2:9) for steward + architect. |
| replan_work_ids | blackboard (persist node :61-62) | WorkArchitectNode :221, merged at :230 | id only — the WHY is UNREAD (no field exists) | The steward's evidence for the replan (which reply, which result) is never given to the architect. For a steward-only flag (no finalizer entry, no end instruction, not stranded) the architect gets objective + graph + the generic situation, nothing saying what changed. |
| user_directed_replan_ids | blackboard | WorkArchitectNode :235-237 -> `licensed` :291 | code flag | Not persisted; also lost if the id falls beyond the replan cap (§4). |
| end_requests[].work_id / reason | `instruct_goal` -> `constraints.architect_instructions` (work_persist.py:88-94; work_objects/store.py:836-846) | architect replan task architect_task.j2:4-7 (reason verbatim); consumed by `consume_goal_instructions` (work_architect_apply.py:266-267) | PROSE-ONLY (steward's reason) for the architect, which also has the graph | After consumption nothing renders the instruction or the architect's answer back to the steward (portfolio.j2 has no instructions block; work_data has no key). See architect_summary. |

### work_architect (agent_form.py; consumer work_architect_node.py + work_architect_apply.py)

| producer.field | written to | read by | form | record content the prose drops |
|---|---|---|---|---|
| architect_summary | nowhere (node reads only nodes/abandon_*/duplicate_of/revise_objective/end_goal, work_architect_node.py:267, 297-308) | nobody | UNREAD | The prompt TELLS the architect to put load-bearing decisions here: "otherwise keep the goal and say in architect_summary why it continues" (shared/work/architect_task.j2:5), "state the blocker in architect_summary instead" (system.j2:189), "prune that contact branch and explain why in architect_summary" (system.j2:224-226). A declined end request leaves no trace; the steward can re-request indefinitely. |
| nodes[].node_id / title / detail | add_node: id `<slug>--<ns>`, title (or detail[:60]), content=detail (work_architect_apply.py:182-196) | materializer -> action_selector (title + content), state_mover (`TASK DIRECTIVE`, `FULL TASK`), wake router -> switchboard, dispatch arguments node `task`/`information` (dayflow_switchboard_arguments_node.py:94-99), worker.j2:5 | RECORD (the stored directive) | — |
| nodes[].depends_on | add_edge depends_on (:198-215) | is_ready; portfolio `DEPENDS ON` (portfolio.j2:22) | RECORD | — |
| nodes[].wake_at / wake_ref | defer_node wake_kind time/event (:221-240) | is_ready; state_mover `waiting for` (state_mover_prep_node.py:207); portfolio `WAIT:` | RECORD | — |
| nodes[].wait_reason | nowhere (apply never reads it) | nobody | UNREAD | Why a node is gated. |
| abandon_node_ids + abandon_reason | set_status abandoned with reason -> payload.terminal.reason (apply :150-159) | portfolio `REASON:` (portfolio.j2:24) | RECORD | — |
| duplicate_of | abandon + redirect_dependencies (apply :136-163, 242-243) | portfolio REASON | RECORD | — |
| revise_objective.objective / success_criteria | revise_goal via goal_update (apply :251-264) | every objective reader | RECORD | — |
| revise_objective.reason | `constraints.objective_revisions[]` (apply :260-263) | nobody (no template or code reads `objective_revisions`) | UNREAD | Why the goal changed; later steward/architect/finalizer see only the new objective. |
| end_goal.status / reason | set_work_status reason `architect: <reason>` (apply :271-277) -> goal payload.terminal.reason | steward ALREADY COMPLETED / RECENTLY DROPPED via _goal_epitaph (strategic_planner_wo_prep_node.py:89-96, 288-292) | RECORD | DONE entries render `finalizers: []` (prep :290) — task judgments are dropped for completed goals, kept for abandoned ones (:117-118). |

### state_mover (agent_form.py; consumer state_mover_persist_node.py)

| producer.field | written to | read by | form | record content the prose drops |
|---|---|---|---|---|
| state_mover_summary | blackboard only | nobody | UNREAD | — |
| node_wakes[].task_id / source_item_id | validated against prepared candidate + exact intake source (persist :381-387) | code | RECORD (ids) | — |
| node_wakes[].evidence | evidence child node `content`, with `payload.external_source` (the full prepared intake record), `matched_condition`, `context_role=external_wake` (persist :389-400) | worker/finalizer via work_context.source_context (work_context.py:208-216) rendered by source_context.j2:11-13 as "wake interpretation (agent judgment)" next to the source summary/excerpt/pod; steward/architect portfolio (compact) | BOTH | Excluded from node_result (work_portfolio.py:54-56), so the finalizer's "ITS FULL RESULT" never contains it — it appears only under source context. |
| held_work_nodes[].task_id / reactivate_at | set_status waiting + defer_node time (or user_reply) (persist :331-350) | is_ready; next state_mover/steward/architect see `WAIT: time | <stamp>` | RECORD (gate only) | — |
| held_work_nodes[].hold_reason | deliberately not stored (comment persist :339-341); log only | nobody | UNREAD | A state_mover hold is indistinguishable from an architect-authored time wake for every later reader (portfolio.j2:23, state_mover user.j2:78). When the wake fires, the wake-pass state_mover cannot see why it held before and can hold again; a wrongly held boundary announcement is invisible to steward/architect. |

### action_selector (agent_form.py; consumer action_selector_router_node -> chat_task_router_node.py)

| producer.field | written to | read by | form | record content the prose drops |
|---|---|---|---|---|
| reasoning | blackboard `reasoning` | only post_room_finalize_node.py:818 inside `_persist_action_selector_action_logs`, which is unreachable (no writer of `action_result_events`/`active_dispatch_records`; bug_list ~382-397) | UNREAD | — |
| acted_on_item_ids | WorkNodeDispatchNode :27-29 -> canonicalized `work_node_ref` (:115-135), then cleared (:36) | code | RECORD (id) | — |
| no_op_tf / handoff_tf | router flags (chat_task_router_node.py:43-116) | code | — | — |
| switchboard_task / switchboard_information | blackboard `task` / `information` (chat_task_router_node.py:96-108) | switchboard user.j2:1-5 — ONLY in the planning tick | PROSE-ONLY | The switchboard's ticket-vs-work decision is made on the selector's rewrite, not the node. NOT forwarded to the dispatch room: open_session passes `delegate_to`, `work_node_ref`, `dispatch_epoch`, task="" information="" (work_session.py:207-217); the arguments node rebuilds task/information from the node (dayflow_switchboard_arguments_node.py:94-99). So the selector's rewrite now affects routing only. |

The action_selector's own input (materializer, work_node_materializer_node.py:69-78): per node only
`summary = title + ". " + content`, id, constant importance "medium". No objective, no
payload.finalizer, no failure_count/epoch, no sources, no wake. It is asked to pick by "urgency +
impact" (system.j2:35-38) with no record that distinguishes a retry from a first attempt.

### switchboard (agent_form.py: reason, delegate_to)

| producer.field | written to | read by | form | record content |
|---|---|---|---|---|
| delegate_to | blackboard -> WorkNodeDispatchNode :26 -> open_session -> dispatch room blackboard + arguments node `action` (work_session.py:207; arguments node :56-73) | code | RECORD (tool name) | — |
| reason | blackboard only | nobody | UNREAD | — |

Switchboard INPUT differs by door:
- planning tick: `task`/`information` = action_selector prose (PROSE-ONLY), chat_task_router_node.py:107-108.
- wake pass: `task` = node.title, `information` = node.content (RECORD), work_node_wake_router_node.py:141-149.
Same node, same agent, different evidence.

### Code-built views agents read (not agent outputs, but where records are or are not rendered)

| view | built at | rendered for | contains | omits |
|---|---|---|---|---|
| work_portfolio (render_portfolio -> portfolio_list.j2/portfolio.j2) | strategic_planner_wo_prep_node.py:231-235 (BEFORE persist/architect) | steward user.j2:96; architect `situation` (work_architect_node.py:191) | objective, success criteria, status counts, execution_state attempts/calls, compact sources, concern briefs, per main task: directive, epoch, failure count, deps, wait, terminal reason, finalizer_summary (verdict/outcome/recommendation/question/next_step), ACTIONS TAKEN | node results/evidence; finalizer `recent_results` receipts; source excerpts; rationale; architect_instructions (pending or consumed); objective_revisions; stranded_review; hold reasons; work_signal (work_signal() exists, work_portfolio.py:145-165, but portfolio.j2 never includes work_signal.j2) |
| _render_existing_graph (= portfolio.j2 of one WO) | work_architect_node.py:176-179 | architect replan `graph` | same as above | same; and system.j2:138-164,189 promises "LIVE ones first ... full detail", an "ALL IDENTICAL" marker and `why:` epitaphs — portfolio.j2 has no ordering, no such marker, and prints `REASON:` |
| _situational_context | work_architect_node.py:182-192 | architect CREATE + REPLAN `information` | ticket responses (response_details records), active tickets, the whole portfolio, recently completed, scheduled reminders | recent_abandoned_work, expected_schedule_view provenance; the portfolio predates this tick's creations |
| state_mover_candidate | work_context.py:305-309 | state_mover READY WORK NODES (tick + wake) | objective, directive, status, epoch, wake, finalizer_summary | sources, concern, node results, hold history |
| actionable_items | work_node_materializer_node.py:69-78 | action_selector | title + content | everything else |

---------------------------------------------------------------------------------------------

## 2. UNREAD fields (written, no downstream reader) — all CONFIRMED by reading the consumer and grepping app/

1. **work_architect.architect_summary** — the prompt routes three decisions into it (keep-goal-despite-end-request, chain-blocker, pruned-contact explanation). Dropped at work_architect_node.py:267/297. HIGH: an end request the architect declines is consumed (apply :266-267) and its answer vanishes; the steward sees neither (portfolio.j2) and may re-request every tick.
2. **work_architect.revise_objective.reason** — stored in constraints.objective_revisions (apply :260-263), rendered nowhere.
3. **work_architect.nodes[].wait_reason** — ignored by apply_architect_dag.
4. **state_mover.held_work_nodes[].hold_reason** — intentionally not persisted (persist :339-341). MEDIUM-HIGH (see table).
5. **state_mover.state_mover_summary** — blackboard only.
6. **strategic_planner_wo.evaluation_summary** — blackboard only (already in bug_list ~3523 for the abandon case).
7. **strategic_planner_wo.replan_work_ids — the reason** (there is no field for it; the architect gets the id only).
8. **intake_triage.triage_summary** and **artifact_decisions[].reason**.
9. **action_selector.reasoning** — only reader is in the unreachable item-lane block.
10. **switchboard.reason** — blackboard only.
11. **Blackboard bookkeeping no one reads**: `work_decompose_result`, `work_replan_result` (work_architect_node.py:326-327), `promoted_work_nodes`, `woken_work_nodes` (state_mover_persist_node.py:105,185).

Inputs that are always empty (writer gone) — the reverse problem, a section the steward reads
that cannot carry knowledge:
- `recent_dispatch_results` ("RECENT OUTCOMES", steward user.j2:110-116): built from items with
  `dispatched_to` + `execution_result` (strategic_planner_wo_prep_node.py:136-141); nothing in
  app/, work_objects/ or belief_engine/ writes `dispatched_to`. CONFIRMED (grep).
- `recent_action_selector_actions` ("RECENT NUDGES SENT", user.j2:55-61): `action_log` items are
  written only by post_room's unreachable action-result path (post_room_finalize_node.py:373-414).
  CONFIRMED. Old rows inside the 18h window could still render (SUSPECTED, not checked in DB).

---------------------------------------------------------------------------------------------

## 3. PROSE-ONLY hops, ranked by risk

1. **action_selector -> switchboard (planning tick only)** — decides the ACTION ROUTE (ticket to the
   user vs work_emi_team). The switchboard reads only switchboard_task/information
   (chat_task_router_node.py:107-108; switchboard user.j2). The selector itself sees only
   title+content and is not forbidden from rewording in practice (its prompt says "do not change
   wording", system.j2:26-29, but nothing enforces it). The wake pass gives the same switchboard
   the node record (work_node_wake_router_node.py:148-149). Misroute = a device action ticketed back
   to the user or a reminder sent to a worker. CONFIRMED. Note for the incident: the selector's
   `switchboard_information` does NOT reach the worker in current code (work_session.py:210-217;
   arguments node :94-99) — the worker's missing retry reason is a different break (item 3 in §4).
2. **steward end_requests.reason -> architect end decision** (END OF WORK). The architect decides
   end_goal on the steward's sentence plus the graph (architect_task.j2:4-7). It has the graph
   (finalizer summaries) but not the intake/ticket evidence the steward cited, except via the
   generic situation block. Its answer is UNREAD (architect_summary). CONFIRMED.
3. **finalizer outcome/recommendation -> architect replan (VERDICT -> PLAN)** — by design the
   architect reads only finalizer prose (instructions.j2; finalizer_summary.j2), never node_result,
   receipts or worker provenance (docs + work_architect system.j2:199-220). It can only re-plan as
   well as the finalizer summarized. Same for the steward. CONFIRMED (design, not a bug; listed
   because the thermostat incident's "unrecoverable" read was exactly this hop).
4. **steward rationale -> architect CREATE (decomposition)** — the steward saw the intake body
   excerpt, concern brief_text/why_now, triage `uncovered`; the architect gets objective + success
   criteria + intake SUMMARY + pod id (goal_content.j2) + rationale prose + a portfolio built
   before this goal existed (strategic_planner_wo_prep_node.py:231-235 vs persist/architect
   later), so the new goal's own concern brief and source excerpt are absent. The architect has
   no tools (config allowed_tools: []), so it cannot pod_fetch. CONFIRMED. (DF10 in bug_list
   covered the earlier variant; the pre-creation portfolio remains.)
5. **steward replan flag -> architect replan (no reason)** — id only. CONFIRMED.
6. **triage uncovered -> steward** — triage's prose, then only the steward's restatement goes
   further. CONFIRMED.
7. **state_mover node_wakes.evidence -> worker/finalizer** — BOTH (source record is attached), low
   risk. CONFIRMED.

---------------------------------------------------------------------------------------------

## 4. Records that exist but are NOT rendered to an agent that acts on them

1. `node.payload.finalizer` — NOT in: action_selector (materializer :69-78), switchboard
   (either door), the dispatch arguments payload (dayflow_switchboard_arguments_node.py:96-104),
   and NOT in shared/work/worker.j2 (renders task id/status/title/directive/success_kind,
   checklist, provenance, facts, task index — never `view.task.finalizer`, though task_data
   carries it, work_context.py:224). Rendered in portfolio, state_mover candidates, recent_work
   (abandoned only), finalizer. So a `retry` node the architect keeps unchanged is re-run by a
   worker that never sees RECOMMENDS. Template + arguments CONFIRMED; that work_emi_team's prompt
   is exactly worker.j2 via workobject_render_node.py:32 is SUSPECTED here (dispatch side).
2. Node results / evidence (node_result) and execution receipts `recent_results` — only the
   finalizer (finalizer_input.j2:12-20). Steward/architect see none (design). CONFIRMED.
3. `constraints.source_intake[].excerpt` — hidden by `compact_sources` in portfolio.j2:12 /
   source_context.j2:9 and absent from goal_content.j2:4-6 (summary + pod only) -> steward
   portfolio and architect never see it. CONFIRMED.
4. `constraints.rationale` — not rendered on replan. CONFIRMED.
5. `constraints.architect_instructions` (consumed) and `constraints.stranded_review` — never
   rendered to the steward or to a later architect pass. CONFIRMED.
6. `constraints.objective_revisions` — never rendered. CONFIRMED.
7. Finalizer judgments of DONE goals — `_render_recent_completed` sets `finalizers: []` for done
   (strategic_planner_wo_prep_node.py:288-290); abandoned ones include them (:117-118). The steward
   deciding "do not recreate" sees only the epitaph. CONFIRMED.
8. `recent_abandoned_work` — not in the architect's situation (work_architect_node.py:185-192;
   config comment work_architect/config.yaml:25-30). CONFIRMED.
9. Work objects themselves — not rendered to intake_triage at all (§1). CONFIRMED.
10. `work_signal` (graph growth/depth) — function exists (work_portfolio.py:145-165), no caller in
    the portfolio path (portfolio.j2 does not include work_signal.j2). CONFIRMED by reading
    portfolio.j2; other callers not searched beyond work_portfolio/work_context.

---------------------------------------------------------------------------------------------

## 5. Drops / truncation / summarizing on the path

- Goal title `objective[:80]` (work_persist.py:61; store.py:831 on revise). Readers that use the
  title (`recent_work` titles, ticket titles) see the cut; portfolio `goal:` uses full content. CONFIRMED.
- Node title fallback `detail[:60]` (work_architect_apply.py:193). CONFIRMED.
- Replan cap `[:_MAX_REPLANS_PER_TICK]` = 3 (work_architect_node.py:278). Graph-persisted triggers
  (finalizer, end instruction, stranded) recur next tick; the steward's blackboard-only
  `replan_work_ids` / `user_directed_replan_ids` beyond 3 are DROPPED for good. CONFIRMED.
- Portfolio compact sources drop `excerpt` (portfolio.j2:12). CONFIRMED.
- Intake `email_body_excerpt` is already an excerpt upstream (triage user.j2:19-23 TODO). SUSPECTED
  length not checked.
- Materializer reduces a node to title+content (materializer :69). CONFIRMED.
- DONE log drops finalizers (prep :290). CONFIRMED.
- Time windows: steward ticket replies 12h (prep :324); state_mover ticket responses 2h and chat 6h
  summaries only (state_mover_prep_node.py:20, 38-60); recent work 18h. CONFIRMED.
- Active tickets `limit=50` (prep :28-29); `_build_recent_dispatch_results` `[:10]` (dead input).
- concern_brief.j2:4 shows `concern_id[:8]` (display id; harmless).
- Logging-only slices: triage uncovered[:200] (spawn guard :124).
- Dead helpers `_t`/`_body` with `_TITLE_CHARS`/`_BODY_CHARS` (work_portfolio.py:16-37) — unused.

---------------------------------------------------------------------------------------------

## 6. Side observations (outside the brief, not verified further)

- WorkArchitectNode swallows every exception ("Never raises", work_architect_node.py:272-274,
  319-324) — a failed decomposition leaves an empty goal until _undecomposed_goals retries it.
- ChatTaskRouterNode sends an "On it." ack to the user on every handoff
  (chat_task_router_node.py:111-113); in the dayflow tick that is a user-facing message per
  dispatch, routed by scope_context.reply_to. SUSPECTED — whether the dayflow scope has a
  reply_to (and so whether anything is delivered) was not checked.
- Per CLAUDE.md these should go in docs/design/bug_list_2026-09-18.md; not done (read-only task).
