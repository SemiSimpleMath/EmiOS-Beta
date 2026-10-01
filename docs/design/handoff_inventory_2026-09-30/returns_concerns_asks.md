# Broken-telephone inventory: concerns, asks, and how outcomes come back

Scope: brain (gate, brain, brief, plus some noticer), concern handoff and feedback, belief work_outcome, asks and tickets, and how the steward and architect see work that has finished or failed.
Repo state: main @ ae49e4d3 with an uncommitted working tree. This was a read-only pass, so nothing was written to `docs/design/bug_list_2026-09-18.md`. The findings below need recording there.

Legend for "form":
- **RECORD**: the reader gets the stored underlying record.
- **PROSE-ONLY**: the reader gets only another LLM's retelling.
- **BOTH**: the reader gets the retelling and the record.
- **UNREAD**: no agent or code reads the field.

Status: **C** = CONFIRMED (I read the path end to end). **S** = SUSPECTED (what was not verified is stated).

---

## 0. The answer to the central question

**Does anything after the finalizer see evidence rather than the finalizer's outcome prose?**

- **Worker results and tool receipts: no.** `node_result()` (work_portfolio.py:40) has exactly one caller, the finalizer itself (work_finalizer_node.py:146). The full owned provenance (`worker_data`, work_context.py:85-124) reaches only a worker or finalizer of the same task.
  - The architect, steward, brain, brief writer and belief work_outcome agent never receive an evidence node's content, a call receipt's `detail`, or `execution.recent_results`.
  - Their entire view of "what happened" is `payload.finalizer.outcome` / `.recommendation`, the goal epitaph `terminal.reason`, and `wo.actions` (channel, target, title, "sent").
  - The finalizer's own prompt says so: "the architect and steward see your summary, not these internal records" (work_finalizer/prompts/system.j2:59-64). The steward's prompt says "Judge main tasks from their finalizer summaries" (strategic_planner_wo/prompts/system.j2:218-219).
  - **C**
- **The user's exact reply: partly.** It travels as a record on the evidence node (`payload.user_reply`, result_recorder.py:66-76 → store.py:595-600) and then forks:
  - Steward and architect: full `response_details.response_history` through `recent_ticket_replies` / `situation.j2`. Only for 12 h (prep:324) and at most 50 tickets (context_sources.py ~822). **C**
  - Concern register (`record_judgment`, persist.py:738-741): the verbatim reply dicts. **C**
  - Brain, brief writer and every work agent's concern brief: one line per reply from `_reply_line` (concern_feedback.py:54-66). It renders only the newest history entry and drops `meaning`, `scope`, `responded_at` and earlier or follow-up entries. **C**
  - Belief work_outcome agent: only the single newest reply in the whole work object (`_last_user_reply`, concern_feedback.py:22-51; work_feedback.py:220-221). **C**
  - Concern "ended" record: `owner_reply` is stored (persist.py:793-795), but `concern.j2:20` does not render it. **C**

---

## 1. Field-by-field table

### 1a. subconscious::gate (feeds the brain)
| producer.field | written to | read by | form | what the record would add |
|---|---|---|---|---|
| decisions[].route | brain_events.route (brain_inbox.py:227-234) | brain_step.pending_events filter (brain_step.py:84-88); `none` rows never reach the brain | n/a (a routing choice) | `route=none` silently keeps an event from the brain. It stays auditable in the table only. **C** |
| decisions[].concerns | brain_events.concern_ids | brain_step._gate_note shows concern *titles* (brain_step.py:112-117) and decides which concerns get the full record (:196-217) | RECORD (concern records chosen by gate) | The other open concerns appear only as one line each (title/done_when, user.j2:25-29). **C** |
| decisions[].reasoning | brain_events.gate_reasoning | nobody agent-side (the brain gets `_gate_note`, not the reasoning) | UNREAD by agents | **C** for brain_step. UI readers not checked: **S** |

