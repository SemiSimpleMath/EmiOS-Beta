# Rudimentary coding-agent capability for Emi

Status: draft for discussion. No code yet. Companion to `cli_emi.md`.

## TL;DR

Emi has a rich agent runtime, scope/permission discipline, manager system, KG, pods — everything except the **coding-agent primitives** that Claude Code, Aider, Cursor, etc. have built their tools around. The gap is roughly: six new tools, one new agent, one new manager, plus a thought-through sandbox model. Estimate: **1.5–2.5 weeks of focused work** to get to a usable v1; another 2-4 weeks to make it actually nice.

The interesting design questions are **not** "can it do it" — the runtime is fine. They are: how much sandboxing is enough, what workspace abstraction sits beneath the agent, and how does this slot into Emi's existing room / scope / authority guards without becoming "the room that can do anything."

This doc walks through what's missing, what would be built, where the real risk lives, and how to phase it so v1 is usable without v2-v3 ever having to ship.

## Goals

1. Emi can perform **mechanical coding work** end to end: read a repo, search for symbols, edit files, run tests, commit, iterate.
2. Coding capability is **scoped to a workspace**, not the whole filesystem. Emi can't accidentally `rm -rf` your home directory.
3. **Same guards as the rest of Emi** — agents are scoped, tools require approval where appropriate, every shell call is audit-logged.
4. Usable from **CLI Emi** (Mode A or B from `cli_emi.md`) and from the master_room UI alike. Same manager, two transports.
5. **Bounded ambition.** v1 is "Emi can change a file and run a test." It is not "Emi builds a SaaS product over a weekend." That's claude-code-level and a different effort budget.

## Non-goals (for v1)

- Multi-repo orchestration, monorepo navigation at scale.
- Inline diff UI in the existing Flask front-end (terminal output is fine for v1).
- Long-running background coding ("work on this PR overnight"). Synchronous, foregrounded only.
- Browser-driving / web-app testing (separate problem; `playwright_manager` is for that).
- Replacing Claude Code on this same machine. They're not exclusive; the goal is "Emi can also do this when convenient," not "Emi replaces it."

## The tool gap — what's missing today

Current Emi text-handling tools (the relevant set):

- `read_text_file` — read a file. Exists.
- `write_text_file` — overwrite a file. Exists. Overwrite-only is the wrong primitive for iterative coding.
- `append_text_file` — append. Exists. Wrong primitive too.

Missing primitives that every coding agent needs:

