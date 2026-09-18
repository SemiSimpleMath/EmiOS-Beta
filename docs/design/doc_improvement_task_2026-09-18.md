# Documentation improvement — task brief (2026-09-18)

Three levels, in this order of value: **(2) agent-facing architecture docs → (1) code comments →
(3) GitHub developer docs.** Each level is its own commit series. Nothing here changes behaviour.

## Ground rules — read before touching anything

1. **Docs only.** No edits to `.py` logic, `.j2` prompts, `.yaml` config, or tests. Prompts are
   behaviour, not documentation, even when they read like prose. At level 1 the diff must contain
   comment/docstring lines only — check with `git diff` before every commit.
2. **Read the whole file before writing about it.** A doc claim about a component is made after
   reading that component end to end, not after a grep. A null grep is not proof of absence.
3. **Every named thing is verified to exist right now** — file, class, function, config key,
   status name, blackboard key. If you cannot find it, the sentence is wrong; fix the sentence, do
   not guess.
4. **No line-number citations, no model names, no "as of <commit>" hashes in docs.** Those are
   the three things that rot fastest (the 2026-09-17 doc inventory found rot correlated exactly
   with them). Dates of decisions are fine ("retired 2026-09-16").
5. **Keep the repo's comment idiom.** Comments here explain WHY and often carry the incident that
   motivated the code ("2026-09-13: a goal grew to 117 nodes…"). Do not flatten those into
   generic prose, and do not add comments that restate WHAT the code does.
6. **Surgical.** Fix what is stale or false. Do not "improve" a paragraph that is correct. Do not
   restructure the docs tree. Never delete a doc — move it under `docs/architecture/archive/`
   with a one-line note at the top saying what superseded it.
7. **No subagent fan-outs.** Do the reading yourself, one file at a time.
8. Commit small (one doc or one module cluster per commit), message says what was stale and
   what the current truth is. End messages with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
9. Finish with a short report: what changed, and a list of claims you could NOT verify (left
   as-is, flagged). An unverified claim left alone beats a confident rewrite.

## Phase 0 — read first, write nothing (this gate is not optional)

Before editing any doc, produce ONE file, `docs/design/structure_map_<date>.md`, from reading —
not from searching. It is reviewed by the owner before Phase 1 starts. It contains:

1. **The reading log.** Every file you read end to end, with its line count. The map below may
   only cite files that appear in this log. A file that was grepped but not read may not be cited.
2. **The structure map.** For each subsystem you will document (dayflow, work objects, managers,
   agents, tools, rooms, scope, routines): entry point → the files it runs through, in call order
   → what it writes (DB tables, resources, blackboard keys) → what it reads. Written as a call
   chain you followed in the code, with the function names.
3. **The claims table** for each doc you intend to touch: `claim in the doc | file:function that
   makes it true or false | verdict (true / stale / false) | evidence (what you read)`. No verdict
   without evidence; "could not find" is a verdict of *unverified*, never of *false*.
4. **Open questions** — anything the code left ambiguous. Ask; do not resolve by guessing.

Why this gate exists: a doc rewrite from a grep-and-guess reading produces confident prose that
is wrong in the specifics, and wrong specifics in an architecture doc cost more than a stale
doc — the next agent trusts them. This repo has a hook (`.claude/hooks/no_reflex_grep.py`) that
blocks bare `grep`; that is a hint about how work is expected to be done here, not an obstacle.
Reading a 1000-line file once is cheaper than twenty greps and a wrong conclusion.

Suggested reading order for dayflow (the subsystem most out of sync with its docs):
`docs/architecture/05_DAYFLOW.md` → `multi_agents/dayflow_orchestrator_manager/config.yaml` →
`dayflow_wake_manager/config.yaml` → `dayflow_dispatch_manager/config.yaml` →
`dayflow_orchestrator/dayflow_scheduler.py` → `dayflow_tick.py` → each control node in state_map
order → `work_objects/model.py` → `work_objects/store.py` → `dayflow_orchestrator/work_session.py`
→ `work_objects/result_recorder.py` → `control_nodes/work_finalizer_node.py`.

