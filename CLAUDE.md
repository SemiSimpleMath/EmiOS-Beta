# CLAUDE.md

<!-- MIRROR-START — everything below this line is byte-identical in CLAUDE.md and AGENTS.md -->

Guidance for AI coding agents working in this repository.

> **Mirrored file.** `CLAUDE.md` and `AGENTS.md` carry the same content under both
> conventional names — Claude Code reads `CLAUDE.md`, other agent tools read `AGENTS.md`.
> Edit one, then copy everything below `MIRROR-START` into the other. They are meant to be
> identical below that marker; if they have drifted, the newer file wins.

## Branching policy — READ THIS FIRST

**There is one branch: `main`.** Every commit goes on `main`. Every push goes from `main` to `origin` (EmiOS-Beta).

- DO NOT create `release-vX.Y`, `feature/*`, `bugfix/*`, or any other branch — even temporarily.
- DO NOT switch to a non-main branch to "stage" work. Commit on main.
- Worktrees created by Agent tools (`worktree-agent-*`) are the only acceptable non-main branches; they're auto-managed and don't get pushed.
- If you find yourself on a non-main branch and didn't deliberately create a worktree, switch back to main before doing anything else.

Remotes:
- `origin` → EmiOS-Beta (public Beta repo). `git push` from main goes here.
- `alpha-legacy` → EmiOS-Alpha (legacy private repo). Do not push to it.

A `pre-push` hook at `scripts/git-hooks/pre-push` enforces both rules: (1) only `main` may be pushed; (2) `main` only goes to `origin`. To enable on a fresh clone:
```bash
git config core.hooksPath scripts/git-hooks
```
If the hook fires, you targeted the wrong remote or are on the wrong branch — read the message, don't edit the hook to bypass it.

## Development Environment

- **Python**: 3.10+ via `.venv` at repo root
- **CRITICAL**: Always use `.venv\Scripts\python.exe` (Windows) or `.venv/bin/python` (Mac/Linux), never system Python. System Python is missing project dependencies.
- **Entry point**: `run_flask.py` — starts Flask + SocketIO on `http://localhost:8000`
- **Launch**: `emi.bat` (Windows) / `emi.command` (Mac) / `start.py` (auto-detects venv)
- **First-time setup**: `setup.py` creates venv and installs `requirements.txt`

## Running Tests

All tests live under `app/assistant/tests/` (plural). The old singular `test/` dir
was consolidated 2026-05-08 — do not create new tests under `app/assistant/test/`.
Subsystem subdirs: `agent_tests/`, `tool_tests/`, `dayflow/`, `kg/`, `manager_tests/`,
`non_agent_tests/`, `chat_narrator/`, etc. Throwaway probes go in `/scratch/`
(gitignored), never in a tests dir.

```bash
# Unit tests (pytest) — entire subsystem
.venv\Scripts\python.exe -m pytest app/assistant/tests/agent_tests/

# Single test file
.venv\Scripts\python.exe -m pytest app/assistant/tests/agent_tests/test_foo.py

# Manager integration tests (real KG + LLMs)
.venv\Scripts\python.exe app/assistant/tests/manager_tests/emi_team/emi_team_test.py
```

Standalone test scripts must bootstrap DI before any project imports:
```python
import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.ServiceLocator.service_locator import DI
```

## Recording bugs found during other work

Record every bug encountered while reading, documenting, or changing code in
`docs/design/bug_list_2026-09-18.md`, even when fixing it is outside the current task.
Check existing entries first; extend them rather than duplicating a finding. Include
actual versus intended behavior, affected functions, evidence, consequence, and status.
Label suspected issues and unresolved design decisions explicitly; do not present them
as confirmed defects. Preserve the finding when it is fixed and update its status.
Recording a bug does not expand a documentation-only task into a runtime repair.

## Architecture

EmiOS is a local-first personal AI assistant: Flask + SQLite + ChromaDB with 65+ LLM agents, knowledge graph memory, and multi-transport communication (UI/WebSocket, SMS, Slack, Telegram).

Full architecture docs live in `docs/architecture/`. Key docs: `00_OVERVIEW.md`, `05_DAYFLOW.md`.

### Layered Architecture

