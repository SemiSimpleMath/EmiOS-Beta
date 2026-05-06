# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

```bash
# Unit tests (pytest)
.venv\Scripts\python.exe -m pytest app/assistant/test/agent_tests/

# Single test file
.venv\Scripts\python.exe -m pytest app/assistant/test/agent_tests/test_foo.py

# Standalone smoke/integration scripts (not pytest — run directly)
.venv\Scripts\python.exe app/assistant/test/tmp_smoke_kg_convergence.py

# Manager integration tests (real KG + LLMs)
.venv\Scripts\python.exe app/assistant/tests/manager_tests/emi_team/emi_team_test.py
```

Standalone test scripts must bootstrap DI before any project imports:
```python
import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.ServiceLocator.service_locator import DI
```

## Architecture

EmiOS is a local-first personal AI assistant: Flask + SQLite + ChromaDB with 65+ LLM agents, knowledge graph memory, and multi-transport communication (UI/WebSocket, SMS, Slack, Telegram).

Full architecture docs live in `docs/architecture/`. Key docs: `00_OVERVIEW.md`, `05_DAYFLOW.md`.

### Layered Architecture

```
Transport (UI, SMS, Slack, Telegram)
  → Room Session Manager (transport abstraction, session modes)
    → Manager Layer (MultiAgentManager / RoomManager — deterministic agent loop)
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

Managers live in `app/assistant/multi_agents/<name>/manager_config.yaml`. `RoomManager` extends `MultiAgentManager` with a `state_map` for deterministic agent routing. Managers are invoked via `DI.manager_invoker.invoke(manager, message)`.

### Dependency Injection

Global service registry: `DI` from `app/assistant/ServiceLocator/service_locator.py`. Access services via `DI.event_hub`, `DI.tool_registry`, `DI.agent_factory`, `DI.global_blackboard`, etc. Bootstrap happens in `app/bootstrap.py`.

### Dayflow Orchestrator

Autonomous daily workflow engine (`app/assistant/dayflow_orchestrator/`). Event-driven via `DayflowScheduler` (debounced, mutual exclusion). Processes items through 9 sub-agents in `app/assistant/agents/dayflow_orchestrator/`.

**Lifecycle**: `new → artifact / important_open / actionable → dispatched → closed`. Side states: `waiting` (blocked on time/event), `watching` (passive), `suppressed` (rejected, terminal), `needs_planning`. Canonical transitions live in `dayflow_item_writer.ALLOWED_TRANSITIONS` and are enforced by `write_dayflow_item`.

**Persistent state**: All items stored in `unified_log_2026` as Messages with `source='dayflow_item'`. Upsert by `Message.id` = `metadata.item_id`. Short numeric IDs (`short_id`) in metadata for LLM prompts. `state_store.py` reads; `dayflow_item_writer.py` writes.

**Tick flow**: `run_dayflow_ingestion()` pulls new chat/email/delegation rows into the items table → `sweep_stale_dispatches()` closes stuck dispatches → manager invocation runs the agent pipeline. Per-agent prep nodes load their own context off the items table; there is no monolithic blackboard builder.

**Tickets are tools, not items**: `create_dayflow_ticket` is a tool the switchboard dispatches like any other. The calling room blocks on the user's response (`threading.Event`); when it returns, `post_room_finalize_node` closes the source item via `acted_on_item_ids`.

### Rooms

Scoped conversation channels in `app/assistant/rooms/<room_id>/`. Each room has identity, permissions, authority level, and policy. `master_room` is primary UI (authority 99). `dayflow_orchestrator` is the autonomous workflow room.

### Pipelines & Routines

Pipelines (`app/assistant/pipelines/`) are sequential step-based processing (daily_insights, kg_chat, entity_cards, belief_engine). Routines (`configs/routines.json`) schedule execution with 5 runner types (tool, task, job, function, pipeline) and policies (daily, weekly, interval, quiet_hours). Routine functions registered in `app/assistant/routine_manager/routine_functions.py`.

### Knowledge Graph

SQLite + ChromaDB. Core in `app/assistant/kg_core/`. Entity cards, taxonomy hierarchy, sentence-level storage with embeddings. KG owner scoping via `ScopeContext.owner_id`.

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
