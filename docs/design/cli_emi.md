# CLI Emi — design proposal

Status: draft for discussion. No code yet.

## TL;DR

**The right mental model: CLI isn't a separate runtime. It's just another room.** `cli_room` has a policy, a manager, a scope, a chat history — exactly like master_room or kg_dev_room. The thing that's "CLI" is the *transport* the user chose to talk to that room with. Two transports work, independently:

- **Browser / Flask UI.** A `/cli` page renders a terminal-styled chat against `cli_room`. No new runtime, no new bootstrap, no daemon. Same Flask, same socketio, just a different room being addressed and a different CSS for the page.
- **Actual command line.** A Python entry point bootstraps DI without Flask, posts a Message to `cli_room`, prints the result. No browser involved.

Both transports route to the same `cli_room` and the same manager runs in both cases. So "make CLI" is two small projects done independently:

1. **Build `cli_room`** (room policy + chat-styled manager). One small build. Available immediately in the Flask UI as a `/cli` page.
2. **Build the headless transport** (terminal entry point that bypasses Flask). A second small build. Worthwhile when you want to script Emi from a shell.

A third axis — **headless daemon mode** (no UI, no CLI prompt, Emi-as-service) — IS genuinely a different runtime question, because that's about whether Flask runs at all and whether dayflow ticks autonomously. We treat that separately from the room/transport story.

This doc walks through all three concerns and proposes phases that ship `cli_room` first (smallest, most value), terminal-transport second, daemon last.

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

## Two axes: transport vs room

Two questions to answer separately:

1. **Which room is the user talking to?** That determines the manager, the chat_gate (or absence of one), the discipline, the allowed tools, the authority. Today we have master_room, kg_dev_room, slack rooms, telegram rooms, etc. Adding `cli_room` is a small additive change.
2. **What transport delivers the message?** Today: Flask HTTP/WebSocket. We can add: terminal stdin/stdout. Both can address any room.

The matrix:

| | Browser/Flask | Terminal |
|---|---|---|
| **master_room** | works today (the UI you use) | could work — if you wanted to chat with master_room from a shell, the entry point lets you |
| **kg_dev_room** | works today (the `/kg-dev` page we just built) | could work — same |
| **cli_room** (new) | a `/cli` page rendered terminal-style | a `emi` shell command |

Both transports → cli_room is the "two ways to use the same dev console" path. Same manager, same chat_gate (if any), same scope, same audit log. The user picks the transport based on whether they're in a browser tab or a shell.

## What `cli_room` actually is

A new room directory: `app/assistant/rooms/cli_room/`. Six files (policy, permissions, access, identity, conversation, room_context). Mirrors kg_dev_room's shape closely. Key choices:

- `manager_name`: probably `kg_dev_room_manager` for v1 (the chat_gate-fronted KG console). cli_room could swap to a different manager once the coder agent ships, or even have a router up front that picks "kg work? coding work? something else?"
- `surface: "cli"` — distinguishes outbound rendering. Flask UI uses different CSS for `surface=cli` (monospace, no avatars, no chat bubbles); terminal transport renders to stdout. Both honor "this is a CLI room, format accordingly."
- `authority_level: 99` — developer console.
- `history.scope: "session"` — the chat_gate's `recent_history` injection picks up prior turns within the session.

That's the entire room build. ~10 lines of JSON, copy-pasted from kg_dev_room with minor edits.

The `/cli` Flask page is also small: a template that renders cli_room's chat history with terminal styling, posts to the existing `/process_request` endpoint, room_id=cli_room. ~50 lines of HTML+JS, ~20 lines of route. Half-day total.

**At this point you can use cli_room from a browser today.** It doesn't unlock anything new yet (kg_dev_room already does this), but the room exists, the routing is wired, and adding the terminal transport later is purely additive.

## Three CLI shapes (transport + behavior combinations)

### Shape 1 — `/cli` page in Flask UI

What it is: a browser tab styled like a terminal. Same back-end as the rest of Flask. Routes user input to cli_room → cli_room's manager → response → render.

