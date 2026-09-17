# EmiCode room (EmiOS-mediated coding agent) — archived 2026-09-16

**DO NOT DELETE.** Snapshot of the first EmiCode design: a chat room where EmiOS
acted as a context curator in front of a one-shot Claude Code CLI call. The
archive structure mirrors the source tree, so `git log --follow` on any file
inside traces back to its original location.

Replaced by a real embedded terminal — `app/assistant/terminal/` plus the
rewritten `/emi-code` page — which runs the interactive `claude` CLI in a PTY
and streams it to the browser over SocketIO.

## Why archived

The mediated design put EmiOS between the user and the coding agent: `chat_gate`
classified the message, `EmiCodeChatTaskRouterNode` synthesized the tool call,
`claude_code_invoke` shelled out to `claude --print --output-format
stream-json`, and `skill_curator` hand-assembled a context bundle into the
prompt. Everything the coding agent said came back through a response
formatter.

That layer bought nothing the CLI does not already do better:

- **Context curation was redundant.** `skill_curator` stuffed CLAUDE.md into
  every cold-start prompt; the CLI auto-loads CLAUDE.md itself. The keyword
  match over `docs/architecture/*.md` was a crude stand-in for skills, which
  the CLI supports natively via `.claude/skills/`.
- **One-shot `--print` could not hold a conversation.** Multi-turn was faked
  with `--resume` over a session id in `data/emi_code/sessions.json`. Nothing
  cleared that id when a resume failed, so once the stored transcript aged out
  the room was permanently wedged: every turn hit `No conversation found with
  session ID` and returned a tool error. It had been in that state since
  2026-05-06.
- **It was read-only by construction** (`--allowed-tools "Read,Glob,Grep"`,
  plus a room policy and a manager `scope_contract` that both refused
  mutators), so it could propose changes but never make one.
- **300s timeout.** The only real coding task it was ever given — the Ring
  integration UI, 2026-05-06 02:13 — timed out at 02:18 without producing
  anything.

Total lifetime traffic: two conversations on 2026-05-05/06, both about Ring
cameras. Nothing since.

## What is archived

| Path | What it was |
|---|---|
| `app/assistant/rooms/emi_code_room/` | Room identity, policy (authority 60, `transformational: false`), scope |
| `app/assistant/multi_agents/emi_code_room_manager/` | Manager wiring chat_gate → router → tool caller → formatter |
| `app/assistant/agents/emi_code/chat_gate/` | Code-task vs chitchat classifier |
| `app/assistant/control_nodes/emi_code_chat_task_router_node.py` | `EmiCodeChatTaskRouterNode` (subclass of `KgDevChatTaskRouterNode`, which stays live) |
| `app/assistant/lib/tools/claude_code_invoke/` | The tool: CLI runner, stream-json parser, session store, skill curator |
| `app/templates/emi_code.html`, `app/static/js/emi_code.js`, `app/static/css/emi_code.css` | The chat-transcript UI |
| `data/emi_code/sessions.json` | The stale session id (`3c3a51bf-…`, dead since May) |
| `app/assistant/tests/tool_tests/test_claude_code_invoke.py` | Tested the archived tool exclusively, so it moved with it |

`app/assistant/tests/non_agent_tests/test_tracked_room_scope_yaml.py` stays
live: it covers five rooms, of which `emi_code_room` was one. Its golden entry
and the two id lists that named it were scrubbed in the same commit; the other
four rooms are unaffected (7 tests still pass). Restoring the room means
putting that golden block back — `git log -p` on that file has the exact
values.

## What did NOT move

- The `/emi-code` route and `emi_code_bp` blueprint — rewritten in place to
  serve the terminal. The two menu links (`_top_bar_dashboards_menu.html`,
  `_top_bar_dev_menu.html`) keep working.
- `KgDevChatTaskRouterNode`, the parent class — still used by `kg_dev_room`.
- `unified_log_2026` rows for `room_id='emi_code_room'` — conversation history
  is kept as a record.

## Restoration

Path-flip the tree back and re-register: the room and manager are discovered
by directory, the tool by `ToolRegistry` import scan, and the control node by
class name in the manager config. The old `emi_code.html` / `.js` / `.css`
must also be restored over the terminal versions, since those filenames are
reused. Note that a restored room resumes wedged unless
`data/emi_code/sessions.json` is deleted first.
