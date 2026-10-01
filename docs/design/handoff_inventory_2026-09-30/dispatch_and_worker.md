# Handoff inventory: the dispatch room and worker execution

Read-only. Every claim cites file:line. CONFIRMED means I read the code path end to end. SUSPECTED means I did not finish verifying it, and the entry says what is missing.
No repository file was edited. The bug list (`docs/design/bug_list_2026-09-18.md`) already records two of these findings:
- "September 23 thermostat failure report fabricated…": the `planner_result` history omission.
- "One failed thermostat call is taken as 'no Nest access'": receipts lack arguments, and the worker view lacks the finalizer judgment.

The remaining findings are new. They were not recorded because this task was read-only.

## 0. The path, in one view (CONFIRMED)

```
work_node_dispatch_node.py:69-99   claim -> open_session(store, work_id, node_id, delegate_to)   [switchboard `reason` not passed]
work_session.py:204-219            dispatch room blackboard: delegate_to, work_node_ref, dispatch_epoch (nothing else)
dayflow_switchboard_arguments_node.py:94-111   args = {task: node.title, information: node.content|wake_ref, work_id, node_id,
                                                trigger_context, valid_hours, wait_timeout_seconds, append_links}
dayflow_tool_caller.py:78 -> _tool_caller_util.execute_dispatch -> _execute_tool:323-333 tool_call(...) receipt
  work_emi_team_manager tool -> ManagerInterface.execute:198-202 -> _run_on_given_node_body:100-115
     set_work_context(main node); task = discharge_task.j2("You have been given this main task… {directive}"), information = ""
     WORK TEAM LOOP: delegator -> workobject_render_node -> work_emi_team::planner -> critic_pre -> (critic) -> tool_caller
        -> tool_result_handler -> tool_return_router -> summary_pre -> (summary) -> render -> planner … 
        planner return_control -> emi_team::final_answer -> manager_exit_node
     MultiAgentManager.handle_exit:697-742 -> ToolResult(content=json.dumps(normalized final_answer), data=normalized + final_answer_raw)
_tool_caller_util._execute_tool:341-349 -> payload = data dict
dayflow_tool_caller._as_tool_result:37-46 -> record_tool_result (result_recorder.py:55-93)
  -> store._op_record_result:573-605: ONE evidence node, content = final_answer_answer ONLY, payload {dispatch_epoch, user_reply?}
work_finalizer_node._judge:141-149 -> work_finalizer agent(task = portfolio, information = finalizer_input.j2)
  -> _apply:165-193 -> store._op_finalize_task:528-571 -> node.payload.finalizer (store.py:797-813)
```

## 1. Row table: every output field of every agent in scope

Form key: **RECORD** means the reader renders the stored record. **PROSE-ONLY** means the reader gets only the producer's retelling. **BOTH** means it gets both. **UNREAD** means no downstream agent or code reads the field.

### 1a. Dispatch room (deterministic producers, included because they choose what the next agents receive)

