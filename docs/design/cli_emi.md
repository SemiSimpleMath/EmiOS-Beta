# CLI Emi — design proposal

Status: draft for discussion. No code yet.

## TL;DR

Emi's runtime (DI, agents, managers, orchestrators, tools, KG, pods, blackboard) does not depend on Flask. Flask is one of several front-ends: it routes HTTP / WebSocket traffic into `process_request` and out via socketio / event_hub. Strip it away and the same runtime is callable from a Python entry point.

A CLI version of Emi is therefore mechanically simple — bootstrap DI, build a `Message`, invoke a manager or orchestrator, print the result. The interesting design questions are not "can it be done" but "what does it cost to maintain a second front-end" and "what scope is it allowed to touch."

This doc proposes three CLI shapes (one-shot, REPL, headless daemon), defines a `cli_room` policy + scope, and lays out a phased path: ship the one-shot first, evaluate, then decide on REPL and daemon.

## Goals

1. **Sub-second iteration** on KG / dev / ops tasks without bouncing Flask.
2. **Scriptable Emi** — pipe input/output, integrate with shell tooling, run in CI.
3. **Headless deployment** — Emi as a service on a Pi / VPS without UI.
4. **Debug surface** — invoke a single manager or orchestrator in isolation, see exactly what it returns. Doubles as integration-test harness.
5. **Preserve the guarded principle.** Same scope_contract / authority / tool-approval mechanisms apply. CLI is a different transport, not a backdoor that bypasses guards.

## Non-goals

- Replacing the Flask UI. The web UI stays the primary interactive surface.
- Multi-user CLI. CLI is single-actor by definition; no SMS / Telegram / family-room concept here.
- Streaming token output. Manager / orchestrator results are emitted as a single ToolResult / final_answer. Streaming would be a later add.
- Browser-driving tools (`playwright_manager`, web_*). Those need a browser process; out of scope for CLI v1. CLI's cli_room policy will exclude them.

## Three CLI shapes

### Mode A — one-shot

```
$ emi-cli "find duplicate Person nodes"
Found 4 candidate pairs:
  Isa (d6ce2baf) ↔ Jorma (41fb3423)
  ...
```

- Entry point: `app/cli/emi_cli.py` (or `emi-cli` script in repo root).
- Boots DI, builds one Message, invokes the chosen manager or orchestrator, prints `final_answer.final_answer_answer`, exits.
- Default target: `kg_dev_manager` (the specialized KG manager). Override via `--manager` flag.
- Default authority: 99 (developer at the keyboard).
- No session persistence. Each invocation is independent.

Use cases: maintenance scripts, ops one-liners, CI checks, bash-piped operations.

### Mode B — REPL

```
$ emi-cli
emi> find duplicate Person nodes
[5/10] kg_query: ...
Found 4 candidate pairs.
emi> merge d6ce2baf into 41fb3423
✓ Merged 'Isa' into 'Jorma'. revision_log_id=e7ba2f4e
emi> ^D
```

- Same entry point with `--repl` flag (or no args).
- Persists transcript to `~/.emi_cli/sessions/<session_id>.jsonl` — same JSONL shape Claude Code uses (one event per line).
- `--continue` resumes the most recent session. `--session <id>` resumes a specific one.
- Recent transcript is fed to chat_gate so the agent sees context across turns.
- Single-thread input; while a manager is running, terminal is blocked. Ctrl-C cancels.

Use cases: interactive debugging, exploratory investigation, "developer console for everything".

### Mode C — headless daemon

```
$ emi-daemon --config emi.daemon.toml
[2026-05-01 10:00:00] dayflow tick: 2 new email rows ingested
[2026-05-01 10:00:03] dispatched ticket #t_abc to telegram:jukka
[2026-05-01 10:00:15] routine: nightly_kg_maintenance starting
...
```