| Tool | Purpose | Existing Emi tool? |
|---|---|---|
| `edit_file` | Surgical replace of `old_string` → `new_string` in one file. The primary edit primitive. | No |
| `glob_files` | Find files by glob pattern (`src/**/*.py`). | No |
| `grep_code` | Search files by regex pattern, return matches with line numbers + context. | No |
| `list_dir` | List directory contents with type/size/mtime. | No |
| `run_shell` | Execute a shell command in a sandboxed working directory; return stdout/stderr/exit_code. | No |
| `read_file_lines` | Read specific line range of a file (so an agent doesn't have to load 5,000-line files). | Partial — `read_text_file` reads whole file. |

Plus a few smaller helpers worth having:

- `git_status`, `git_diff`, `git_log` — could be `run_shell` invocations, but a typed wrapper is friendlier and easier to scope-gate. Could go via `run_shell` in v1.
- `apply_patch` — apply a unified diff. Useful when an LLM thinks in patch terms rather than search-and-replace. v2.

**Build cost per tool (with Emi's existing tool framework):** roughly half a day each. Each needs a `tool_contract.json`, two prompt templates (`<name>_description.j2`, `<name>_args.j2`), a Pydantic form, the implementation Python file, and registration. ~3-4 days for the six core tools.

## The agent gap — coder agent + discipline

Emi's existing planners are generalists. A coder needs different discipline:

- **Read before edit.** Never modify a file you haven't read first.
- **Search before guess.** When the user names a function or module, find it before assuming what it does.
- **Atomic edits.** One concept per edit. Reject "while I'm here, also fix this other thing" instincts.
- **Test after change.** If the workspace has tests, run the relevant ones. If they fail, read the failure and fix root cause.
- **Surgical scope.** Touch only files that trace to the user's request. Don't reformat adjacent code.
- **No invented APIs.** Verify symbols exist (via grep) before calling them.
- **Clean up own mess.** If your changes orphaned an import or variable, remove it. Don't leave dead code.

These are **prompt-level disciplines** — they go in the coder agent's system.j2. Most of them are already articulated in this repo's `CLAUDE.md`; we'd lift them.

**What the coder agent needs in its config:**

```yaml
name: coder::planner
class_name: Planner
llm_params:
  llm_provider: "openai"
  engine: "gpt-5.4"
  model_tier: "smart"

allowed_tools:
  - read_text_file
  - read_file_lines
  - edit_file
  - write_text_file
  - glob_files
  - grep_code
  - list_dir
  - run_shell

user_context_items:
  - date_time
  - task
  - information
  - recent_history
  - workspace_summary    # NEW — see below

system_context_items:
  - resource_coding_discipline    # NEW — the discipline rules
  - tool_descriptions
```

**Build cost: 1-2 days.** The hard part is the prompt. Quality emerges from iteration on real coding tasks.

## Manager structure — `coder_manager`

Mirror the existing `kg_dev_manager` shape:

```
multi_agents/coder_manager/
└── config.yaml          # planner + tool loop + final answer
```

Outer chat-gate version (parallel to `kg_dev_room_manager`):

```
multi_agents/coder_room_manager/
└── config.yaml          # chat_gate + router → coder_manager
```

Plus a wrapping `coder_manager` tool so other managers (emi_team) can delegate:

```
lib/tools/coder_manager/
├── coder_manager.py
├── tool_contract.json
├── tool_forms/
└── prompts/
```

This is the same template we just walked through for kg_dev. ~half day.

## Workspace and sandbox model

The single biggest design decision and the largest risk. **Where does Emi's coder agent operate?**

### Option 1 — Single fixed workspace

```
~/emi_workspace/    # one directory; coder always works inside this.
```

- Coder bootstraps with cwd=`~/emi_workspace`. All tool paths resolved relative to it.
- `run_shell` refuses to operate outside this tree (path canonicalization, prefix check).
- `edit_file` / `write_text_file` likewise.
- Pros: simplest. Easy to reason about blast radius. Good for v1.
- Cons: only one project at a time. To work on EmiAi itself you'd have to clone or symlink.

### Option 2 — Multiple registered workspaces

```
~/.emi/workspaces.json
{
  "emi": "/home/jukka/EmiAi_sqlite",
  "ring_viewer": "/home/jukka/projects/ring_viewer",
  "scratch": "/home/jukka/emi_workspace"
}
```

- Coder takes a `workspace` argument; resolves to the corresponding root.
- Switching workspace requires explicit command (no implicit cwd from tool args).
- Pros: realistic for multi-project use. EmiAi itself is one of the workspaces.
- Cons: more code, slightly more complex permission model.

### Option 3 — Per-task ephemeral workspace

For each top-level task, the coder gets a fresh git worktree at `/tmp/emi_workspace/<task_id>`. After the task, the user reviews the worktree and merges back.

- Pros: total isolation. No way to corrupt the main repo without a deliberate `git push`. Excellent for autonomous "let it work for an hour and review" mode.
- Cons: heavy. Setup/teardown per task. Most users want the changes in the main repo immediately.

**Recommendation:** v1 = Option 1 (single workspace, configured at startup). v2 = Option 2 (registered workspaces with explicit switching). Option 3 is a future hardening for daemon mode.

### `run_shell` sandbox specifics

This tool is where 80% of the safety thinking lives.

What it should do:
- Resolve `working_directory` to an absolute path; reject if outside the configured workspace.
- Reject command strings containing destructive ops by default unless `--allow-destructive` is set: `rm -rf /`, `:>` redirects to system paths, `dd if=...`, etc. (Soft heuristic, not a sandbox replacement.)
- Wall-clock timeout (configurable, default 60s).
- Capture stdout + stderr; cap output at, say, 100 KB and report truncation.
- Capture exit code. Non-zero is not an error per se — agents should read it.
- No interactive stdin. If a command tries to prompt, it hangs and the timeout fires.
- Audit-log every invocation: command, cwd, exit code, output truncated to N chars. Goes to `kg_revision_log` or a new `shell_invocation_log`.

What it should NOT do:
- Pretend to be a real sandbox. Process-level isolation requires Docker / firejail / bubblewrap, which is a significant build. Path-prefix checks + heuristic blocklist + timeout is "harm-reduction," not "sandboxed." That's still a real improvement over no constraint, but we should be honest.

For real isolation in a future phase: spin the coder's `run_shell` in a Docker container with the workspace bind-mounted read-write and the rest of the host filesystem hidden. ~3-5 day project on top of v1.

**v1 sandbox effort: 1-2 days** for path-prefix enforcement, blocklist, timeout, audit log. v2 (Docker isolation): 3-5 days separate.

## Iteration loop — the actual value

A coding agent's value comes from the inner loop:

```
read → think → edit → test → read failure → re-edit → test → commit
```

Today Emi planners do `read → action → read result → action`. The coder agent's variant is the same shape with three additions:

1. **The agent expects multiple actions per task.** A bug fix is rarely one tool call. The planner's `max_cycles` should be generous (~30). `kg_dev::planner` already has 30; same number works.
2. **Test invocation discipline.** After each non-trivial edit, the agent should consider running tests. The system prompt encodes this. The actual test command is workspace-specific (`pytest`, `npm test`, `cargo test`) — the prompt frames "if this workspace has tests, run them," and the user/agent figures out which.
3. **Failure-driven re-edit.** When tests fail, the agent reads the failure output, identifies the relevant file/line, edits, re-runs. This is mostly emergent from the prompt + the tools being right; no new infrastructure.

What we DON'T need to build (because it emerges from the existing Planner class):
- The action-budget loop. Already there.
- Tool-result handling. Already there.
- Final answer synthesis. Already there.

What we DO need:
- A `workspace_summary` context item that gives the agent its starting picture: cwd, top-level dirs, presence of `package.json` / `pyproject.toml` / `Cargo.toml`, recent git log. ~half day to write.

## UI thoughts (deferred)

For v1, terminal output is fine. CLI Emi (Mode A or B) is the primary entry point.

For later, the master_room UI could grow:
- A "diff viewer" panel that renders the coder's changes since session start. Accept/reject buttons.
- A "shell log" panel that streams `run_shell` output.
- A workspace switcher.

This is real frontend work and we should defer until v1 is proven. ~1 week if pursued seriously.

## Phasing

### Phase 1 — minimum viable (5-7 days)

1. Build the six core tools (`edit_file`, `glob_files`, `grep_code`, `list_dir`, `read_file_lines`, `run_shell`). ~3 days.
2. Sandbox basics for `run_shell` — path prefix, blocklist, timeout, audit. ~1 day.
3. Coder agent (`coder::planner`) + system prompt with discipline. ~1 day.
4. `coder_manager` config + manager-tool wrapper. ~half day.
5. CLI entry: `emi-cli --manager coder_manager "fix the bug in X"`. ~half day (assumes `cli_emi` Mode A is shipped first).
6. Real-world dogfooding: have it do 10 small tasks on this repo. Iterate on prompt and tool descriptions. ~1-2 days.

After Phase 1: Emi can do mechanical coding work in CLI mode against a single workspace. Good enough for "fix this typo," "rename this function across the repo," "add a route that does X," "refactor this module." Not good enough for "build a Ring viewer from scratch" — that needs Phase 2.

### Phase 2 — usability + multi-workspace (3-5 days)

1. Multiple registered workspaces.
2. `apply_patch` tool for unified-diff workflows.
3. Git wrappers (`git_status`, `git_diff`, `git_log`) as typed tools — not because shell can't, but because typed wrappers compose better in agent reasoning.
4. `workspace_summary` enrichments (recent commits, dirty files, branch name).
5. Better failure-recovery prompt patterns — if a build fails 3 times in a row, the agent should bail out and ask the user instead of looping.

After Phase 2: Emi can drive moderately complex coding tasks. Enough to attempt "build a Ring viewer page in Flask" because all the pieces exist (Flask templates, routes, JS files are all just-files-to-edit) and the agent has good enough discipline.

### Phase 3 — UI + isolation (1-2 weeks)

1. Diff viewer in master_room UI.
2. `run_shell` Docker isolation.
3. Per-task ephemeral worktrees (Option 3).
4. Streaming output for long-running commands.

Defer until Phase 1 + 2 prove themselves. Most of this is nice-to-have, not need-to-have.

## How this plugs into Emi's guards

Same three layers as everything else:

1. **Room policy.** `coder_room/policy.json` (new) sets authority. Default: 99 for the dev console / CLI. For master_room delegation, the inner `coder_manager` would be approval-gated by emi_team's planner.
2. **Manager scope_contract.** `coder_manager` allowed_tools = the six coding tools, nothing else. No `send_email`, no `kg_*`, no `playwright_manager`. The coder is a coder, not a generalist.
3. **Tool-level approval.** `run_shell` should have `requires_approval: true` by default for any non-`master_room` / non-`coder_room` actor. This prevents Telegram-routed requests from triggering shell execution. Toggleable via room policy if you trust a given surface.

The chat_gate in front of `coder_manager` confirms before destructive operations exactly the way the kg-dev gate does — same pattern, different vocabulary.

## What CLI Emi gets you on top of this

Most of the time you'd actually use this, you're at a terminal anyway. So the natural entry point is CLI:

```
$ emi-cli --manager coder_manager "add a /ring route that lists recent snapshots"
[planner] action 1/30: glob_files src/routes/**/*.py
[planner] action 2/30: read_text_file app/routes/__init__.py
[planner] action 3/30: read_file_lines app/routes/wiki_viewer.py 1 50
[planner] action 4/30: edit_file app/routes/__init__.py (register new blueprint)
[planner] action 5/30: write_text_file app/routes/ring_viewer.py
[planner] action 6/30: run_shell .venv/Scripts/python.exe -m pytest app/test/test_routes.py -k ring
[planner] action 7/30: edit_file app/routes/ring_viewer.py (fix typo)
[planner] action 8/30: run_shell .venv/Scripts/python.exe -m pytest app/test/test_routes.py -k ring
[planner] return_control
Added /ring route with snapshot listing. Created app/routes/ring_viewer.py and registered the blueprint. Tests passing.
```

This is what claude-code-style work looks like, on Emi's runtime, against your repo.

## Risks

- **Shell execution blast radius.** Heuristic blocklists are not real sandboxes. Single biggest risk in v1. Mitigations: workspace path prefix, timeout, audit log, `requires_approval` for non-trusted rooms. Long-term: Docker isolation.
- **Agent quality.** Coding agents need iteration on prompts to be reliably useful. v1 will be mediocre and improve over time. Plan for that — don't ship and forget.
- **Maintenance vs Claude Code.** Claude Code has dedicated team-years on its tools. Emi's coder will always lag. The point isn't to compete; it's to have a workable in-house coder for tasks that involve Emi-specific context (KG, pods, dayflow), where dropping into a separate tool is friction.
- **Drift between coder agent and the rest of Emi.** Once the coder can edit Emi's own code, it's tempting to let it self-modify. We should set a discipline: the coder edits user workspaces; it does not edit `app/assistant/` of the running Emi process. (Different workspace; if you point it at EmiAi as a workspace, fine — but be aware.)
- **Test loop quality.** Without good tests in a workspace, the coder works without a feedback signal. Fine for "edit this typo," dangerous for "refactor this module." The agent's prompt should articulate "if there are no tests for this code path, write a test first."

## Open questions

1. **What's the v1 workspace default?** Single workspace at `~/emi_workspace`? Or default to wherever the user is when they run `emi-cli`? **Pick one.**
2. **`run_shell` approval policy.** Default `requires_approval: false` for cli/master_room and `true` for all other surfaces? Or always `true` and you opt out per session? Trade-off: convenience vs blast-radius.
3. **Do we use the existing `task` system for sub-task tracking?** Claude Code uses `TodoWrite` to track multi-step plans. Emi has a task system (`tasks/specs/`, `tasks/runs/`). Reuse it? Or have the coder use a lightweight in-memory checklist like the existing planner pattern?
4. **Test command discovery.** Does the coder agent guess (`pytest` for python, `npm test` for js)? Or does the workspace declare it (`workspace.json` with `"test_command": "pytest"`)? The latter is more robust; the former is friendlier in the common case.
5. **Editing the running Emi.** If `coder_manager` is invoked from CLI Emi while Flask Emi is running, and the coder edits `app/assistant/...`, does Flask hot-reload? Crash? Should we explicitly forbid the coder from touching the workspace it's running inside? **Probably forbid for v1.**
6. **Pair with `kg_dev_manager`?** A real coding task in this repo often needs to query the KG for examples or check pod tags. Should `coder_manager` have `kg_dev_manager` and `pod_search` in its allowed_tools? Or stay minimal and let the user route those manually? Probably yes — being able to grep AND ask the KG is a unique Emi superpower.

## Recommended decision path

1. Read this doc + `cli_emi.md`.
2. Decide on the open questions (or punt to "Phase 1 will inform").
3. Greenlight Phase 1 (5-7 days). This is the part where you find out if Emi-as-coder is useful for you.
4. Use it for two weeks on real tasks. Track which prompts/tools fall short.
5. Decide Phase 2 + 3 from real experience.

The Ring-viewer test case is a good Phase-1.5 milestone: build the route + page using `coder_manager`, see how many actions it takes, what it gets wrong, where the prompts need work. If it's painful at Phase 1, the post-mortem tells you what Phase 2 needs to fix.