| producer.field | written to | read by | form | what the record has that the reader loses |
|---|---|---|---|---|
| switchboard.reason (orchestrator) | orchestrator blackboard | not passed into the dispatch room: `open_session` takes only `delegate_to` (work_node_dispatch_node.py:99, work_session.py:207-217) | UNREAD by dispatch (SUSPECTED for other orchestrator readers; I did not check them) | why this tool was chosen for this node |
| SwitchboardArgumentsNode.task / information | blackboard `tool_arguments.arguments` (dayflow_switchboard_arguments_node.py:70-74, 96-104) | tool. `create_dayflow_ticket` uses task and information. `work_emi_team_manager` ignores both and re-renders the node from ids (manager_interface.py:199-202, 109-115) | RECORD (node.title/content) | `node.payload.finalizer`, failure_count and prior evidence are not in the arguments. For a ticket, the question is whatever the architect wrote into `content` (CONFIRMED that the args builder adds nothing else) |
| SwitchboardArgumentsNode.work_id/node_id/trigger_context/append_links | same | ManagerInterface (ids); ticket tool (links, trigger_context) | RECORD (ids) | — |
| DayflowToolCaller → record_tool_result | `store.record_result`: evidence node `content = _answer_text(result)` (result_recorder.py:31-41, 77-89; store.py:595-600); `pod_ref`; `result_error_code`/`result_abort_policy`/`result_epoch`/`result_actor` on payload (store.py:602-605) | finalizer through `node_result` (work_portfolio.py:40-62) and `provenance.j2`; the worker on later attempts through provenance.j2 | PROSE-ONLY for `work_emi_team_manager`: the evidence is `emi_team::final_answer.final_answer_answer` | dropped: `final_answer_sources`, `final_answer_what_was_done`, `final_answer_interesting_info`, `final_answer_data_list`, `result_summary`, and `final_answer_raw` (the structured payload). Only the first pod_ref is kept (result_recorder.py:62-63) |
| execute_dispatch `result`, `tool_result_for_formatter`, `accumulated_pod_references` | dispatch-room blackboard (_tool_caller_util.py:112, 121-142) | only `final_answer_node` in the dispatch room (final_answer_node.py:125-149), whose output is used only when the node is still `dispatched` after the room exits (work_session.py:220-224) | UNREAD in the normal path | — |
| `_persist_tool_result` | unified_log row, `message = content[:2000]`, plus a blackboard message (_tool_caller_util.py:384-424) | SUSPECTED: any reader of the `dayflow_orchestrator` room history. Not verified | — | truncated at 2000 characters (see §4) |
| WorkFinalizerNode → `work_finalizer_result` | blackboard (work_finalizer_node.py:110) | `_finalize_recorded_result` reads it as a boolean (work_session.py:270) | UNREAD as content | — |

### 1b. work_emi_team_manager agents