### 1b. subconscious::brain (agents/subconscious/brain)
| producer.field | written to | read by | form | what the record would add |
|---|---|---|---|---|
| event_decisions[].decision/reason | brain_matters.decisions (brain_step.py:353-355, 364-371) | routes/brain_debug.py (UI) only | UNREAD by any agent | **C** (grep: brain_step, brain_inbox, brain_debug only) |
| concern_updates[].note (action=note) | concern journal `reinforcement_notes` "[brain] …" (persist.py:226-228) | brain (concern.j2:23 `journal`), brief writer (same template) | BOTH: the note sits beside the concern's evidence list | Journal capped to the newest 10 entries (persist.py:71-80). Older notes are silently replaced by "(N earlier notes archived)". **C** |
| concern_updates[].events | concern.evidence (persist.py:225; `_evidence` brain_step.py:306-311) | brain/brief via `_evidence_text` (brain_step.py:144-151) | RECORD for chat (message re-read) and email (pod re-read) | A **work** event's evidence snippet IS the finalizer prose (`_evidence` :309-310; `_evidence_text` returns the snippet). Work "evidence" on a concern is prose labelled as evidence. Evidence capped at 3 head + 9 tail (persist.py:56-64). **C** |
| concern_updates[].note (action=resolve) | concern.resolution_reason, resolution_evidence, journal "RESOLVED" (persist.py:229-237) | concern_door (dedup of a repeat, concern_door.py:76-77), noticer context_builder.py:656, digest_builder.py:112 | PROSE (+ evidence refs kept) | The brain itself never sees resolved concerns again (build_payload only uses active/addressing, brain_step.py:194). **C** |
| new_concerns[].title/subject/kind/severity/horizon/done_when/notes | concern record via concern_door.apply_admission (persist.py:240) | brain (concern.j2), brief writer, handoff `_brief_text`, briefs_for_refs → every work agent (concern_brief.j2:4-5) | RECORD of the brain's own fields | Title max 120 chars (brain agent_form.py:36). **C** |
| new_concerns[].owner_words | concern.owner_request {words, at, ref} (brain_step.py:345-349); code checks the words are verbatim (:283-285) | concern.j2:4, concern_brief.j2:6 | RECORD (verified verbatim) | **C** |
| new_concerns[].events | concern.evidence | as above | RECORD | Email evidence snippet stored as first line only (brain_step.py:308), but re-read from the pod for brain and brief. **C** |

### 1c. subconscious::brief (agents/subconscious/brief)
| producer.field | written to | read by | form | what the record would add |
|---|---|---|---|---|
| what, why_it_matters, tried, owner_wishes, recommendation, known[], depends_on_it[], open_questions[] | concern.brief (concern_brief.py:211-213; persist.set_concern_brief :260-281) | concern_brief.j2:7-18 → (a) steward via handoff `brief_text` (concern_handoff.py:352-360; steward user.j2:68-71); (b) every work agent through `work_data.concerns` → portfolio.j2:13-14 (steward portfolio, architect replan graph, finalizer task) and task_context.j2:10-13 (finalizer/worker information); (c) reading page (UI) | PROSE-ONLY (third hand: LLM summary of brain notes + finalizer outcomes + steward reasons) | `known[].source` may cite a **work id** (allowed_refs, concern_brief.py:126-132). The "fact" behind that id is only finalizer prose. Validation checks only that the ref string was shown (:135-142, :177-183). A finalizer misjudgment becomes a "known" fact with a source in every later brief. **C** |
| readiness.decision | concern.brief.readiness | concern_handoff.run_handoffs gate (concern_handoff.py:397-414) | decides action | act_now/hold/no_action is made from the brief writer's reading of prose history. **C** |
| readiness.task | handoff item `summary` (concern_handoff.py:370) → steward; then `source_intake.summary` (work_intake.py:11) → goal content "Originating intake" (goal_content.j2:5-6, 14) | steward, architect (create: goal content), all work agents | PROSE-ONLY | **C** |
| readiness.why | item `why_now` (concern_handoff.py:371) | steward only (user.j2:69) | PROSE-ONLY | Not copied into source_intake (work_intake.py:9-21). Lost after conversion. **C** |
| readiness.hold_until | hold_until_utc (concern_brief.py:209-210) | next_hold_at / stale (code) | RECORD (time) | **C** |
| `brief_text` (rendered at handoff) | item metadata | steward only | PROSE (rendered brief + attempts) | Not carried into work constraints (work_intake.py:6-21). The work's agents get the live brief instead, **except the architect on CREATE** (see §3). **C** |

