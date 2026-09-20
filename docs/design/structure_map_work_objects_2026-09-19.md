# Work-object lifecycle and persistence documentation audit — 2026-09-19

## Scope and reading coverage

Code is the source of truth. This pass covers the graph persistence boundary, result
recording, standalone helper and manual editor. It preserves earlier runtime/Dayflow
edits. It does not claim a new full audit of all planner, scheduler or worker internals.

| File | Current lines | Coverage |
|---|---:|---|
| `work_objects/model.py` | 403 | Full file |
| `work_objects/store.py` | 827 | Full file |
| `work_objects/result_recorder.py` | 130 | Full file |
| `work_objects/discharge.py` | 204 | Full file |
| `work_objects/runtime.py` | 59 | Full file |
| `work_objects/runtime_setup.py` | 34 | Full file |
| `work_objects/tools.py` | 167 | Full file |
| `work_objects/work_tools.py` | 305 | Full file |
| `work_objects/ui/blueprint.py` | 229 | Full file |
| `app/assistant/dayflow_orchestrator/work_store.py` | 116 | Full file |
| `app/assistant/dayflow_orchestrator/work_persist.py` | 92 | Full file |
| `docs/architecture/08_WORK_OBJECTS.md` | 465 | Full file |
| `work_objects/README.md` | 116 | Full file |
| `AGENTS.md` | 218 | Full file |
| `CLAUDE.md` | 219 | Mirrored Dayflow section; full guidance read in earlier runtime pass |
| `work_objects/DESIGN.md` | 316 | Status header and opening architecture sections only; body retained as history |
| `work_objects/EMI_TEAM_VS_WORK.md` | 89 | Opening comparison only; historical benchmark not recertified |
| `docs/design/bug_list_2026-09-18.md` | 659 | Prior findings carried from earlier audit; relevant LIVE 1/3 and runtime follow-ups checked |

## Call chain and boundaries

- Dayflow accessor → WorkStore construction → migrations → terminal repair → cached store.
- apply → handler → rollup → structural validation → event insertion + graph persistence.
- discharge_node → require existing claim → capture epoch → context binding → ManagerInterface
  invocation → record_tool_result → return ToolResult. Finalization belongs to its caller.
- recorder → initial status check → optional dispatch hop → optional pod write → fenced status
  write → evidence creation. Each store call commits separately.
- graph tools → WorkGraphTools wrappers → one or more apply calls. The binding supplies
  attribution, not a general subtree permission boundary.
- UI → live-store accessor (broad-exception dev fallback) → read APIs or apply mutators.

## Claims checked

| Old claim | Finding / documentation correction |
|---|---|
| Events are replayable truth | Graph tables hold current state; WO2 |
| Completion is epoch-fenced as a whole | Only recorder status write is fenced; WO1 |
| Dependency DAG / inherited ceilings enforced | Ownership checks and immediate explicit authority only; WO3 |
| All terminal writes require reasons | Specific changed targets and terminal container writes only; WO3 |
| Closure always mirrors the goal | Proposed goal can be abandoned under done container; WO4 |
| drive_work runs ready nodes to completion | Missing claim and finalizer; WO5 |
| UI abandon works / fallback only on missing app | Missing reason and broad exception fallback; WO6/WO7 |
| Deferred event reference blocks generic readiness | is_ready does not inspect wake_ref or wake_kind; WO8 |
| stale is derived / payload is LLM-only | Stored knowledge status / machine-read metadata |

## Verification

Isolated in-memory WorkStore probes confirmed dependency-cycle acceptance, ancestor
authority gap, event-wait readiness, missing generated goal ID in event data, initial
closed status without reason, rejection of reasonless container abandonment, and
done-container/abandoned-goal rollup. The actual recorder functions were AST-extracted
with only a logging stub to avoid application bootstrap; a rejected stale result still
attached its pod. No production database or external tool was used.

Before editing, affected files were saved in scratch/work_object_doc_pass_baseline.json.
Verification compares Python ASTs after removing docstrings. Only SQL comments inside
SCHEMA_SQL are normalized; executed SQLite schemas are also compared. Documentation
links and git diff whitespace checks complete the pass. No runtime repair is included.

## Decisions left for implementation

WO1–WO8 are open in the bug list. Decide whether replay is a product requirement,
how to make result recording atomic, which graph contracts the writer must enforce,
and whether to repair or retire the standalone driver. Worker planner/reconcile internals,
live DB contents, deployment behavior and benchmark claims remain outside this pass.