```
Transport (UI, SMS, Slack, Telegram)
  → Room Session Manager (transport abstraction, session modes)
    → Manager Layer (MultiAgentManager — deterministic agent loop)
      → Agent Layer (LLM decisions with structured output)
        → Control Nodes (deterministic routing, tool dispatch)
          → Tool Layer (registered tools with Pydantic schemas)
            → Service Layer (DI, EventHub, Blackboard, ResourceManager)
```

### Agent Contract

Each agent lives in `app/assistant/agents/<namespace>/<agent_name>/` with:
- `config.yaml` — name, LLM params, allowed tools, context items
- `prompts/system.j2`, `prompts/user.j2` — Jinja2 templates
- `agent_form.py` (optional) — Pydantic model for structured output

Agents produce decisions; they never directly execute actions. Control nodes and tools act on agent output.

Context items in `config.yaml` (`user_context_items`) are resolved by the context injector: `resource_*` prefixed items load from ResourceManager, special keys (`active_dayflow_items`, `planned_tasks`, etc.) resolve from the blackboard, and `agent_input` comes from the inbound Message.

### Manager Contract

Managers live in `app/assistant/multi_agents/<name>/config.yaml`. Every manager (rooms included) is `class_name: MultiAgentManager`; routing is deterministic via the `Delegator` agent's `flow_config.state_map` lookup. Managers are invoked via `DI.manager_invoker.invoke(manager, message)`, one fresh instance per invocation.

`request_handler` copies the inbound message's `data` onto the blackboard once; the activation Message a control node receives carries none of it — read trigger data from the **blackboard**, never `message.data`. Construction rejects `state_map` targets outside configured names plus role-binding keys/values; it does not prove that every alias resolves to a loaded instance or that every emitted return-control edge exists. Check those explicitly.

Runtime-owned key conventions and the actual agent-output/input filter are documented in `docs/architecture/02b_RUNTIME_DATA_CONTRACT.md`. Use `docs/architecture/02_MANAGERS.md` for cancellation, scope ingress, budgets, and exit results; `final_answer` alone is not proof of success because the default error fallback uses that type too.

### Dependency Injection

Global service registry: `DI` from `app/assistant/ServiceLocator/service_locator.py`. Access services via `DI.event_hub`, `DI.tool_registry`, `DI.agent_factory`, `DI.global_blackboard`, etc. Bootstrap happens in `app/bootstrap.py`.

### Dayflow Orchestrator

**Evaluator closure:** complete/abandon decisions first save durable `pending_work_closure` intents. A failed closure stops the pass and blocks further execution for that work; evaluator prep retries before planning. Do not clear the intent to make work runnable. Concern feedback is post-commit and best-effort.

**Execution ownership:** use standard ManagerInvoker, agent activation, tool dispatch,
and monitored thread/executor helpers. WorkContext preserves the main task's captured
epoch through helper provenance. Timeout/abandonment revokes further calls and writes;
it does not kill threads. Durable attempt/call receipts block replacement execution
within the work object while execution or an external outcome remains unresolved.
Never clear that barrier merely to retry. See `docs/architecture/EXECUTION_OWNERSHIP.md`.

**Task/provenance boundary:** only direct `subtask` children of the goal are independent
orchestrator assignments (`WorkObject.is_work_unit`). Worker-created descendants are
execution provenance, never candidates for promotion, wake or dispatch. A takeover
worker and the finalizer read their full history; architect/steward read the finalizer's
summary. Preserve provenance on takeover; do not add parent-completion dependencies
or turn internal records into additional graph tasks. See architecture/08_WORK_OBJECTS.


Autonomous daily workflow engine (`app/assistant/dayflow_orchestrator/`). Event-driven via `DayflowScheduler` (debounced, mutual exclusion, precise per-node time wakes, work-progress follow-up ticks). Full doc: `docs/architecture/05_DAYFLOW.md`.

**Everything actionable is a WORK OBJECT**: a goal plus a typed graph in the work store (`work_objects/`; five graph tables and two execution-receipt tables in emi.db via `work_store.py`, plus migration metadata). Ownership cycles are checked; new dependency edges reject cycles, self-links and duplicates. Historical graph validation does not repair old dependency defects. Each `apply` atomically writes current graph state and a mutation-input audit event; the log is not replay-complete. Changed `set_status` targets use per-family transitions; entering `closed`/`abandoned`/`superseded` requires a reason, as does terminal `set_work_status`. Initial node statuses must belong to the lifecycle; same-status writes do not replay transition checks. See `docs/architecture/08_WORK_OBJECTS.md` for persistence and result-fence limits.

