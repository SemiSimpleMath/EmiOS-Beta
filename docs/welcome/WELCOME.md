# Welcome to EmiOS

You are looking at a personal AI assistant that runs locally on the user's laptop. It has memory (a knowledge graph, plus a belief engine and a wiki built on top of it), it talks across multiple surfaces (UI, SMS, Slack, Telegram), it runs autonomous routines (morning briefing, KG maintenance, wiki refresh) and an autonomous "subconscious" that proactively reaches out, and it composes specialized LLM agents into managers that solve concrete tasks.

If this is your first day in the codebase, the goal of this page is to get you productive in roughly an afternoon.

In code: when you see `wiki_writer.py`, `/wiki/`, "the wiki vault," etc., that refers to **EmiPedia** — the user-facing personal-life wiki that EmiOS generates from the knowledge graph into a Markdown vault on disk. Not these docs.

---

## What EmiOS is, in five lines

1. A Flask app that serves a chat UI plus a fleet of admin pages.
2. The chat UI talks to **Rooms** (scoped channels). The main room is `master_room`.
3. Each room invokes a **Manager** that runs an **Agent loop** (LLM → control node → tool → repeat).
4. Background **Routines** (one file per routine under `configs/routines/public/`) fire scheduled work — pipelines, tools, tasks, functions.
5. A **Knowledge Graph** (SQLite + ChromaDB) stores everything the assistant knows about the user; it's the source for a browseable **Wiki**, structured **entity cards**, and a confidence-tracked **belief engine**.

If you understand those five pieces, you understand EmiOS at the architectural level. The rest is detail.

A handful of larger subsystems sit on top of this skeleton — you don't need them on day one, but you should know they exist:

- **Memory** — the **Knowledge Graph** (`kg_core/`, `kg/`) plus what's built on it: the per-entity **Wiki** (EmiPedia) and structured **entity cards**.
- **Belief engine** (`belief_engine/`) — confidence-tracked beliefs derived from daily insights, with tag-scoped retrieval that agents pull from.
- **Subconscious** (`subconscious/`) — the autonomous "mind": a concerns register, proactive outreach woven into chat, and proposer/arbiter lanes (meal, wellness, scheduling).
- **Pods** (`pod_store/`) — URI-addressable artifacts (`datapod:kind:id`); agents pass references instead of bytes.
- **Scope** (`scope/`) — the permission model: a four-layer gate (allowed-tools ceiling → visibility → authority floor → approval) on every Message.

See **[00_OVERVIEW](../architecture/00_OVERVIEW.md)** for the subsystem map and **[INDEX](../INDEX.md)** for the per-subsystem doc.

---

## Recommended read order