| producer.field | written to | read by | form | what the record has that the reader loses |
|---|---|---|---|---|
| work_emi_team::planner.what_i_am_thinking | blackboard; `planner_result` message (Planner.py:127-134); UI progress fact | progress_emitter (UI narrator). Not readable by any agent: `planner_result` is outside both history formatters' allowed sets (history_formatting.py:105-111; agent_runtime/services/history_formatter.py:13-19) | UNREAD by agents | — |
| planner.plan | blackboard; `planner_result` message | nobody (same filter) | UNREAD | — |
| planner.checklist[].id/text/status | subtask nodes under the owned node: `tools.add_subtask` and `set_status` (WorkPlanner.py:98-112, 147-172) | the planner's next turn (worker.j2:9-13 shows id, status and title); summary_pre's `checklist` reads the blackboard delta, not the graph (summary_pre_node.py:128); the finalizer through provenance.j2 | RECORD | — |
| planner.checklist[].evidence | the subtask's `content`, written on a done/waiting/abandoned transition (WorkPlanner.py:154-170) | provenance.j2:5-6 (worker on later turns and attempts; finalizer) | RECORD of the planner's own prose (no tool evidence attached) | the tool result the evidence claims to rest on |
| planner.findings[] | evidence nodes under `active_attribution_node` with **no `dispatch_epoch` and no title** (WorkPlanner.py:118-131) | finalizer: provenance.j2 only. **`node_result` excludes them** because it keeps only evidence whose `payload.dispatch_epoch == epoch` (work_portfolio.py:57-60), and record_result always writes such an evidence. Findings under a checklist child are never direct children either. **emi_team::final_answer cannot see them** (only `planner_result` carries them) | Finalizer: RECORD in provenance, unlabeled by attempt. final_answer: none | the findings never reach "ITS FULL RESULT". In provenance, nothing marks which attempt wrote them (provenance.j2 does not render `record.epoch`) |
| planner.info_for_others[] | root evidence "fact" node + `informs` edge to the goal (WorkPlanner.py:136-139, 175-199) | worker.j2:15-17 (RELEVANT INFO) for every worker on this goal. Not rendered in finalizer_input or portfolio | RECORD (planner prose) | — |
| planner.action | blackboard `action`; pending tool (flow_controller.py:63-71) | critic_pre_node, ToolCaller | RECORD (control) | — |
| planner.action_input (tool call) | blackboard; `tool_arguments` via `_generate_tool_args` (Planner.py:345-413) | ToolCaller → tool; `shared::tool_arguments` on the slow path; the critic; a `tool_request` message "Calling tool X with arguments {json}" (tool_caller.py:342-348), which the planner and final_answer later see | RECORD of the arguments within this invocation only | **not persisted**: `work_execution_calls` has no arguments column (execution_store.py:17-20). The arguments disappear when the manager invocation ends |
| planner.action_input (manager call, prose) | shared::tool_arguments maps it to the manager tool's `task` | the sub-manager's planner as `task` (manager_interface.py:212-220) | PROSE-ONLY (the planner's restatement) | the node record and prior results. Partly offset: any tool-enabled sub-agent inside a work context gets the worker projection appended (prompt_builder.py:155-160), but that projection lacks the finalizer judgment and receipts (see §3) |
| planner.action_input (return_control) | blackboard `action_input`; `result = whole result_dict` (flow_controller.py:21-32) | **nobody**. final_answer's `user_context_items` are task, information and recent_history (emi_team/final_answer/config.yaml:13-17). The `result` key is overwritten when final_answer writes `final_answer_*` (manager_exit_node.py:15-31) | UNREAD | final_answer system.j2:9-11 says this text "goes into final_answer_answer verbatim". It never reaches the agent. The work planner's form even says it is "empty for return_control" (agent_form.py:45) |
| emi_team::critic.must_revise_plan | blackboard → CriticPostNode routing (critic_post_node.py:223-236) | runtime routing | RECORD (control) | — |
| critic.critic_diagnosis / actionable_change / tags / confidence | `agent_request` "CRITIC RESULT: {json}" to the planner (critic_post_node.py:268-317); the critic's own `agent_result` audit (Agent create_audit_message) | the planner via recent_history (receiver match, Planner.py:105-114); final_answer via its unfiltered history (agent_result/agent_request are allowed, history_formatter.py:13-19) | PROSE-ONLY. The critic's inputs are `task` (the discharge text), `information` (""), recent_history, and planned call/arguments (critic user.j2). **It sees no work_projection**: the critic's allowed_tools is [], so prompt_builder.py:157 does not append it | the node record, prior attempts, the finalizer verdict |
| work_emi_team::summary.summary_pairs | `metadata.history_summary` on each tool_result message (summary_post_node.py:354-358) | the planner's history replaces the result with the summary (history_formatting.py:239-243, 255-267) | PROSE-ONLY (a mini model's compression replaces the raw tool result for the planner) | the full tool result. The disk copy (tool_result_handler.py:256-273) survives 72h/500 files; the history shows only its id |
| summary.hide_ids / delete_ids | `history_hidden` / `history_deleted` (summary_post_node.py:359-367) | the planner's history drops the entry (history_formatting.py:226-242) | drop | the entire tool result, for the planner |
| summary.unhide_ids | `history_hidden=False` | planner history | — | — |
| summary.pin_ids | `history_pinned` (summary_post_node.py:363-364) | only summary_pre's window rows (summary_pre_node.py:219, 246). The planner's formatter checks `context_pinned`, not `history_pinned` (history_formatting.py:112-117). Pinned rows are still summarizable and hideable | effectively UNREAD | the summary system prompt (system.j2:26) says pinning keeps evidence visible. It does not protect anything |
| summary agent_result audit | message, `context_suppressed` (summary_post_node.py:419-449) | **final_answer still sees it**: the agent_runtime HistoryFormatter ignores context_suppressed/hidden (history_formatter.py:83-88) | PROSE-ONLY | — |
| emi_team::final_answer.final_answer_answer | blackboard → manager_exit → `final_answer` → ToolResult (manager_exit_node.py:15-57; MultiAgentManager.py:697-742) → **the node's only result evidence** (result_recorder.py:77) | **work_finalizer** as "ITS FULL RESULT" (finalizer_input.j2:12-13); the worker on later attempts (provenance.j2) | **PROSE-ONLY, and the highest-risk hop.** The final_answer agent itself sees only `task` + `information` ("") + recent_history (tool_request/tool_result/agent_result/agent_request of this invocation's root scope) | the planner's findings and return_control text; prior attempts' records; receipts. On a turn with no tool call, its history is empty (matches the bug-list live diagnostic `msg_count=1, rendered_len=0`) |
| final_answer.result_summary | envelope (final_answer_normalizer.py:184) → ToolResult data | dropped by record_tool_result | UNREAD downstream | — |
| final_answer.final_answer_sources / data_list / task / what_was_done / interesting_info / detail_level | envelope | dropped by record_tool_result (only the answer text is kept) | UNREAD by finalizer | `final_answer_sources: ["user_instruction"]` (the incident) never reaches a judge, so a fabricated-source signal is lost |
| shared::tool_arguments.(tool form fields) | `tool_arguments` (ToolArguments.py:136, 260-264; Planner.py:382-413) | ToolCaller → tool | PROSE-ONLY. It sees target_name, the tool schema and the planner's raw action_input, nothing else (user.j2:1-15; config `user_context_items`). It is **not used by the dispatch room**: DayflowSwitchboardArgumentsNode builds arguments deterministically | the node, the history, prior results. If `action_input` is empty or prose, the nano model fills required fields from nothing |
| shared::tool_arguments.tool_argument_pairs (agent_form) | — | unused: `_run_llm_with_schema(messages, schema)` passes the tool's form, not this form (ToolArguments.py:102-133) | UNREAD (dead form) | — |