**Three managers, one pass at a time** — a planning tick and a node wake both hold `DayflowScheduler._run_gate`:

- **`dayflow_orchestrator_manager`** — the planning tick (state_map in `multi_agents/dayflow_orchestrator_manager/config.yaml`): intake_triage → context_enricher → evaluator (`strategic_planner_wo`: decides WHAT work exists, converts intake to work objects) → work_architect (per-goal DAG + re-plan; wake primitives `wake_at` | `wake_ref`; a replan prunes queued/`actionable` or future-wake-held nodes ONLY when licensed by the finalizer's verdict on that node or a steward-classed user directive — the store's churn fence refuses silence-based prunes) → state_mover (deterministic `is_ready` promotion; the LLM may HOLD for current context; its prompt forbids holding a boundary announcement for the boundary it announces) → materializer → action_selector → switchboard (reads each node's GOAL: reach-the-user → `create_dayflow_ticket`, everything else → `work_emi_team_manager`) → **`work_node_dispatch` claims ONE node and the pass ENDS**.
- **`dayflow_wake_manager`** — one due time-wake: stage that node → state_mover re-judges the moment → dispatch or hold. No planning stage exists in it, so a wake cannot re-plan.
- **`dayflow_dispatch_manager`** — one claimed node on its own thread: arguments → the tool call (which BLOCKS this room, not the tick) → `work_finalizer_node`.

**The finalizer** judges whether the node's GOAL was achieved — `achieved` / `achieved_plan_changes` / `retry` / `unrecoverable` (+ `next_step` = stop | new_approach | ask_user) — always with an `outcome` prose account, persisted on the node at `payload.finalizer`. Normal Dayflow producer of `closed` (all main tasks require it; nested checklist subtasks can satisfy at `done`). This is a runtime convention, not a store authorization rule. Every not-achieved verdict passes through `failed`, which is how a step reaches the architect. Repeated failure is the runtime's call: at `_REPEAT_FAILURE_LIMIT` unmet attempts since the last successful main-task judgment it forces `ask_user`, except that `stop` is never overridden. Lifetime failure counts remain historical; legacy objects without an episode counter start at zero. Explicit approval denial propagates through nested managers and requires the finalizer to stop the action. `work_repair` is retired (files on disk, unwired); `sweep_stuck_work_nodes` records a failed result after over 80 minutes of subtree inactivity; ordinary finalization judges that result and owns failure counting; the steward can request an architect re-plan.

**Asks**: a node whose goal is to ask/tell the user is ticketed and marked `dispatched + wake_kind=user_reply` — an in-flight tool call whose result is the user's reply (one live ask per work object; a new ask ticket supersedes prior open ones). The reply/dismissal is recorded as evidence → done and the finalizer judges it; ticket expiry returns "user not reached", which is also recorded and finalized. Boot recovery reconnects the existing ticket and runs the same recorder and `WorkFinalizerNode` before signaling planning; it never creates another ticket. There is no re-ask timer.

**Items** (`unified_log_2026`, `source='dayflow_item'`, upsert by `Message.id` = `metadata.item_id`, `short_id` for prompts) are now intake + context: ingestion (chat/email/delegation/pods) → triage → the evaluator converts actionable intake and closes it `converted_to_work_object:<id>`. Item transitions live in `dayflow_item_writer.ALLOWED_TRANSITIONS`, enforced by `write_dayflow_item`; `state_store.py` reads. The item dispatch lane is retired (a guard in `work_node_dispatch_node` raises on non-work-node references).

### Rooms

Scoped conversation channels in `app/assistant/rooms/<room_id>/`. Each room has identity, permissions, authority level, and policy. `master_room` is primary UI (authority 99). `dayflow_orchestrator` is the autonomous workflow room.

### Pipelines & Routines

Pipelines (`app/assistant/pipelines/`) are sequential step-based processing (daily_insights, kg_chat, entity_cards, belief_engine). Routines schedule execution with 5 runner types (tool, task, job, function, pipeline) and policies (interval, daily, weekly) gated by active windows. **Entries are one JSON file per routine** in `configs/routines/public/` (tracked) and `configs/routines/private/` (gitignored, wins on id collision); `configs/routines.json` holds only manager settings. Handlers are auto-discovered via `@routine_handler()` in `app/assistant/routine_handlers/`; `routine_manager/routine_functions.py` is the legacy registry.

### Knowledge Graph

SQLite + ChromaDB. Data layer in `app/assistant/kg/` (store, promoter, chroma sync); utilities/taxonomy in `app/assistant/kg_core/`. Entity cards, taxonomy hierarchy, sentence-level storage with embeddings. The graph is single-owner: nodes/edges carry no owner column (`ScopeContext.owner_id` is identity metadata elsewhere in the system, not a KG partition key).

## Key Conventions

### Dayflow item rendering in agent prompts

All dayflow orchestrator agents use a standardized Jinja2 pattern to render `active_dayflow_items`, grouping by state and using:
```jinja2
- task: {{ meta.get("task_id", meta.get("short_id", meta.get("item_id",""))) }}{% if meta.get("plan_id") %} | plan: {{ meta.get("plan_id") }}{% endif %} | state: {{ meta.get("state","") }}
  {{ meta.get("summary","") }}
```
New dayflow agents should follow this same pattern rather than inventing custom rendering.

### Task spec contract

Task specs are immutable once a run starts. Markdown with YAML frontmatter. Required keys: `task_id`, `manager`, `inputs`, `outputs`. Run state recorded under `tasks/runs/<run_id>/`.

### HTML/CSS

Page-specific styles go in dedicated CSS files in `app/static/css/`, never inline `<style>` blocks.

## Development Principles

- **Fix root causes, not symptoms.** Never patch one instance; fix the pipeline so all cases work. No manual JSON edits, no bandaids.
- **Fail loudly.** Never swallow exceptions with default return values. If something fails, raise or log ERROR and re-raise. Only use defaults for genuinely optional function arguments.
- **No backwards compatibility layers.** When refactoring, update all callers. No shims, aliases, or legacy wrappers. Exception: changes that affect database schemas or stored data — ask first.
- **Read before answering.** Never make claims about how code works without reading the actual file first.
- **Ask when unclear.** Stop and ask for clarification rather than guessing.

## Working Principles

### 1. Think Before Coding
Don't assume. Don't hide confusion. Surface tradeoffs.

Before implementing:

- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First
Minimum code that solves the problem. Nothing speculative.

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes
Touch only what you must. Clean up only your own mess.

When editing existing code:

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:

- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution
Define success criteria. Loop until verified.

Transform tasks into verifiable goals:

- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:

```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

### Dayflow repair contract (2026-09-19)

Claims, result recording, finalizer judgments and architect revision batches are transactional and dispatch-epoch fenced. Finalizer judgments increment failure counts once per attempt. Admitted intake from every source remains durable until transferred with source context to a work object or explicitly reviewed as no-action. Deferrals require a reason and future reconsideration time; omitted/failed decisions stay pending. See `dayflow_orchestrator/intake_review.py`. Agent-facing work text is rendered by shared/work Jinja templates; Python prepares structured values. Architect and steward see main-task statuses, dependencies, gates and finalizer summaries; workers and finalizers additionally see owned provenance. See docs/design/dayflow_prompt_context_standard_2026-09-19.md and its rendered example. External-source context is rendered automatically from structured intake and wake evidence, with exact pod references and attributed summaries/excerpts. External wakes require a prepared source ID and pre-LLM work-version fence; gate release and evidence creation are atomic. Do not append arrival evidence to task directives or treat a linked reply as proof of approval. Contextual ticket choices are designed by ticket_builder::composer; label/count validation is deterministic, semantics live in Jinja. Preserve exact choice meaning/scope and typed text in response history. Acknowledgment is receipt only; accepted ticket state is not authorization. Tool approvals use their separate gate.


### Concern outcome delivery

Concern-linked terminal transitions enqueue `work_concern_feedback` in the same
WorkStore transaction, including automatic rollup. Keep register/model calls outside
the generic store. Dayflow finalization/closure deliver after commit; evaluator prep
recovers pending receipts. Register writes are receipt-idempotent; acknowledge only
after every linked concern is updated. Preserve `work_outcomes` in concern context,
including attributed user replies and finalizer judgments. A completed notification
does not prove the underlying need resolved. Noticer recurrence policy lives in Jinja.
