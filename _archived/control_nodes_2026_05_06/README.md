# Control nodes — archived 2026-05-06

**DO NOT DELETE.** Snapshot of 13 control node files (and 2 dedicated test files) that had no production callers as of the audit date. History preserved via `git mv`; `git log --follow` on any file inside this directory traces back to its original location.

## Why archived

Audit (2026-05-06) of all 70 control nodes against every manager config and Python module showed 13 nodes with **zero production references** — no manager wires them in `control_nodes:` lists or `state_map:` keys, and no other Python module imports them outside their own test files.

## Five thematic groupings

### Group A — Dayflow refactor casualties (4 nodes, ~472 LOC)

Orphaned by the dayflow simplification (commit `8a53d6dd refactor(dayflow): collapse state machine to single source of truth`):

- `cooldown_gate_node.py` (`CooldownGateNode`) — Anti-dup cooldown filter for dayflow items
- `plan_gate_node.py` (`PlanGateNode`) — Compiler-style gate deciding whether `strategic_planner` should run
- `plan_validator_node.py` (`PlanValidatorNode`) — Cross-plan hygiene pass; could request planner rebuild on duplicate evidence
- `pre_room_ingest_node.py` (`PreRoomIngestNode`) — Sweeps stale artifact / needs_planning items to closed; seeds ingestion keys

### Group B — Playwright auto-scan pipeline (5 nodes, ~437 LOC)

A coherent feature ("after each action, auto-snapshot, accumulate notes across turns, fold into final answer on return_control") that lost out to a simpler design. None of the playwright manager's current state_map references these:

- `post_action_scan_node.py` (`PostActionScanNode`) — Auto-snapshot scheduler after action tools
- `playwright_auto_scan_complete_node.py` (`PlaywrightAutoScanCompleteNode`) — Closes the auto-scan cycle, returns control
- `playwright_note_accumulator_node.py` (`PlaywrightNoteAccumulatorNode`) — Accumulates planner notes across turns
- `playwright_return_capture_node.py` (`PlaywrightReturnCaptureNode`) — On return_control, folds accumulated notes into `final_answer_content`
- `playwright_page_overview_node.py` (`PlaywrightPageOverviewNode`) — Standalone "snapshot two viewports' worth of prose" (with detailed CSS-vs-PNG resolution comments)

### Group C — Summary alternatives (2 nodes, ~394 LOC)

The live summary path is `SummaryPreNode` + `SummaryPostNode` (used by 11 and 9 managers respectively). These two were alternatives that didn't win:

- `summary_context_node.py` (`SummaryContextNode`) — Context compaction for long-running manager loops; marks older messages as suppressed, pins high-value ones, adds compact summary that survives suppression
- `maybe_summary_gate.py` (`MaybeSummaryGate`) — Reads `manager_flow_config.gates.summary` policy, decides whether to run a summary node. Also a `Maybe*` smell per `feedback_three_architectural_rules.md`.

### Group D — Item dedupe (1 node, 184 LOC)

- `item_dedupe_node.py` (`ItemDedupeNode`) — Dedupes dayflow items by source content keys (summary, scheduled_start_utc, calendar_status, email_subject, etc.). Same dayflow refactor era as Group A.

### Group E — Task spec tool planner (1 node, 173 LOC)

- `task_tool_planner_node.py` (`TaskToolPlannerNode`) — Read task spec, extract plain-English steps, invoke `master_room::tool_planner` agent, inject recommendations for spec writer. The "tool planner is a separate step" architecture was collapsed in the task-spec redesign.

## Tests archived alongside

These two test files exclusively test archived nodes, so they're moved together to keep history coherent:

- `app/assistant/tests/dayflow/test_item_dedupe_node.py` — tests `ItemDedupeNode` only
- `app/assistant/test/agent_tests/test_summary_context_node.py` — tests both `SummaryContextNode` and `MaybeSummaryGate`

A third file, `app/assistant/test/agent_tests/test_agent_runtime_services.py`, has tests for many live control nodes plus one block testing `MaybeSummaryGate`. That single block (the `test_maybe_summary_gate_routes_to_summary_context_node` function and its import) was scrubbed in the same commit; the rest of that test file stays live.

## Restoration

`git mv` the directories back to their original paths. The archive structure mirrors the source tree (`_archived/control_nodes_2026_05_06/app/assistant/control_nodes/<file>.py`), so restoration is a path-flip. After restore, re-add the `test_maybe_summary_gate_routes_to_summary_context_node` function + import to `test_agent_runtime_services.py` (or check `git log --follow` on that file for the diff).

## Total

13 control node files, ~1,623 LOC, plus 2 dedicated test files. All last touched at the initial public release or in the dayflow refactor that obsoleted them.