What it gets you: terminal-flavored interaction without a separate process. No bootstrap juggling, no SQLite-locking concerns, no daemon. Whenever Flask is running, this page is available.

Effort: half a day. Mostly CSS and a thin route.

### Shape 2 — terminal entry point (`emi` shell command)

What it is: a Python entry point that bootstraps DI without Flask, posts a Message to cli_room (or any room you specify), prints the result, exits. Or runs a stdin-loop REPL persisting transcript to JSONL.

What it gets you: scriptable Emi — pipes, shell composition, CI integration, no browser required.

Effort: 1-2 days. Bootstrap-without-Flask is the largest piece. The room itself already exists from Shape 1, so this is purely about transport.

Sub-shapes inside Shape 2:

- **Single-shot.** `emi "find duplicate Person nodes"` — one message, one result, exit. Best for scripting.
- **REPL.** `emi` with no args drops into a prompt. Each line is a Message. Persists transcript. Ctrl-C cancels current turn; Ctrl-D exits.

Both are the same code; REPL is "single-shot in a loop with stdin input."

### Shape 3 — headless daemon

What it is: Emi running as a service with no UI and no interactive prompt. Dayflow ticks. Routines run on schedule. Inputs come from existing ingress (email, telegram, etc.). Outputs go to outbound channels (telegram, sms, log file).

This is genuinely different from Shapes 1 and 2 because it's about whether Flask runs at all, not which room is addressed. It's "Emi the autonomous service" rather than "Emi the responsive interface."

Effort: 2-3 days, separate from the room/transport work.

Use cases: home-server Emi on a Pi, VPS deployment, multi-machine setups.

**Phasing recommendation:** Shape 1 first (half day, available immediately in Flask). Shape 2 second (1-2 days, scriptable Emi). Shape 3 only if a real deployment target emerges.

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

### Shape 1 — `/cli` page in Flask UI

Net-new:

```
app/assistant/rooms/cli_room/
├── policy.json
├── permissions.json
├── access.json
├── resource_identity.json
├── resource_conversation.json
├── resource_room_context.json
└── resource_safety.json

app/templates/cli.html       # terminal-styled page
app/static/css/cli.css       # monospace, no chat bubbles
app/static/js/cli.js         # send/receive against /process_request
app/routes/cli.py            # GET /cli  → render template
```

That's it. No new runtime. No new transport. Same Flask, same socketio, same `/process_request` endpoint. The only thing different about cli_room from kg_dev_room is what its manager allows and how the page renders.

### Shape 2 — terminal entry point

Net-new on top of Shape 1:

```
app/cli/
├── emi.py              # entry point: arg parsing, dispatch, stdin loop
├── cli_session.py      # transcript persistence (REPL mode)
├── cli_event_sink.py   # subscribes to event_hub, prints progress to stdout
└── README.md

app/bootstrap_cli.py    # DI bootstrap that skips Flask things

emi (or emi.bat)        # repo-root shell wrapper
```

About 250 lines of new code total.

### Shape 3 — headless daemon

Net-new on top of Shapes 1 and 2:

```
app/cli/
├── emi_daemon.py       # daemon entry point, no stdin
└── daemon_config.toml  # example config

# plus pidfile / signal handling / outbound routing config
```

About 200 more lines, plus operational glue.

### What's removed

Nothing. Flask remains the primary front-end. The new transports are additive.

### What we touch (small edits, all shapes)

- `app/bootstrap.py` — refactor to extract Flask-specific initialization into `bootstrap_flask()` so `bootstrap_cli()` can reuse the rest. Trivial.
- `event_hub` — ensure it can have multiple sinks (it probably already does for socketio + logging; CLI is just another sink).
- `_resolve_reply_to` (in chat_task_router_node etc.) — handle `surface == "cli"` to route ack messages to stdout (Shape 2) or to a cli-styled emit (Shape 1).

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

Critical constraint: SQLite is single-writer. If both Flask and the terminal entry point target `emi.db` concurrently:

- **Reads are fine** in WAL mode (which the project uses).
- **Writes will conflict** under load. The user's existing rule (memory: `feedback_no_db_lock_over_llm`) already names this.

So:

- **Shape 1 (`/cli` page in Flask UI):** no conflict. Same Flask process, no contention.
- **Shape 2 (terminal one-shot):** safe alongside running Flask if the operation is short and read-mostly. Locking conflict is possible during writes (e.g. `kg_merge_nodes`); the manager would retry or fail.
- **Shape 2 (terminal REPL):** same caveat, but more likely to do bursts of writes. Practical advice: "stop Flask before opening a REPL for write-heavy work."
- **Shape 3 (daemon):** mutually exclusive with Flask on the same DB. The daemon IS Emi for that machine.

A future hardening: a small lock file (`emi.db.cli.lock`) that the terminal transport takes on launch for write-intensive sessions; Flask's `process_request` can check for it and refuse to start a new turn while the CLI holds it. Out of scope for v1.

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

**Phase 1 — Shape 1 (`/cli` page in Flask UI, half day):**

1. `app/assistant/rooms/cli_room/` — six JSON files, copy-edited from kg_dev_room.
2. `app/routes/cli.py` — GET /cli renders the template.
3. `app/templates/cli.html` + `app/static/css/cli.css` — terminal styling.
4. `app/static/js/cli.js` — POST to existing `/process_request` with room_id=cli_room.
5. Verify the existing chat_gate / scope / approval guards work unchanged.

Ship same-day. Available immediately whenever Flask is running. This validates that "cli is just a room" works and doesn't unlock anything new yet — but the room exists, the routing is wired, and Shape 2 layers on cleanly afterward.

**Phase 2 — Shape 2 (terminal transport, 1-2 days):**

1. `app/bootstrap_cli.py` — DI bootstrap without Flask.
2. `app/cli/emi.py` — entry point; one-shot mode (`emi "task"`) and REPL mode (`emi` with no args).
3. `app/cli/cli_session.py` — JSONL transcript persistence (REPL only).
4. `app/cli/cli_event_sink.py` — event_hub subscriber printing progress to stdout.
5. `_resolve_reply_to` handles `surface=cli` for stdout output.
6. `--room`, `--manager`, `--orchestrator`, `--authority`, `--json` flags.
7. Smoke test: invoke `kg_dev_manager` from a shell; verify result matches Flask output.

Ship after Phase 1. Adds the scriptable / pipeable Emi.

**Phase 3 — Shape 3 (headless daemon, 2-3 days):**

1. `app/cli/emi_daemon.py` — daemon entry point.
2. Daemon config file format (toml or yaml).
3. Disable Flask routes in bootstrap; enable scheduler / dayflow ticking.
4. Outbound routing — telegram / SMS / log file as default reply destinations (no UI to receive them).
5. Process management — pidfile, signal handling, graceful shutdown.
6. SQLite single-writer enforcement — refuse to start if another process holds the lock.

Ship only if there's a concrete deployment target. Phase 3 is real ops work.

## Use cases that justify the effort

For each, mark which shape covers it:

- **"Browser-based terminal-styled chat against the dev console"** — Shape 1. The `/cli` page. No new transport needed.
- **"Find every duplicate Person node and propose merges from a shell"** — Shape 2 (one-shot). Single command, structured output, pipe to a review tool.
- **"Investigate the KG interactively for an hour from a terminal"** — Shape 2 (REPL). Session history persisted to JSONL.
- **"Run the nightly KG maintenance batch"** — Shape 2 (cron'd one-shot) or Shape 3 (daemon ticks naturally).
- **"Build an integration test suite that exercises real managers"** — Shape 2 (one-shot). CLI is the test harness.
- **"Run Emi on a Raspberry Pi at home, no monitor, talks to Telegram"** — Shape 3.
- **"Pipe email content into Emi, get a structured summary, pipe to next tool"** — Shape 2 with `--json`.
- **"Debug why a manager's prompt is producing weird output"** — Shape 1 or 2. Both walk the same runtime; pick by what's faster to launch.

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
