# Dayflow end-to-end logic review — 2026-09-19

## Subsequent provenance repair

Following the user's ownership clarification, DF3 and DF23 are fixed locally; DF20's
missing full directive is fixed while its separate success-criteria persistence issue
remains open. The code now distinguishes schedulable main tasks from worker execution
provenance at orchestration boundaries. Takeover workers/finalizers receive full history;
architect/steward receive finalizer summaries. No storage migration or data rewriting.
The Dayflow suite passes 371 tests, including 11 new provenance cases. The original
review below describes findings before that repair; other release blockers remain.

## Conclusion

The main execution chain has been read from ingestion through finalization and recovery.
It is not ready for publication. Passing component tests did not establish that the
boundaries compose correctly. Several concrete source-level defects can lose intake,
duplicate external work, lose an answer, or strand a completed tool result.

This pass changed review records only. No production code, schema, live data, commit or
push was changed by this review. Earlier local repairs remain present. Findings are
source-confirmed unless explicitly marked suspected; concurrency/failure scenarios below
were reasoned from the code, not exercised against a running user installation.

## Actual execution chain

| Stage | Owner and output | Important boundary |
| --- | --- | --- |
| Schedule | DayflowScheduler serializes tick/wake managers | Dispatch workers/finalizers run concurrently with that gate |
| Ingest | Chat, email, delegation and pods become intake rows | Source cursors and destination writes are separate |
| Triage/enrich | New intake becomes admitted artifacts, optional context | Admission is durable; this tick's evaluator inbox is transient |
| Evaluate | strategic_planner_wo creates/changes/closes goals | Intake transfer, goal writes and decomposition are separate |
| Architect | Adds/prunes nodes, then edges, then wakes | No atomic publication of the complete plan |
| Promote/select | State mover promotes, materializer lists, selector chooses one | Scans include worker-owned descendants; readiness can become stale |
| Dispatch | Switchboard routes, dispatch writes dispatched, session starts | Same-status write is not an exclusive claim |
| Execute | Dispatch manager builds arguments and blocks on tool/worker/ticket | Other planning passes continue throughout this call |
| Record | ToolResult is turned into evidence, pod and status | Multiple writes; normal caller ignores rejection return |
| Judge | WorkFinalizer judges the top-level result | No attempt fence or durable retry of missing judgment |
| Revise/close | Finalizer closes or leaves architect instructions | Pending instruction does not block execution; acknowledgement unversioned |
| Wake/recover | Time wake runs a smaller manager; boot reconnects asks | Holds not rearmed immediately; ticket category/attempt binding mismatch |

## Worker ownership clarification

The user confirmed that descendants created by workers are internal helpers for the
assigned main node. They are not independent architect/steward work units. The
orchestrator must exclude them from its dispatch candidates even though they share
the graph storage representation. DF3 concerns failure to enforce that scheduling
boundary; it does not propose adding parent-completion dependencies to helpers.

## Most consequential failure scenarios

1. **Duplicate worker execution (DF3 / LIVE 3):** a worker creates a proposed child and
   enters a slow sub-manager call. Another planning tick promotes that child, selects it,
   and starts a second manager on the same work.
2. **Lost judgment (DF4 / WO1):** the tool result commits as done; the process crashes or
   finalizer fails. Neither dispatched-only recovery nor the sweeper picks it up. A
   separate race allows an old finalizer to change a newer attempt.
3. **Broken ask recovery (DF1/DF12):** a normal ticket has a composer category other than
   work_notify, so boot never selects it. For selected tickets, node identity without
   dispatch incarnation can bind an old reply to a successor.
4. **Incomplete delivery (DF2/DF20):** ticket composition loses the node lookup handle;
   finalizer later receives the node title but not its detailed instruction. Both stages
   lack information their contracts assume they have.
5. **Execution before revision (DF5–DF7/DF22):** a retry is proposed while its required
   replan is deferred or fails; promotion runs it anyway. Partial DAG writes can expose
   steps before their gates exist, and existing actionable nodes skip new dependency gates.
6. **Lost input (DF9/DF15–DF17):** admission can outlive its transient evaluator handoff;
   cursor writes can precede destination persistence; the tick later overwrites current
   status with its old snapshot. These are separate defects and can mask one another.

## Repair order

1. Fix deterministic context/binding defects: ticket graph reference, durable ask identity
   and recovery selection, complete finalizer directive, and durable response reconciliation.
2. Establish one ownership/attempt contract across claim, nested execution, result commit,
   finalization, timeout and recovery. Resolve LIVE 3's held decision explicitly; do not
   assume that locking planning also locks workers or that epoch-checking one write is enough.
3. Make pending adjudication/replan recoverable and non-dispatchable as appropriate;
   validate and publish graph deltas coherently; acknowledge exact instruction versions.
4. Repair durable intake handoff and cursor/status updates; then wake rescheduling and
   stop semantics. Keep uncertain policy questions separate from confirmed wiring defects.
5. Verify these invariants with focused interleaving/crash tests and an isolated complete
   request → work → delivery → reply → finalization → restart exercise. Retain the broader
   install/upgrade/release gates in release_readiness_2026-09-19.md.

## Reading coverage and limits

Read the three active Dayflow manager state maps; scheduler/cadence/status; ingestion and
chat cursor path; triage, enrichment, evaluator preparation/persistence; architect and
DAG application; promotion, materialization, selection/routing, dispatch and wake nodes;
session execution and ask reconnection; tool caller/shared dispatch seam; ticket creation,
formatting/waiting; manager node handoff and worker checklist reconciliation; finalizer,
portfolio/result projection; work sweeper; active planner/architect/state-mover/selector/
switchboard/finalizer prompts, forms and configs as needed to check their data contracts.
The work model/store/recorder and shared manager runtime were also read in preceding
passes; this pass followed the relevant mutation and exit paths across those seams.

This is the core orchestrator lifecycle review, not an exhaustive review of every tool,
every email/calendar adapter, all transport/ticket endpoints, or the whole KG/belief
implementation. No live LLM/side-effect run, fresh install or upgrade was done here.

## Documentation and comment mismatches to fix with the corresponding repairs

- AGENTS/CLAUDE describe asks as dispatched + user_reply and reliable boot reconnection;
  the normal producer does not establish that complete contract (DF1).
- Dispatch config/interface claim exclusive ownership and no child race (LIVE 3/DF3).
- Finalizer says nothing else can change while it judges, and promises a node-goal judgment
  despite omitting full node content (WO1/DF20).
- Architect says per-object failure leaves an object as-is; separate commits contradict
  that claim (DF6).
- Materializer comments imply current readiness filtering, but it tests status only (DF22).
- Precise wake wording omits held-wake rescheduling and stop gaps (DF13/DF14).
- Status legend calls failed a finalizer judgment, although tool recording and the sweeper
  can write it before any judgment. It also describes proposed as awaiting architect
  approval, but promotion has no such acknowledgement check (DF5).

Every discovered issue, including lower-confidence follow-ups, is recorded in
`bug_list_2026-09-18.md` under the end-to-end review section. That is the repair ledger;
this report explains how the failures connect.