### 1d. Noticer (partial: only the fields that touch concerns)
| producer.field | written to | read by | form | notes |
|---|---|---|---|---|
| reinforced_concerns[].notes / addressing notes / disposition reason | concern journal (persist.py ~339-341, 362-366, 405-409) | brain, brief (journal) | PROSE | Schema caps: notes 300, reasons 300 (noticer agent_form.py:94,112,121,141). **C** (persist); noticer prompt not read in full: **S** |
| resolved_concerns[].reason | resolution_reason (persist.py:381) | concern_door, noticer, digest | PROSE | Refused for owner-requested concerns (persist.py:194-205). **C** |
| escalated_concerns[] | concern.escalation (persist.py:396-401, 448-453) | no reader found by repo grep | UNREAD | **S** (null grep) |
| belief_updates[] | tick log only (persist.py:10, 541-548) | none | UNREAD | **S** |
| pending_questions[].why_asking | not passed to enqueue_question (persist.py `_enqueue_pending_questions`) | none on this path | UNREAD/dropped | Docstring claims it is kept "for audit". Code does not keep it. **C** for this function |
| summary | tick log | none | UNREAD | **S** |

### 1e. strategic_planner_wo (the steward)
| producer.field | written to | read by | form | notes |
|---|---|---|---|---|
| evaluation_summary | nowhere (persist reads only new_or_changed/end_requests/intake_reviews/replan ids, strategic_planner_wo_persist_node.py:25-62) | none | UNREAD | grep of app/**/*.py, .j2, .yaml, .html, .js: only agent_form.py. **S** that no agent-call audit record keeps it |
| new_or_changed[].objective | goal content + constraints.objective (work_persist.py:60-68); title = objective[:80] (:61) | architect (goal content), portfolio.j2:2 (steward/architect/finalizer), concern attach record, belief context, work_links | RECORD of the steward's text | The 80-char title is what brain events and the steward's done/dropped logs show (concern_feedback.py:75, 81; recent_work.j2:1). **C** |
| new_or_changed[].rationale | constraints.rationale (work_persist.py:67) | architect **on CREATE only** (work_architect_node.py:47, 260-261) | PROSE-ONLY | Not rendered on replan (`information=info`, :296) or in portfolio.j2. Not updated on revise (goal_update has no rationale param, work_intake.py:41). **C** |
| new_or_changed[].success_criteria | constraints + goal content (work_persist.py:62-68) | portfolio.j2:4, goal content, belief context (belief_outbox.py:41) | RECORD | Stored on the concern's attached_work (persist.py:707) but **not rendered** to brain, brief or work agents (concern.j2, concern_brief.j2, attempts() concern_brief.py:83-87). **C** |
| new_or_changed[].based_on | source_intake (RECORD of item meta), concern_refs, belief_refs (work_persist.py:45-68) | goal_content.j2, source_context.j2 | RECORD (attributed summaries + pod refs) | **C** |
| intake_reviews[].reason/outcome/reconsider_at | item evaluator_review (write_intake_reviews); concern journal "HANDOFF … the planner reviewed it as no_action: <reason>" (persist_node.py:99-108; concern_handoff.py:424-429) | steward (prior deferral, user.j2:76-78); brief writer (journal); brain (journal) | PROSE-ONLY | The brief prompt makes this reason sticky: "decide act_now again only when something has happened since" (brief system.j2:111-113). **C** |
| replan_work_ids / user_directed_replan_ids | blackboard → architect node (work_architect_node.py:221-237) | code (trigger + prune licence) | n/a | **C** |
| end_requests[].reason | constraints.architect_instructions (store.py:836-846) | architect_task.j2:4-7 | BOTH (the architect also gets the graph) | The architect's answer when it keeps the goal is discarded (see architect_summary). Consumed entries are not rendered to the steward (portfolio.j2 has no architect_instructions), so the steward cannot see that it already asked and was refused. **C** |