- Same code as REPL but with stdin disabled and a daemon config.
- Runs `dayflow_orchestrator` ticking, `routine_manager`, `socket_manager` for outbound (Telegram, etc.).
- Inputs come from existing ingress paths (email, Telegram, scheduled routines).
- Outputs go to logs and configured outbound rooms (Telegram, SMS, etc.) — NOT to the (absent) UI.
- Best for "Emi as a service on a server" without humans staring at it.

Use cases: home-server Emi, VPS deployment, multi-machine setups where the UI is on one box and the worker is on another.

**Phasing recommendation:** ship A first (single-shot, no session, no daemon). It's the smallest. Evaluate. Then decide on B and C separately.

## Architecture

### What stays exactly the same

- DI service locator (`app.bootstrap`).
- Tool registry (file-based; same on disk).
- Agent registry, manager registry, orchestrator registry.
- Knowledge graph (emi.db), pod_store, ChromaDB, unified_log.
- Manager runtime (manager_invoker, manager_runtime, blackboard, control_nodes).
- Orchestrator runtime (parallel children, blackboard, brain agents).
- Agent classes, prompt rendering, context injection.
- All existing managers, orchestrators, tools.

In other words: **almost everything**. The runtime never knew it was running inside Flask.

### What's new

A small CLI layer:

```
app/cli/
├── emi_cli.py          # main entry point, arg parsing, dispatch
├── cli_session.py      # transcript persistence (REPL only)
├── cli_event_sink.py   # subscribes to event_hub, prints progress to stdout
└── README.md
```

A new room:

```
app/assistant/rooms/cli_room/
├── policy.json         # authority, scope_contract, retention
├── permissions.json
├── access.json
├── resource_identity.json
├── resource_conversation.json
├── resource_room_context.json
└── resource_safety.json
```

That's the entire net-new code. ~150 lines of CLI + a room directory.

### What's removed

Nothing. Flask remains the primary front-end. CLI is additive.

### What we touch (small edits)