### 1c. Finalizer

| producer.field | written to | read by | form | what the record has that the reader loses |
|---|---|---|---|---|
| work_finalizer.outcome | `payload.finalizer.outcome` (work_finalizer_node.py:175-182; store.py:805-813); also `payload.terminal.reason` (store.py:568-569) | architect via instructions.j2:9 (work_architect_node.py:51-79); steward/architect/finalizer via portfolio.j2:25 → finalizer_summary.j2:3; concern feedback "judged" (work_finalizer_node.py:186-188) | PROSE-ONLY, by design (the architect and steward read the summary) | the result evidence and receipts. Decides replan or end of the goal. **The worker on a retry does not see it** (worker.j2 never renders `view.task.finalizer`, although work_context.py:46 supplies it) |
| work_finalizer.verdict | `payload.finalizer.verdict` → status sequence (store.py:538-571) | store rollup; architect; portfolio | RECORD (enum) | — |
| work_finalizer.next_step | `payload.finalizer.next_step`. The runtime may override it to ask_user (store.py:554-557) | architect (instructions.j2:2-7); `has_pending_revision` (model.py:290-295); rollup hold (store.py:1028-1045) | RECORD (enum) | — |
| work_finalizer.recommendation | `payload.finalizer.recommendation` | architect (instructions.j2:10); finalizer_summary.j2:4 | PROSE-ONLY | as for outcome |
| work_finalizer.question_for_user | `payload.finalizer.question_for_user`. When escalated, it falls back to recommendation or outcome (store.py:557) | architect (instructions.j2:11) → an ask node → the ticket | PROSE-ONLY | the question reaches the user with only the finalizer's prose as grounding |
| work_finalizer.abandon_node_ids | **dropped**: `_apply` copies only outcome/recommendation/question_for_user + verdict/next_step (work_finalizer_node.py:175-176); `_op_set_status` keeps a fixed key set (store.py:805-813) | nobody | UNREAD | — |