### 1f. work_architect
| producer.field | written to | read by | form | notes |
|---|---|---|---|---|
| architect_summary | nowhere (node reads nodes/abandon_*/duplicate_of/revise_objective/end_goal only, work_architect_node.py:267, 297-308) | none | UNREAD | architect_task.j2:5 tells the architect to explain in architect_summary why a goal continues against a steward end request. That explanation is dropped, and `consume_goal_instructions` stamps only `consumed_at` (store.py:848-853). **C** |
| end_goal.reason | goal payload.terminal.reason and constraints.terminal "architect: …" (work_architect_apply.py:271-277; store.py:975-983) | `_goal_epitaph` → steward ALREADY COMPLETED / RECENTLY DROPPED (prep:89-96, 116, 290); brain event "ended … Reason:" (concern_feedback.py:80-82); concern ended.reason (persist.py:793-794) → concern.j2:20, concern_brief.j2:21; work_links ended_because (work_links.py:92) → brain/brief past_work.j2:5; belief context terminal (belief_outbox.py:43) | PROSE-ONLY | An end reason built on a finalizer misjudgment becomes the epitaph that every later steward pass reads as "recorded reason stands" (steward user.j2:106). **C** |
| end_goal.status | wo.status | concern/belief receipts, rollups | RECORD | **C** |
| revise_objective.objective/success_criteria | constraints + goal content (work_architect_apply.py:251-265) | all readers of the objective | RECORD | **C** |
| revise_objective.reason | constraints.objective_revisions (work_architect_apply.py:260-263) | no reader found | UNREAD | **S** (repo grep found only the writer) |
| nodes[].title/detail/depends_on/wake_at/wake_ref | graph nodes, edges, wakes (work_architect_apply.py:180-240) | everyone downstream | RECORD | **C** |
| nodes[].wait_reason | not applied (work_architect_apply.py:217-240 ignores it) | none | UNREAD | **C** |
| abandon_reason | each pruned node's terminal.reason (work_architect_apply.py:150-159) | portfolio.j2:24 "REASON:" (steward, architect, finalizer) | PROSE-ONLY | **C** |
| abandon_node_ids / duplicate_of | node status + code-written reason | code | RECORD | **C** |

### 1g. work_finalizer
| producer.field | written to | read by | form | notes |
|---|---|---|---|---|
| outcome | node.payload.finalizer.outcome (work_finalizer_node.py:175-182) | (1) architect: instructions.j2:9 "OUTCOME (the finalizer, having read the full result)" + portfolio graph; (2) steward: portfolio.j2:25 → finalizer_summary.j2:3; RECENTLY DROPPED (prep:116-119); (3) concern register judgment (concern_outbox.py:58-63 → persist.py:738-741) → brain (concern.j2:15), brief writer, concern_brief.j2:22 (steward handoff and every work agent); (4) brain event text (concern_feedback.py:73-79) and concern "work" evidence (brain_step.py:309-310); (5) work_links outcomes → brain/brief past_work (work_links.py:93-94); (6) belief work_outcome agent (belief_outbox.py:44-46); (7) if escalated: becomes the **question to the user** when question_for_user and recommendation are empty (store.py:557) | PROSE-ONLY for every reader except the user replies listed in §0 | This is the single choke point. No downstream reader can check it against the result text or receipts. **C** |
| verdict / next_step | payload.finalizer | architect trigger (work_architect_node.py:51-79), store status (`finalize_task`), concern judgment, brain event | RECORD of a judgment | **C** |
| recommendation | payload.finalizer | architect (instructions.j2:10; prune licence :139-145), steward (finalizer_summary.j2:4) | PROSE-ONLY | Stored on concern judgments (persist.py:740) but not rendered by concern.j2 or concern_brief.j2. **C** |
| question_for_user | payload.finalizer | architect (instructions.j2:11) → an ask node → switchboard args → ticket_builder composer → user | PROSE (paraphrased again by the composer) | **C** up to the architect. Arguments agent not read: **S** |
| abandon_node_ids | dropped (payload keeps only outcome/recommendation/question_for_user, work_finalizer_node.py:175) | none | UNREAD | **C** |

### 1h. belief_engine::work_outcome
| producer.field | written to | read by | form | notes |
|---|---|---|---|---|
| action / valence / statement | BeliefStore retire/revise/add_evidence (work_feedback.py:161-183) | belief system | n/a | Input was finalizer prose + epitaph + newest reply only (§0). **C** |
| reasoning | evidence text, retire reason, revise reasoning (work_feedback.py:169-181) | belief readers (not traced) | PROSE | **S** for downstream readers |

