---
name: diagnosing-emi
description: How to find your way around EmiOS without reading the whole project — which directory and architecture doc own a given subsystem, how to read the logs to see what EmiOS actually did, how to query runtime state in emi.db, and the repo-specific traps. Use when fixing a bug, explaining why EmiOS behaved a certain way, tracing a request through the layers, or locating the code that owns something.
license: Apache-2.0
metadata:
  author: emi-team
  version: "1.0"
  auto_inject_when:
    task_keywords:
      - "fix"
      - "bug"
      - "why did"
      - "debug"
      - "trace"
      - "diagnose"
      - "where does"
      - "read the logs"
---

# Finding your way around EmiOS

`CLAUDE.md` is already loaded and covers conventions, the venv, test commands
and the layer stack. This skill covers the part it does not: **where a thing
lives, and how to see what the running system actually did.**

The sibling `extending-emi-*` skills each describe adding one kind of thing.
This one is for everything else — fixing, tracing, explaining.

## 1. Go to the owning directory first

`docs/architecture/` holds 38 files and ~790KB. Do not browse it. Pick the one
row you need:

| The subsystem | Code | Doc |
|---|---|---|
| Autonomous daily workflow, ticks, dispatch | `app/assistant/dayflow_orchestrator/` | `05_DAYFLOW.md`, `05a_DAYFLOW_ORCHESTRATOR_REFERENCE.md` |
| Goals + node DAGs, the work store | `work_objects/` | `08_WORK_OBJECTS.md` |
| Deterministic routing between agents | `app/assistant/control_nodes/` | `04_CONTROL_NODES.md` |
| An LLM decision unit | `app/assistant/agents/<ns>/<name>/` | `01_AGENTS.md` |
| Agent + control-node wiring, `state_map` | `app/assistant/multi_agents/<name>/config.yaml` | `02_MANAGERS.md`, `02b_RUNTIME_DATA_CONTRACT.md` |
| A conversation surface, its policy + authority | `app/assistant/rooms/<room_id>/` | `03_ROOMS.md` |
| A callable capability | `app/assistant/lib/tools/<name>/` | `07_TOOLS.md` |
| Knowledge graph store + promotion | `app/assistant/kg/`, `app/assistant/kg_core/` | `09_KG_PIPELINE.md`, `13_KG_MUTATOR_TOOLS.md` |
| URI-addressable memory objects | `app/assistant/pods/` | `14_PODS.md`, `14b_PODS_MEDIA_LIFECYCLE.md` |
| Concerns register, noticer, arbiter | `app/assistant/subconscious/` | `SUBCONSCIOUS.md` |
| Scheduled / evented execution | `configs/routines.json`, `app/assistant/routine_manager/` | `06_PIPELINES_AND_ROUTINES.md` |
| Who may do what, scope resolution | `app/assistant/scope/` | `SCOPE.md`, `15_EMI_TEAM_AND_SCOPE.md` |
| Credentials, OAuth accounts | `app/assistant/lib/google_auth/` | `SECRETS_ACCOUNTS.md` |
| UI, SMS, Slack, Telegram | `app/assistant/room_session_manager/` | `18_TRANSPORTS.md` |
| Text injected into prompts | `resources/` | `19_RESOURCES.md` |

Read the whole owning file, not a keyword slice. A narrow search that returns
nothing proves nothing — it usually means the search was wrong.

## 2. Read what actually happened, not what should have happened

Each run writes two logs named for its process id:

- `logs/emi_logs_<pid>.log` — everything. Large.
- `logs/emi_logs_<pid>_agents.log` — **the one that answers "what did EmiOS do"**:
  rendered prompts as the agent actually received them, and tool calls with
  their real arguments.

**Confirm which file is live before drawing any conclusion.** The directory
holds logs from every past run, so a search against a stale pid returns nothing
and looks exactly like "it never happened":

```bash
ls -lat logs/*.log | head
```

Then read a contiguous window around the timestamp. Reading a window beats
pattern-hunting: the surrounding lines are what explain the one you were
looking for.

When the question is "why did the agent decide that", find the rendered prompt
in `_agents.log` and read what it was actually shown. Do not reason about what
a Jinja template ought to have produced.

## 3. Inspect runtime state

Open `emi.db` read-only so a running EmiOS is never disturbed:

```python
import sqlite3
con = sqlite3.connect("file:E:/EmiAi_sqlite/emi.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
```

| Question | Table |
|---|---|
| What work exists, and is it open | `work_objects` |
| What steps that work has, their status and wake | `nodes` |
| The audit trail of who changed a work object | `events` |
| What was said, on any surface, plus dayflow items | `unified_log_2026` |
| What EmiOS asked the user and what came back | `tickets`, `proactive_tickets` |

`events` is the one to reach for when a work object is in a state nobody
expects — it records the actor and operation for every transition.

The concerns register is a file, not a table:
`resources/subconscious/resource_concerns_register.json`.

## 4. Repo-specific traps

- **Two hooks police edits.** `.claude/hooks/no_reflex_grep.py` blocks the Grep
  tool outright — read whole files, or define `grep_2() { grep "$@"; }` in Bash
  when you genuinely need to locate a known token across many files.
  `.claude/hooks/no_fallback.py` rejects the word "fallback" anywhere in code.
- **EmiOS may be running while you edit.** Changes to an imported module do not
  reach the live process; a restart is required for anything on the boot path.
- **Some suites are already red** for reasons unrelated to any change you make:
  the `test_web_*` files under `non_agent_tests/` all fail on a missing
  `app/mcp/refresh_tool_cache.py`. Establish the baseline before blaming
  yourself.
- **A few tests are order-dependent** and pass alone but fail in a full run.
  Re-run a suspicious failure in isolation before treating it as a regression.

## 5. Verify the change

Use the venv Python explicitly — system Python lacks the dependencies:

```bash
.venv\Scripts\python.exe -m pytest app/assistant/tests/<subsystem>/
```

Match the suite to what you touched: `dayflow/` for orchestrator and work
objects, `tool_tests/` for tools, `agent_tests/` for agents and control nodes,
`non_agent_tests/` for everything else. Throwaway probes belong in `/scratch/`,
never in a tests directory.