## 2. UNREAD fields (written, never read downstream)

1. `work_emi_team::planner.action_input` on return_control. Nothing reads it: final_answer has no `action_input` context item (config.yaml:13-17), and `result` is superseded at exit (manager_exit_node.py:15-31). CONFIRMED.
2. `planner.plan` and `planner.what_i_am_thinking` are never given to any agent; `planner_result` is filtered by both history formatters (history_formatting.py:105-111; history_formatter.py:13-19). Thinking goes only to the UI progress fact. CONFIRMED.
3. `final_answer.final_answer_sources / data_list / what_was_done / interesting_info / result_summary / detail_level` and `final_answer_raw`: record_tool_result keeps only the answer text and the first pod (result_recorder.py:61-89). CONFIRMED.
4. `work_finalizer.abandon_node_ids` (work_finalizer_node.py:175-176). CONFIRMED.
5. `summary.pin_ids` has no protective effect (summary_post_node.py:343-368 does not check pinned; history_formatting.py:112-117 reads `context_pinned`). CONFIRMED.
6. `shared::tool_arguments` agent_form (`tool_argument_pairs`) is never used as the schema. CONFIRMED.
7. Switchboard `reason` is not carried into the dispatch room (work_session.py:207-217). CONFIRMED that it is not carried. SUSPECTED that no other orchestrator stage reads it (not checked).
8. `worker_data()['dependencies']` is computed (work_context.py:120-124) but rendered by neither worker.j2 nor finalizer_input.j2. In the "render" path ManagerInterface passes `information=""` (manager_interface.py:111-115), so upstream task results are not in the worker prompt. They are reachable only through graph-read tools. CONFIRMED for the templates.
9. `task_data.failure_count / terminal / epoch / finalizer` for the worker's own task: supplied (work_context.py:41-53) but not rendered by worker.j2 (lines 2-8). CONFIRMED.
10. `ReturnControlNode` (`note` → `final_answer_content`, return_control_node.py:21-25): not in the state_map path. The planner's return_control goes through FlowController (flow_controller.py:21-47) straight to final_answer, and final_answer does not read `final_answer_content`. SUSPECTED inert in this manager; I did not trace every Delegator route.

## 3. PROSE-ONLY hops, ranked by risk