### 1i. Asks / tickets
| producer.field | written to | read by | form | notes |
|---|---|---|---|---|
| composer.title/message/ticket_kind/response_choices/suggestion_type | ticket row (create_dayflow_ticket.py:279-285, 123-134) | user; reply receipt `question = ticket.message` (contextual_response.py:22); finalizer of the ask node via ticket_response.j2 (title, message, history, meaning) | RECORD of what the user actually saw | **C** |
| composer.reasoning | dropped (create_dayflow_ticket.py:279-285) | none | UNREAD | **C** |
| ticket_builder::planner gathered tool results | manager history → composer `recent_history` | composer | BOTH | The summary agent may compress history once it passes 6000 chars or 20 messages, before the composer writes (ticket_builder_manager/config.yaml:21-32, 102-115). SummaryPreNode not read: **S** |
| user reply (record, not an LLM) | ticket.user_response_parsed.response_history; evidence node payload.user_reply (result_recorder.py:66-76) | see §0 | RECORD → lossy line (`_reply_line`) for brain/brief/work agents | Follow-ups (contextual_response.py:29-55) after the tool call returned stay on the ticket only. No new evidence node was found and concern judgment replies are a snapshot (concern_outbox.py:40-43). **S** (no path found) |

---

## 2. PROSE-ONLY hops, ranked by risk

1. **Finalizer outcome → architect (ends goals, prunes, plans asks).**
   - instructions.j2:9 labels it as having read the full result. The graph the architect gets is portfolio.j2: statuses and finalizer summaries only.
   - It decides end_goal, abandon_node_ids and the ask question. **C**
2. **Finalizer outcome → steward.**
   - Portfolio and RECENTLY DROPPED. The steward is told to judge from finalizer summaries (system.j2:218-219).
   - It decides end_requests, no_action on concern handoffs, and whether to re-create work.
   - This is the thermostat pattern: one outcome sentence ("device access is unavailable") is re-read every pass. **C**
3. **Finalizer outcome → brain event → concern note or resolve.**
   - The brain resolves concerns, the only path to resolution.
   - It sees reply lines (lossy record) and the work's objective, but neither the success_criteria nor the result. **C**
4. **Finalizer outcome + brain journal + steward HANDOFF reasons → brief writer → brief.**
   - The brief is read by the steward, the finalizer, the architect on replan, and workers. Its readiness gates handoffs.
   - It is third-hand prose that passes as sourced facts (`known[].source` = a work id). **C**
5. **Architect end_goal.reason (epitaph) → steward, brain, concern, belief.**
   - It governs re-creation ("that recorded reason stands", steward user.j2:106) and the brain's settle decision. **C**
6. **Steward intake_reviews.reason → concern journal → brief readiness.**
   - A no_action reason resting on a misjudged outcome freezes the concern until "something new" happens. **C**
7. **Brief readiness.task / steward rationale → architect at CREATE.**
   - The architect decomposes without the concern brief or attempts (§3.2). **C**
8. **Finalizer outcome → belief work_outcome.**
   - It decides resolve or revise of a belief. Mitigated by "resolve only on the user's words", but the agent still sees only the newest reply. **C**
9. **Finalizer question_for_user (or outcome via store.py:557) → architect → ask node → composer → user.**
   - The user may be asked a question built from a misjudgment. **C** up to the architect.
10. **Architect abandon_reason → node REASON → later passes.** Medium risk. **C**

---

## 3. Records that exist but are not rendered to an agent that acts on them

1. **Worker results and call receipts after judgment.**
   - Evidence nodes and `execution.recent_results` reach only the finalizer of that node.
   - Not in `work_data`, portfolio.j2, instructions.j2, the `concern_outbox._queue_concern_feedback` judged payload (finalizer + replies only), the belief_outbox payload, or `work_links.work_view`. **C**
2. **Architect CREATE gets no concern brief or attempts.**
   - `work_architect_node.py:257-266`: task = goal content, information = rationale + situation.
   - The situation portfolio was rendered at steward prep (prep:231), before the work object existed. **C**
3. **Steward rationale is not shown on architect replan** (work_architect_node.py:296; portfolio.j2). **C**
4. **The architect's reason for keeping a goal against a steward end request.** `architect_summary` is discarded, and consumed `architect_instructions` are invisible to the steward (portfolio.j2). The steward can re-request every pass. **C**
5. **User replies in the steward's terminal-work logs.**
   - `_abandoned_line` says it includes "the last user reply recorded" (prep:99-112) but renders only the epitaph, finalizer dicts and goal note (prep:116-120).
   - ALREADY COMPLETED renders no finalizers at all (prep:290).
   - Replies reach the steward only through the 12 h ticket window. **C**