1. **You are here.** Finish this page.
2. **[Mental Model](MENTAL_MODEL.md)** — plain-English definitions of Manager, Agent, Planner, Room, Scope, Tool. Read this **before** any architecture doc.
3. **[Glossary](../GLOSSARY.md)** — keep open in a tab. Refer when a term you don't recognize appears.
4. **[00_OVERVIEW](../architecture/00_OVERVIEW.md)** — the layered stack diagram + the subsystem map.
5. Then go subsystem-by-subsystem in dependency order: 01_AGENTS → 02_MANAGERS → 04_CONTROL_NODES → 03_ROOMS → 07_TOOLS → 06_PIPELINES_AND_ROUTINES → 09_KG_PIPELINE → the rest.
6. For the bigger subsystems, read the dedicated doc when you need it — belief engine (16), subconscious (SUBCONSCIOUS.md), pods (14), scope (SCOPE.md), entity cards (12). The **[INDEX](../INDEX.md)** lists them all.
7. When you have a concrete task, jump to the relevant **[Recipes](../INDEX.md#recipes-how-to-add-things)** page.

---

## Dev environment

```bash
# One-time setup
python setup.py            # creates .venv, installs requirements.txt

# Run the app
emi.bat                    # Windows
./emi.command              # Mac
# or directly
.venv\Scripts\python.exe run_flask.py
```

The app starts at `http://localhost:8000`.

**Critical**: always use `.venv\Scripts\python.exe` (Windows) or `.venv/bin/python` (Mac/Linux). System Python is missing project dependencies and will silently produce broken imports.

### Run tests

All tests live under `app/assistant/tests/` (plural). Throwaway probes go in `/scratch/` (gitignored), never in a tests dir.

```bash
# pytest unit tests
.venv\Scripts\python.exe -m pytest app/assistant/tests/agent_tests/

# Manager integration tests (real KG + real LLMs)
.venv\Scripts\python.exe app/assistant/tests/manager_tests/emi_team/emi_team_test.py
```

Standalone test scripts must bootstrap DI before any project imports:
```python
import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.ServiceLocator.service_locator import DI
```

---

## What lives where

| Directory | What's in it |
|-----------|--------------|
| `app/assistant/agents/` | Every LLM agent — one directory per agent, with `config.yaml`, `prompts/`, optional `agent_form.py` |
| `app/assistant/multi_agents/` | Manager configurations (e.g., `emi_team_manager`, `kg_investigation_manager`) |
| `app/assistant/manager_classes/` | Base classes: `MultiAgentManager`, `RoomManager` |
| `app/assistant/control_nodes/` | Deterministic state machines that route the agent loop |
| `app/assistant/lib/tools/` | Tool entry-point wrappers (one directory per tool) |
| `app/assistant/lib/core_tools/` | Tool implementations (the heavy logic) |
| `app/assistant/rooms/` | Per-room config — one `ROOM.md` (frontmatter: policy/permissions/access) + `scope.yaml` per room |
| `app/assistant/pipelines/` | Step-based pipelines (daily_insights, kg_pipeline, etc.) |
| `app/assistant/routine_manager/` | The scheduler that fires pipelines + tools + tasks |
| `app/assistant/kg_core/` | Knowledge graph core (taxonomy, scoring, etc.) |
| `app/assistant/kg/db/` | KG database models (`Node`, `Edge`) |
| `app/assistant/wiki_generator/` | The wiki page generation pipeline |
| `app/assistant/entity_management/` | Entity cards (per-entity "what's true now" snapshots) |
| `belief_engine/` | The belief engine (top-level package; tables live in `emi.db`) |
| `app/assistant/subconscious/` | The autonomous "mind": concerns register, proposers, digest, noticer |
| `app/assistant/pod_store/` | Pods: store, classifier, materializers, ingest |
| `app/assistant/scope/` | Scope loader + permission contracts |
| `app/assistant/dayflow_orchestrator/` | The autonomous daily workflow engine |
| `app/assistant/ServiceLocator/` | The DI container (`from app.assistant.ServiceLocator.service_locator import DI`) |
| `app/routes/` | Flask routes — UIs and APIs |
| `app/templates/` | Jinja2 page templates |
| `app/static/` | CSS / JS / images |
| `configs/` | JSON config (routines, model tiers, etc.) |
| `resources/` | JSON state files persisted at runtime |
| `docs/` | This documentation |
| `tasks/` | Task specs (saved playbooks the user can rerun) |

---

## Your first read pattern

When a page in the chat UI feels wrong, find the room → find the manager → find the agent that produced the bad output → read its `prompts/system.j2` and `agent_form.py`. That four-step trace works for >90% of debugging.

When a scheduled routine fails, open `/debug/runtime/concurrency` (the runtime monitor), then `/routines` (the schedule view) to see last-run / last-error per routine.

When a KG fact looks wrong, open the **KG Visualizer** from the "My Life" menu, find the node, drill back to the source window via `node → proposal → evidence → unified_log_id`. See [09_KG_PIPELINE](../architecture/09_KG_PIPELINE.md) for the chain.

---

## Coding principles (also in CLAUDE.md)

- **Fix root causes, not symptoms.** No band-aids, no manual JSON patches.
- **Fail loudly.** Never swallow exceptions with default return values. Defaults are for genuinely optional function arguments — not for hiding errors.
- **No backwards compatibility shims.** Update all callers. (Schema/DB changes are the one exception — ask first.)
- **Read before answering.** Never reason about how code works without reading the file.
- **Ask when unclear.** Stop and ask rather than guess.

These are not aspirational — the user enforces them in code review and will roll back PRs that violate them.

---

## When you are ready to ship something

- Adding an agent? → [recipes/ADD_AN_AGENT.md](../recipes/ADD_AN_AGENT.md)
- Adding a tool? → [recipes/ADD_A_TOOL.md](../recipes/ADD_A_TOOL.md)
- Adding a manager? → [recipes/ADD_A_MANAGER.md](../recipes/ADD_A_MANAGER.md)
- Adding a pipeline? → [recipes/ADD_A_PIPELINE.md](../recipes/ADD_A_PIPELINE.md)
- Adding a routine? → [recipes/ADD_A_ROUTINE.md](../recipes/ADD_A_ROUTINE.md)
- Adding a room? → [recipes/ADD_A_ROOM.md](../recipes/ADD_A_ROOM.md)

Welcome aboard.