1. **emi_team::final_answer → work_finalizer (decides the verdict).** The node's result evidence is final_answer's `final_answer_answer` alone (result_recorder.py:77). final_answer is built without the planner's findings or return_control text (history allowed set, history_formatter.py:13-19; config.yaml:13-17), and it never sees the node record (prompt_builder.py:157 appends the projection only for agents with tools). The finalizer reads it as "ITS FULL RESULT" (finalizer_input.j2:12-13), the section its system prompt tells it to judge on (system.j2:52). Provenance and receipts follow below it, but are unlabeled and argument-less. CONFIRMED. This is the 7:02 incident chain.
2. **work_finalizer.outcome/recommendation/next_step/question_for_user → architect / steward (decides replan, ask, or end of goal).** Prose by design (instructions.j2; portfolio.j2:25). The architect's only account of what happened is the finalizer's account. CONFIRMED (reader side read only as far as `_pending_finalizer_instructions`).
3. **work_emi_team::planner action_input (manager call) → sub-manager planner (decides the action).** The sub-manager gets the planner's prose as `task` (manager_interface.py:212-220). Tool-enabled sub-agents also get the worker projection (prompt_builder.py:155-160), but it lacks the finalizer judgment, attempt epochs and receipts. CONFIRMED for the code path. SUSPECTED for each specific sub-manager's prompt (I read only the devices::planner config).
4. **summary → planner (decides the next action and whether work is done).** A gpt-5-mini compression replaces or hides raw tool results in the planner's history (summary_post_node.py:354-367; history_formatting.py:226-243). Its system prompt wrongly says the finalizer "reads what survives here" (summary system.j2:3). The finalizer reads graph evidence, not this history. CONFIRMED.
5. **critic → planner (can force a revision).** The critic sees the discharge task text and history, not the node record (critic config user_context_items; allowed_tools [] → no projection). CONFIRMED.
6. **planner action_input → shared::tool_arguments → tool (decides the call's arguments).** The nano agent sees only the target, the schema and the planner's text (tool_arguments user.j2). CONFIRMED.
7. **planner.findings / checklist evidence → finalizer (provenance).** The planner's own prose claims are stored as RECORD-shaped evidence with no link to the tool result they claim (WorkPlanner.py:123-131). The finalizer cannot tell a claim backed by a call from an unbacked one. The bug list's "accounting records it never wrote" entry is this. CONFIRMED.

## 4. Records that exist but are not kept or not rendered to the agent that acts on them

1. **Tool-call arguments.** They exist on the blackboard (`tool_arguments`) and in the in-memory `tool_request` message (tool_caller.py:342-348). They are not persisted: `work_execution_calls` has columns (id, work_id, node_id, epoch, tool_name, external, state, detail, updated_at) (execution_store.py:17-20). `admit_execution_call` writes `detail=None` (execution_store.py:103-108); `tool_call` writes only the content (execution.py:248-255). So a `get_status: true` read and a set call look alike. CONFIRMED (already in the bug list).
2. **Receipt attribution.** Every nested call is keyed to `owner.main_node_id` (execution_store.py:29-30), including calls made inside sub-managers and child nodes. The finalizer's receipts list cannot say which helper or which checklist item made a call. CONFIRMED.
3. **Receipt window.** `_load` keeps only `LIMIT 20` receipts per **work object**, across all nodes and attempts, newest first (store.py:327-328). On a busy goal, earlier attempts' receipts, including the one that applied a setting, fall out of the finalizer prompt. CONFIRMED.
4. **Receipts not shown to the worker.** worker.j2 includes execution_state.j2 (via task_context.j2:17), which renders only unresolved `attempts` and `calls`, never `recent_results` (execution_state.j2:2-6). On a retry the worker cannot see that the previous attempt's call returned "applied". CONFIRMED.
5. **The finalizer judgment not shown to the worker.** `task_data()` includes `finalizer` (work_context.py:46) but worker.j2:2-8 does not render it, nor terminal, failure_count or epoch. A retried node appears with prior evidence and no verdict, so it reads as finished work. CONFIRMED (already in the bug list).
6. **Attempt labels on provenance.** `record_data` supplies `epoch` (work_context.py:28) but provenance.j2:5-9 does not render it. The worker and the finalizer cannot tell this attempt's evidence from earlier attempts'. CONFIRMED.
7. **Planner findings left out of the judged result.** `node_result` filters to current-epoch evidence (work_portfolio.py:57-60). Planner findings carry no `dispatch_epoch` (WorkPlanner.py:127-130), so they never appear in "ITS FULL RESULT". CONFIRMED.
8. **Full tool results.** `ToolResultHandler` writes the full ToolResult to `uploads/temp/tool_results/tool_result_<id>.json`, keeping 72h and 500 files (tool_result_handler.py:28-113, 256-273). Only the id appears in planner history. Neither the finalizer nor a later attempt has a reference to it, and it is pruned. CONFIRMED.
9. **Nested-manager results.**
   - Non-node-aware managers (e.g. devices_manager) return `ToolResult(content=json.dumps(normalized envelope))` (MultiAgentManager.py:718, 737-742). That becomes one in-memory tool_result message (tool_result_handler.py:283-295), a temp file, and a ≤2000-character receipt. Nothing goes on the graph.
   - Node-aware managers (`work_web_manager`, `work_personal_admin_manager`) get a child node whose evidence is again only `final_answer_answer` (manager_interface.py:166-168 → result_recorder.py:77).
   - CONFIRMED.
10. **Structured user reply.** `evidence.payload.user_reply` (store.py:598-599) is read only by the concern and belief outboxes (concern_outbox.py:42-43, belief_outbox.py:50-52). `record_data` excludes the payload (work_context.py:20-28), so the finalizer and worker see only the answer text. CONFIRMED that it is not rendered. SUSPECTED that nothing is lost, if the ticket tool's answer text already carries the response history (I did not read create_dayflow_ticket).
11. **Prior node state for the ticket path.** The arguments node sends only title and content (dayflow_switchboard_arguments_node.py:94-104), never `payload.finalizer` or prior evidence. CONFIRMED.

## 5. Drops, truncation and compression on the path

1. `execution_store.finish_execution_call`: `detail[:2000]` (execution_store.py:113). A manager's detail is a JSON envelope, so it can be cut mid-JSON. CONFIRMED.
2. `store._load`: receipts `LIMIT 20` per work object (store.py:328). CONFIRMED.
3. `_persist_tool_result`: `json…[:2000]`, `str(result)[:2000]`, `message: content[:2000]` (_tool_caller_util.py:387, 389, 414). These are unified-log and dispatch-room message copies. SUSPECTED that no dayflow agent reads them.
4. `record_tool_result` keeps only `final_answer_answer` plus the first pod_ref (result_recorder.py:61-89). Everything else in the envelope is discarded. CONFIRMED.
5. The summary agent replaces, hides or deletes tool results in the planner's history (cadence 4 actions with ≥30 messages, or a result of 5000+ characters; the last 3 results are protected; config.yaml flow_config.summary; summary_pre_node.py:132-169; summary_post_node.py:297-368). It affects only the planner. final_answer's formatter ignores the metadata and sees raw results. CONFIRMED.
6. `Planner._mint_research_findings` sets `context_suppressed` on tool results after minting (Planner.py:288-300). It is a no-op for the work planner because its form has no `findings_to_pod`. CONFIRMED (already in the bug list as the WorkPlanner pod-docs entry).
7. `ToolResultHandler` replaces content with the `summary` conversion only when content is empty (tool_result_handler.py:212-226). It logs `content_str[:500]` to the trace only. CONFIRMED that this does not drop prompt content.
8. `work_portfolio._t`/`_body` (`[:90]`, `[:400]`, work_portfolio.py:16-37) are defined, but the portfolio now renders through Jinja. SUSPECTED unused in this path; I did not trace all callers.
9. The graceful-exit path replaces any partial result with "No final answer was produced" (graceful_exit_control_node.py:186-233). The findings survive on the graph, but the node's result evidence is the abort text. CONFIRMED.

## 6. Other defects found while reading (not recorded in the bug list: this task was read-only)

- **History tool-name parse never matches.** `history_formatting._parse_tool_request` uses `r"Calling tool\\s+…"` (history_formatting.py:171-175). In a raw string `\\s` is a literal backslash plus `s`, so the pattern never matches. The planner's history therefore labels every call with the result_type and shows the whole sentence as "arguments". A probe rendered `TOOL CALL: {"tool_name": "final_answer", "arguments": "Calling tool devices_manager with arguments {…}"}`. CONFIRMED by a probe run (scratch/re_probe.py in the scratchpad). The arguments are still present; the tool identity is wrong.
- **Stale reply-block instruction (SUSPECTED).** work_emi_team::planner system.j2:84-89 tells the planner to act on a `[User replied: …]` block in its node content. Replies are now recorded as evidence with `payload.user_reply` (store.py:595-600), not appended to content. I did not search for any remaining producer of that block.
- **Encoding defect.** worker.j2:9 contains mojibake "â€”" in the checklist header. CONFIRMED by reading.
- **Contradictory final_answer contract.** final_answer system.j2:3-11 says the planner's return_control action_input is the answer. work_emi_team::planner agent_form.py:45 says action_input is empty for return_control and the result goes in `findings`. The two prompts contradict each other, and neither field reaches final_answer. CONFIRMED.