6. **Concern attached_work.success_criteria, judgment.recommendation, ended.owner_reply.**
   - All three are stored (persist.py:707, 740, 795) and not rendered by concern.j2 or concern_brief.j2.
   - The brain judges "done-when met?" without the criterion the work was judged against. **C**
7. **objective_revisions history** (work_architect_apply.py:260-263): no reader found. **S**
8. **Reply meaning and scope, and earlier or follow-up history entries.** `_reply_line` (concern_feedback.py:56-66) renders only the last entry's label and words. **C**
9. **The question text in situation.j2 and the steward's TICKET REPLIES.** `response_details.j2` renders typed_text, label, meaning and scope, not `receipt.question`. The row shows only the ticket title. Scope carries the question only for typed answers (contextual_response.py:21). **C**
10. **recent_dispatch_results** (steward "RECENT OUTCOMES"). It needs item metadata `dispatched_to` + `execution_result`, written only by the legacy plan-item path (post_room_finalize_node.py:944-975). It is probably always empty for work objects. **S**

---

## 4. Drops, truncations, caps and summaries on the path

| where | what is lost | status |
|---|---|---|
| persist.py:56-64 `_trim_evidence` | concern evidence beyond the first 3 + last 9 | C |
| persist.py:71-80 `_trim_journal` | journal beyond the newest 10 entries. Brain notes, HANDOFF answers, "USER RESPONSE via …" json and WORK ATTACHED/ENDED lines all compete for the slots. | C |
| persist.py:653 `annotate_concern_answer` | question[:80], answer[:200] | C |
| persist.py:431 accept_chronic | evidence cut to 2 + 2 | C |
| noticer agent_form.py:73,94,112,121,141,154,187,190 | max_length 600/300/400 caps on prose | C (schema) |
| brain agent_form.py:36 | concern title ≤120 | C |
| brain_step.py:308 | stored email evidence snippet = first line only (re-read from pod for brain/brief) | C |
| work_persist.py:61 | WO title = objective[:80]. Used in brain events, done/dropped logs and past-work headings. | C |
| concern_feedback.py:56-58 `_reply_line` | only history[-1]; meaning, scope, timestamps and earlier entries dropped | C |
| concern_feedback.py:22-51 `_last_user_reply` | only the newest reply in the WO → concern ended.owner_reply, belief agent | C |
| concern_feedback.py:80-82 `_event_text` (ending) | the ending event carries only terminal.reason. The `context.tasks` finalizers in the receipt are not rendered. | C |
| prep:324; context_sources ~822 | ticket replies limited to 12 h and 50 tickets | C |
| prep:259-290 | terminal work limited to 18 h; done items carry no finalizers | C |
| prep `_build_recent_dispatch_results` | 6 h, 10 items | C |
| context_sources.py `get_responded_tickets_categorized` | swallows exceptions and returns empty, so the steward sees "no replies" (fail-silent) | C |
| work_links.py:34-40 | similar work k=5, cosine ≥0.36 | C |
| brain_step.py:48 | room history 48 h | C |
| concern_brief.j2:4 | concern id shown as [:8] (display only) | C |
| ticket_builder summary agent | LLM compression of gathered context before the composer | S |
| ResponseChoice (response_choices.py:8-9) | label ≤24 chars / 3 words, scope ≤240 | C |
| format_response_result / format_expiry_result | newlines collapsed; legacy path uses question=title, not message (create_dayflow_ticket.py:334) | C |
| store.py:557 | escalation copies recommendation or **outcome** into question_for_user | C |
| work_architect_node.py:31 | at most 3 replans per tick (deferral, not a drop) | C |
| work_portfolio.py:16-37 `_t`/`_body` (90/400 chars) | probably dead (no callers found) | S |

## 5. Not verified
- Noticer prompts and its context_builder (what it reads of dayflow outcomes) were not read in full.
- The dayflow switchboard arguments agent, which turns an ask node into the ticket brief, was not read.
- SummaryPreNode/SummaryPostNode were not read.
- Readers of belief evidence text were not traced.
- Whether agent-call audit records persist `evaluation_summary` / `architect_summary` was not checked.
