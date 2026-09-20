# Unpublished update: release readiness — 2026-09-19

## Status: not cleared for publication

GitHub main was checked directly with ls-remote and matched the local origin/main
tracking ref. Local main is 30 commits ahead, not behind. There were 69 tracked files
changed against published main before this repair batch, plus untracked documentation.
Nothing was pushed or committed during this pass. Existing work was preserved.

The shared user base makes installation, upgrade and existing-data behavior release
requirements. A green subsystem suite is evidence for that subsystem, not release approval.

## Current checkpoint

The live Dayflow claim/result/finalizer/recovery path, durable intake handoff, prompt
projections, scheduler jobs, ticket expiry races and shared-runtime repairs now have
local code changes. 547 combined offline checks passed; subsequent focused scheduler /
projection checks (16) and legacy signal rendering checks (20) also passed.

The sections below preserve earlier review checkpoints. Their test counts, executable
file inventory and then-open blockers are historical. Current per-finding status and
remaining limitations are in bug_list_2026-09-18.md, Latest repair checkpoint.

The requested prompt artifact is examples/dayflow_complex_work_prompts.md, generated
from production templates with synthetic graph data. No live LLM or outbound action was
used. Fresh-install, upgrade and full-app release checks remain outstanding. This is
not a declaration that every ledger finding is fixed or that publication is safe.

## First repair batch

- WO9: reject malformed deferred timestamps before persistence; no schema change or
  live-data repair. Valid offset/UTC/naive timestamps and clearing remain supported.
- WO6: manual object abandonment now supplies the reason required by the store.
- New regressions reproduced four failing cases before repair; four valid-input cases
  already passed. All eight pass after repair as part of the Dayflow suite.

## Verification completed

- Dayflow suite: **360 passed**, one warning.
- Ticket/camera targeted suite: **33 passed**, one warning. Files: test_camera_emergency_escalation,
  test_ticket_response_label, test_ticket_kind_callers_are_valid, test_ticket_kind_enum_unwrap.
- Tests used test DB configuration; the new regression fixture uses an in-memory WorkStore.
- Executable-AST comparison against published main separates Python comments/docstrings
  from behavior. Fifteen production Python files differ in executable AST after this batch:
  post_room_finalize_node, work_node_dispatch_node, work_node_wake_router_node,
  dayflow_item_writer, dispatch_sweeper, work_session, create_dayflow_ticket and its
  tool_forms, camera_dispatcher, scheduler_arbiter_persist, ticket_manager, ticket_service,
  ticket_api, work_objects/store and work_objects/ui/blueprint.
- This inventory does not classify non-Python changes or prove the listed changes correct.
  The prior ask-recovery fix remains part of the unpublished update and Dayflow coverage.

## End-to-end logic review: additional release blockers

The subsequent source review traced the full core Dayflow lifecycle and identified
integration defects beyond the earlier suite coverage. See
`dayflow_end_to_end_review_2026-09-19.md` and the DF entries in
`bug_list_2026-09-18.md`. Release remains blocked: nested work can be dispatched twice,
ticket recovery does not match the normal producer, finalizer failure can strand done
results, and pending/partial replans can still execute. Intake durability and full
instruction/context delivery also need repair. These are source-traced failure paths,
not claimed production incidents or additional executed tests.

The repair order in that review supersedes the narrow first-batch priorities below.
No code or publication action was performed during the review itself.

## Priorities recorded before the end-to-end review

1. WO1: make tool-result recording atomic across incarnation check, pod, status and
   evidence. Cover stale completion, duplicate completion, rollback and concurrent callers.
2. R2 and R5: failed exits and structured-result loss; trace consumers before changing
   envelopes. These may cause callers to act on a misleading successful result.
3. WO7: store initialization must not conceal a live-store failure; define explicit
   standalone/dev behavior without silently switching databases.
4. Triage remaining findings by user impact. Keep intentional/held decisions distinct
   from confirmed defects. WO2 replay design and broader WO3 contract enforcement need
   explicit design work; they are not reasons to combine a redesign with a small repair.

## Required evidence before publishing the combined update

- Review the entire published-main-to-candidate diff, including config, prompts, room
  policy, JavaScript, new/untracked files, dependencies and installer/update behavior.
- Run the broader automated suite safely. Reproduce failures on published main in an
  isolated checkout before classifying them as pre-existing; old failure counts are not
  evidence for the current candidate.
- Exercise a fresh install and an upgrade from published main with disposable data and
  representative existing schemas. Verify startup, migrations, restart and rollback
  expectations. Preserve backups before any real upgrade; code rollback alone is not
  proof that migrated data is reversible.
- Use an isolated configuration for end-to-end checks of master-room request/reply,
  dispatch/finalization, ask reply/dismissal/expiry/restart recovery, routine triggers,
  and the changed camera/ticket paths. Stub outbound actions or use dedicated accounts.
- Review release notes, unresolved known issues and a small opt-in rollout before broad
  distribution. Push only the reviewed candidate after release approval.

No fresh-install, upgrade, full-app smoke, broad-suite baseline comparison or rollback
exercise has been completed by this pass. It is not yet evidence that the large update
is safe for all users.


## Worker provenance repair following the logic review

- DF3: main-task ownership is enforced across scheduling, wake, dispatch, recovery and
  supervision boundaries. Architect references cannot target internal provenance.
- DF23: helper failure no longer counts as a main-goal failure.
- DF20 partial: finalizer receives the full directive and owned provenance. Workers
  taking over see that history; architect/steward see finalizer summaries.
- Agent prompts, agent/developer docs and UI provenance labels now describe that contract.
- Eleven new regressions cover visibility, promotion, direct dispatch rejection, timed
  and event wakes, architect targeting, failure counting and UI labels. The initial five
  tests failed before repair. Dayflow suite: 371 passed, one warning after the repair.
- No schema migration, historical data rewrite, commit or push. Stored inflated failure
  counts are not repaired. Remaining claim/epoch/recovery/intake defects still block release.

## User-authorized publication checkpoint

The user requested commit and push after observing nightly cooling, the dog-walk
notification, and closure of the camera emergency after their response. The final
combined offline regression run passed 562 tests with one dependency-version warning.
The context scanner and state-mover instruction/context repairs are included; those
latest changes still need live verification after restart. Remaining findings in the
bug ledger are not claimed fixed. Fresh-install and upgrade exercises remain unperformed.
Prompt templates contain runtime variables, and the rendered example uses synthetic data.