- `app/bootstrap.py` — accept a `mode="cli" | "flask" | "daemon"` flag so Flask-only services (socketio binding, route registration) skip when not needed.
- `event_hub` — add a CLI-aware sink alongside the existing socketio sink. Optional in v1 (CLI can ignore progress events).
- Routine manager — needs a flag to disable scheduled ticking when running in one-shot mode (so you don't fire a routine in the middle of a single command).

## Entry-point sketch (Mode A)

```python
# app/cli/emi_cli.py — sketch, not final code
import argparse, sys, uuid
from pathlib import Path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task", help="Task text. Use '-' for stdin.")
    parser.add_argument("--manager", default="kg_dev_manager",
                        help="Manager or orchestrator to invoke.")
    parser.add_argument("--orchestrator", action="store_true",
                        help="Treat --manager as an orchestrator name.")
    parser.add_argument("--authority", type=int, default=99)
    parser.add_argument("--json", action="store_true",
                        help="Print full ToolResult as JSON instead of just final_answer.")
    args = parser.parse_args()

    # Bootstrap in CLI mode — skips Flask app construction, socketio binding,
    # routine scheduler ticking.
    import app.bootstrap_cli  # new sibling of app.bootstrap
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import (
        Message, ScopeContext, ScopeApprovalPolicy, ScopeResourcePolicy,
    )

    task_text = sys.stdin.read() if args.task == "-" else args.task

    scope = ScopeContext(
        scope_id=f"scope::cli_room::{uuid.uuid4().hex[:8]}",
        owner_id="jukka",
        actor_id="cli_user",
        surface="cli",
        room_id="cli_room",
        approval=ScopeApprovalPolicy(authority_level=args.authority),
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
    )

    if args.orchestrator:
        orch = DI.orchestrator_factory.create_orchestrator(args.manager)
        result = orch.run(task=task_text, sender="cli_user")
        print(result.content)
    else:
        message = Message(
            data_type="agent_activation",
            sender="cli_user",
            content=task_text,
            task=task_text,
            scope_context=scope,
            room_id="cli_room",
            room_surface="cli",
        )
        result = DI.manager_invoker.invoke(args.manager, message)
        if args.json:
            import json
            print(json.dumps(result, indent=2, default=str))
        else:
            answer = (result or {}).get("final_answer", {}).get("final_answer_answer", "")
            print(answer)

if __name__ == "__main__":
    main()
```

This is the full Mode A in ~50 lines. The rest of the work is in `bootstrap_cli` (skipping Flask things) and the `cli_room` policy.

## cli_room policy

```json
{
  "policy_id": "room_policy::cli_room::v1",
  "manager_name": "kg_dev_room_manager",
  "surface": "cli",
  "default_visibility": "owner_only",
  "default_context_id": "main",
  "authority_level": 99,
  "history": {
    "scope": "session"
  },
  "retention": {
    "write_unified_log": true,
    "write_kg": false,
    "allow_fact_extraction": false
  },
  "delivery": {
    "auto_send": true,
    "allow_initiation": false
  },
  "privacy": {
    "owner_only_memory_visible": true,
    "room_facts_only": true
  },
  "participant_identity": {
    "display_name": "Jukka",
    "aliases": ["Jukka", "User", "cli_user"]
  }
}
```

Notes:

- `authority_level: 99` matches master_room. Developer at the keyboard, full power.
- `manager_name: kg_dev_room_manager` makes the dev console's chat_gate the default front for CLI sessions. Mode A bypasses this by invoking a target manager directly; Mode B uses it for the REPL flow.
- `write_kg: false` — CLI conversation isn't ingested into the KG (same as kg_dev_room).
- `surface: "cli"` — new surface enum value. Used by output sinks and possibly by `_resolve_reply_to` (which today switches between socketio / telegram / sms; CLI would route to stdout).

Open question: should CLI have its own scope_contract that's narrower than master_room's? E.g. block `send_email`, `playwright_manager`, anything that touches outside services by default unless explicitly enabled. Discuss.

## Permissions and scope

Three layers of guard apply, same as Flask:

1. **Room policy** — `cli_room/policy.json` declares authority and surface.
2. **Manager scope_contract** — each manager has its own scope_contract that narrows what tools it can dispatch.
3. **Tool-level approval** — `requires_approval`, `approval_min_authority` on each tool.

The CLI's `--authority` flag lets the user run as a lower-authority actor for testing, e.g. `--authority 60` to verify that something correctly blocks below authority 80. This is a real testing affordance.

Default for daily use: authority 99, same as the user sitting at the UI.

## Output and progress

Two paths:

- **Final result.** Manager / orchestrator returns; CLI prints `final_answer.final_answer_answer` to stdout. With `--json`, prints the full ToolResult.
- **Progress events.** During execution, agents and tools publish to event_hub: tool calls, action counts, child lifecycle (orchestrator), etc. Today these route to socketio. For CLI, add a stdout sink that subscribes to a small set of topics and prints one-line progress markers:

```
[gate] handing off: kg_merge_nodes
[planner] action 1/20: kg_query
[planner] action 2/20: kg_merge_nodes (dry_run=true)
[planner] return_control
```

Optional in v1; the CLI can run silently if the user doesn't want noise. `--verbose` flag turns it on.

For Mode C (daemon) the stdout sink IS the operational log. Plus the existing log file at `data/logs/`.

## Session persistence (Mode B)

Same shape as Claude Code:

```
~/.emi_cli/sessions/
└── 2026-05-01_jukka_a3f4b2c1.jsonl
```

Each line is one event:

```json
{"type": "user_message", "ts": "...", "content": "..."}
{"type": "agent_message", "ts": "...", "agent": "kg_dev::chat_gate", "content": "..."}
{"type": "tool_call", "ts": "...", "tool": "kg_query", "args": {...}}
{"type": "tool_result", "ts": "...", "tool": "kg_query", "content": "..."}
{"type": "final_answer", "ts": "...", "content": "..."}
```

`--continue` reads the most recent file and seeds the manager's blackboard with the historical messages. The chat_gate's `recent_history` then sees the full prior session.

This is the JSONL transcript pattern that's been working for Claude Code, Aider, Codex CLI for years. Don't reinvent.

## SQLite concurrency

Critical constraint: SQLite is single-writer. If both Flask and CLI-Emi target `emi.db` concurrently:

- **Reads are fine** in WAL mode (which the project uses).
- **Writes will conflict** under load. The user's existing rule (memory: `feedback_no_db_lock_over_llm`) already names this.

So:

- **Mode A (one-shot):** safe alongside Flask if the operation is short and read-mostly. Locking conflict is possible during writes (e.g. `kg_merge_nodes`); the manager would retry or fail.
- **Mode B (REPL):** same caveat, but you're more likely to do bursts of writes. Practical advice in the doc: "stop Flask before opening the REPL for write-heavy work."
- **Mode C (daemon):** mutually exclusive with Flask on the same DB. The daemon IS Emi for that machine.

A future hardening: a small lock file (`emi.db.cli.lock`) that CLI takes on launch for write-intensive modes; Flask's `process_request` can check for it and refuse to start a new turn while CLI holds it. Out of scope for v1.

## What we reuse vs. build

Reuse (no changes):

- DI, manager_invoker, manager_runtime, orchestrator runtime, agent runtime, tool registry, blackboard, control nodes, all 65+ agents, all managers, all tools, KG, pods, ChromaDB, unified_log.

Build:

- `app/cli/emi_cli.py` — main entry point. ~80 lines.
- `app/cli/cli_session.py` — JSONL transcript I/O. ~60 lines.
- `app/cli/cli_event_sink.py` — event_hub subscriber printing to stdout. ~40 lines.
- `app/bootstrap_cli.py` — DI bootstrap variant that skips Flask things. ~30 lines.
- `app/assistant/rooms/cli_room/` — six small JSON files copied from kg_dev_room, edited.
- `emi-cli` shell wrapper at repo root (Windows: `emi-cli.bat`).

Total: ~250 lines of net-new code + 6 small JSON files.

Edit (small):

- `app/bootstrap.py` — refactor to extract Flask-specific initialization into a `bootstrap_flask()` function so `bootstrap_cli()` can reuse the rest.
- `event_hub` — minor: ensure it can have multiple sinks (it probably already does for socketio + logging; CLI is just another sink).
- `_resolve_reply_to` (in chat_task_router_node etc.) — handle `surface == "cli"` to route ack messages to stdout instead of socketio.

## Phasing

**Phase 1 — Mode A only (1-2 days):**

1. `app/bootstrap_cli.py`.
2. `app/cli/emi_cli.py` for one-shot.
3. `cli_room/` policy.
4. `_resolve_reply_to` handles cli surface (no-op or stdout print).
5. `--manager`, `--orchestrator`, `--authority`, `--json` flags.
6. Smoke test: invoke `kg_dev_manager` with a known task; verify result matches Flask output.

Ship and use. Validates the runtime can be driven without Flask. Probably 90% of the value.

**Phase 2 — Mode B (REPL, +1 day):**

1. `cli_session.py` for JSONL transcripts.
2. `--repl`, `--continue`, `--session` flags.
3. Stdin loop, blackboard re-seeding from transcript.
4. `cli_event_sink.py` for progress lines.

Ship after Phase 1 has been used for a week and we know what's missing.

**Phase 3 — Mode C (daemon, +2-3 days):**

1. `app/cli/emi_daemon.py` — different entry, no stdin.
2. Daemon config file format (toml or yaml).
3. Disable Flask routes in bootstrap; enable scheduler / dayflow ticking.
4. Outbound routing — telegram / SMS / log file as default reply destinations.
5. Process management — pidfile, signal handling, graceful shutdown.
6. SQLite single-writer enforcement — refuse to start if another process holds the lock.

Ship only if there's a concrete deployment target. Mode C is real ops work.

## Use cases that justify the effort

For each, mark which mode covers it:

- **"Find every duplicate Person node and propose merges"** — Mode A. Single command, structured output, pipe to a review tool.
- **"Investigate the KG interactively for an hour"** — Mode B. REPL with session history.
- **"Run the nightly KG maintenance batch"** — Mode A or C. Single command can be cron'd; daemon ticks naturally.
- **"Build an integration test suite that exercises real managers"** — Mode A. CLI is the test harness.
- **"Run Emi on a Raspberry Pi at home, no monitor, talks to Telegram"** — Mode C.
- **"Pipe email content into Emi, get a structured summary, pipe to next tool"** — Mode A with `--json`.
- **"Debug why a manager's prompt is producing weird output"** — Mode A or B; in either, the LLM call still goes through, you can `--verbose` to see prompt construction.

## Risks

- **Path divergence.** A bug or behavior difference between Flask and CLI hides in one path. Mitigation: Mode A serves as the integration-test harness, so both paths are exercised in CI.
- **Maintenance burden.** Anything that assumes Flask context (cookies, session, request_id from HTTP) needs CLI fallbacks. Audit during Phase 1 implementation.
- **SQLite locking.** Already discussed. Document clearly.
- **Permission surprises.** A CLI default of authority 99 is permissive; mistakes could mutate KG that the Flask UI would have caught at scope_contract. Mitigation: cli_room scope_contract should explicitly list allowed managers, not "all".
- **"Backdoor" perception.** CLI bypasses chat_gate UI confirmations. Counter: cli_room can still go through `kg_dev_room_manager`'s chat_gate; only Mode A direct-manager invocation skips it. That's an explicit user choice when running with `--manager kg_dev_manager` directly.

## Open questions for discussion

1. **Single-shot default target.** I propose `kg_dev_manager` (most useful for ops). Alternative: `emi_team_manager` for full power. Or no default — require `--manager` always. **Pick one.**
2. **Authority default.** I propose 99. Alternative: lower by default (say 80) so CLI can't accidentally do high-blast-radius things; require `--root` to elevate. **Trade-off: convenience vs blast-radius.**
3. **REPL: implicit chat_gate or direct-manager?** Mode B routes through `kg_dev_room_manager` (with chat_gate confirming destructive ops). Alternative: REPL goes direct to a target manager, no gate. **The chat_gate version matches the dev console UX; the direct version is faster for someone who knows what they're doing.**
4. **Mode C scope.** Headless daemon includes dayflow + routines. Does it ALSO include outbound channels (Telegram, SMS, email)? If yes, the daemon is "full Emi minus UI." If no, it's "scheduled batch worker." **The first is more ambitious.**
5. **Streaming.** Today nothing streams token-by-token. Mode A prints one final result. Should Mode B stream the gate's chat_response as it's generated, like a real shell agent? **Adds complexity; probably v2.**
6. **Cancellation.** Ctrl-C in REPL — what does that do? Cancel the current manager invocation? Cancel and exit? Both with two presses? **Spec it.**
7. **Telemetry.** Should CLI log its turns to the same `data/logs/` files as Flask, or get its own log? **Probably the same, with a `surface=cli` tag.**

## Recommended decision path

1. Read this doc.
2. Decide on the open questions above (or punt some to "Phase 1 will inform").
3. Greenlight Phase 1 only. ~1-2 days of work.
4. Use it for a week.
5. Decide on Phase 2 + 3 from real experience.

This is a bounded, low-risk addition to the system. The asymmetry of "small effort, large optionality" is what makes it worth doing — even if Mode C never ships, Mode A pays for itself in script-ability and test-harness value.