## Current truths the docs must reflect (verified 2026-09-18)

These are the recent structural changes most docs have not caught up with:

- **Dayflow has three managers.** `dayflow_orchestrator_manager` (the planning tick: intake →
  steward → architect → state_mover → materializer → action_selector → switchboard → dispatch
  claim → finalize), `dayflow_wake_manager` (a time-wake: `work_node_wake_prep_node` → state_mover
  → `work_node_wake_router_node` → switchboard → dispatch claim; NO planning stage), and
  `dayflow_dispatch_manager` (one claimed node: arguments → tool call → `work_finalizer_node`).
  A manager is a `config.yaml` under `app/assistant/multi_agents/<name>/`, auto-discovered.
- **One pass at a time.** Ticks and wakes both hold `DayflowScheduler._run_gate`.
- **Retired and gone:** `tick_router_node`, `dayflow_orchestrator::plan_mode` (+ its `planning_mode`
  flow), `work_repair` (files remain on disk, unwired), the dayflow ITEM dispatch lane and item
  timers, `fast_tick`. Any doc or comment that says a failed node goes to work_repair is wrong.
- **Finalizer contract:** verdicts `achieved` / `achieved_plan_changes` / `retry` / `unrecoverable`
  (+ `next_step` ∈ stop / new_approach / ask_user), always with an `outcome` account; every
  not-achieved verdict passes through `failed`; repeated failure escalates to `ask_user`
  deterministically (`_REPEAT_FAILURE_LIMIT` on the goal's `goal_unmet_attempts`). Old
  vocabulary (proceed / amend / replan / blocked / resolve) is gone.
- **Time:** the store normalizes every datetime to UTC; every prompt renders local wall-clock with
  the day (`work_portfolio.local_stamp`).
- **Control nodes read the blackboard, never `message.data`.** The manager copies the trigger's
  data onto the blackboard once; activation Messages carry none of it.
- `CLAUDE.md`'s "Dayflow Orchestrator" section still describes the pre-09-16 pipeline
  (work_repair, evaluator → finalizer → architect order, item lane). It is the first thing an
  agent reads; fix it first.

Source of truth for all of the above: `docs/architecture/05_DAYFLOW.md` (kept current through
09-18) and the code. When 05_DAYFLOW and another doc disagree, 05_DAYFLOW wins; when 05_DAYFLOW
and the code disagree, the code wins and 05_DAYFLOW gets fixed.

## Level 2 — architecture docs for agents reading the code base (`docs/architecture/`)

Goal: an agent that reads a doc before touching a subsystem is not misled.

Known-stale (from the 2026-09-17 inventory; re-verify each):
- `CLAUDE.md` Dayflow section (see above).
- `05a_DAYFLOW_ORCHESTRATOR_REFERENCE.md` — its own header says the P2–P8 path enumeration
  describes a pipeline that no longer exists. Rewrite the paths section against 05_DAYFLOW; drop
  P8 (fast tick) and P9 (planning mode) as retired; keep the agent-by-agent reference where still true.
- `08_WORK_OBJECTS.md` — check the ops table and status vocabulary against `work_objects/store.py`
  (`TRANSITIONS`, the ops registered in `apply`) and `work_objects/model.py`.
- `04_CONTROL_NODES.md` — the node list: `tick_router_node` is gone, `work_node_wake_prep_node`
  exists, `plan_mode_final_router_node` is master_room-only now.
- `15_EMI_TEAM_AND_SCOPE.md` and `14b` — flagged FALSE in the inventory; read the code they
  describe and either correct or archive.
- `21_SKILLS.md` — a "substring" claim about skill matching flagged wrong; verify against the
  skill loader.
- `02b` — reserved-key guidance flagged as inverted; verify against the context injector.
- `01_AGENTS.md` step 5 — flagged stale; verify against `agent_factory` / the agent contract.
- `00_OVERVIEW.md` — make sure the three dayflow managers and the retirements above appear.

Method per doc: read the doc end to end; list every concrete claim (file, class, key, path);
verify each; fix in place; add nothing speculative.

## Level 1 — comments and docstrings in code

Goal: no comment points at a component that no longer exists or a flow that no longer runs.

Known-stale references (found by searching for the retired names; re-verify each in context):
- `work_repair` still cited as the thing that adjudicates failed nodes in: `dispatch_sweeper.py`,
  `node_dispatch.py`, `work_session.py`, `work_store.py`, `dayflow_tick.py`,
  `work_node_dispatch_node.py`, `task_runtime/task_runner.py`, `task_runtime/tool_executor.py`,
  `work_objects/model.py`, `work_objects/result_recorder.py`, `work_objects/store.py` (a few
  transition-table comments). The current truth: the finalizer judges failed nodes and routes;
  the architect acts on the route; the steward decides a whole goal.
- Module docstrings that describe the tick as running the tool call inline, or the finalizer as a
  tick stage — it runs in the dispatch room.
- Any comment saying wakes "bypass the `_running` gate" or "never wait on a tick".
- Any comment describing item-lane dispatch, `fast_tick`, or `planning_mode` in dayflow.
- `work_repair_apply.py`, `work_repair_node.py`, the `work_repair` agent dir and
  `relevance_cleaner` are unwired but on disk: give each a two-line header stating it is retired,
  when, and what replaced it. Do not delete.

Method: search for each retired name across `app/` and `work_objects/`, read the surrounding
function, rewrite the comment to the current truth. Comment-only diffs.

## Level 3 — GitHub developer docs (README, docs index, how-to guides)

Goal: a developer who has never seen the repo can run it and extend it.

Deliverables (verify every step by reading the code path it describes):
- `README.md`: what EmiOS is, install/run (setup.py, emi.bat / emi.command, `.venv` rule), where
  the docs are, where to start reading (`00_OVERVIEW`, `05_DAYFLOW`).
- A docs index (`docs/README.md` or the top of `00_OVERVIEW.md`) listing every architecture doc
  with one line each and its status (current / archived).
- **"How to extend EmiOS"** — one page, one section per question, written from the code:
  - add a **tool** (the 4-file tool contract under `app/assistant/lib/tools/<name>/`; a tool under
    an existing manager needs no scope edit)
  - add an **agent** (`app/assistant/agents/<ns>/<name>/` contract: config.yaml, prompts, agent_form)
  - add a **manager** (a config.yaml directory, auto-discovered; to make it callable as a tool from
    a room: one line in the room's allowed tools + a wrapper tool)
  - add a **room** (`app/assistant/rooms/<id>/`: identity, permissions, authority, policy, scope)
  - add a **control node** (class under `app/assistant/control_nodes/`, declared in a manager
    config, read the blackboard not the Message)
  - add a **routine** (`configs/routines.json`, runner types, functions registry)
  - adjust a **prompt** without touching code (prompts hot-reload; `.py` needs a restart)
  Source material: `docs/design/modular_emios_plan.md` (registry survey) and the reachability
  notes in `docs/architecture/SCOPE.md`.
- `CONTRIBUTING.md`: branch policy (main only — copy from CLAUDE.md), tests location and how to
  run them, the "no fallbacks / fail loud / no backwards-compat layers" principles, the pre-commit
  bans (family names, and the assistant's bare first name as a whole word).

## What "done" looks like

- `CLAUDE.md` describes the pipeline that runs today.
- No doc or comment names `work_repair`, `tick_router_node`, `plan_mode`, `fast_tick`, or the item
  dispatch lane as live.
- Every architecture doc has been read end to end once and either fixed or archived.
- A developer page answers each "how do I add X" question with the real file paths.
- The final report lists what was changed and what could not be verified.
