# Extending EmiOS

EmiOS is built to be extended without rewriting. Most extension
points are filesystem-discoverable: drop a directory in the right
place, restart Flask, the new thing is registered.

The conventions are documented as **skills** so an agent (Claude
Code, Codex CLI, or Emi's own EmiCode room) can read them on
demand and know exactly what to create. Each skill has keyword
triggers so it auto-loads when relevant.

## Extension points

| Adding a new... | Skill | Pattern |
|---|---|---|
| Agent | [`extending-emi-agents`](skills/extending-emi-agents/SKILL.md) | `app/assistant/agents/<ns>/<name>/{config.yaml, prompts/, agent_form.py?}` |
| Tool | [`extending-emi-tools`](skills/extending-emi-tools/SKILL.md) | `app/assistant/lib/tools/<name>/{tool_contract.json, <name>.py}` |
| Manager | [`extending-emi-managers`](skills/extending-emi-managers/SKILL.md) | `app/assistant/multi_agents/<name>/manager_config.yaml` |
| Routine | [`extending-emi-routines`](skills/extending-emi-routines/SKILL.md) | append to `configs/routines.json` (+ optional handler in `app/assistant/routine_handlers/`) |
| Skill | [`extending-emi-skills`](skills/extending-emi-skills/SKILL.md) | `skills/<name>/SKILL.md` |
| Camera | [`extending-emi-cameras`](skills/extending-emi-cameras/SKILL.md) | append to `configs/cameras.json` (+ analyzer agent) |
| Room | [`extending-emi-rooms`](skills/extending-emi-rooms/SKILL.md) | `app/assistant/rooms/<id>/ROOM.md` (one file, frontmatter + body) |
| Pod kind | [`extending-emi-pod-kinds`](skills/extending-emi-pod-kinds/SKILL.md) | append to `configs/pod_kinds.json` |
| Resource (static markdown) | [`extending-emi-resources`](skills/extending-emi-resources/SKILL.md) | `resources/instructions/resource_<name>.md` |

## When you ask Claude Code (or any agent) to extend Emi

The agent reads the relevant `extending-emi-*` skill, finds the
canonical example pointed at by that skill, copies the directory or
appends the entry, and tells you what to restart. No bespoke
knowledge of Emi's internals required.

A typical session:

```
You: "Add a new agent that watches for upcoming flights in my email
      and surfaces them as dayflow items 24h before departure."

Claude Code: reads extending-emi-agents → copies a canonical
agent directory → fills in your prompt + config → reads
extending-emi-tools to see if a 'find_emails_by_keyword' tool
exists → reads extending-emi-routines to schedule a daily check →
saves the files → "Restart Flask and the routine 'flight_watch'
will run daily at 06:00 in the morning window."
```

## Cross-cutting conventions

- **Restart for code changes; live-reload for config changes.**
  Adding agents, tools, managers, skills, or rooms requires a
  restart (registries load at boot). Adding routines, cameras,
  windows, or pod-kind allowlists is hot-loaded — edit the JSON,
  next refresh tick or next event picks it up.
- **Validation runs at startup.** Bad configs (missing fields, typo'd
  references, unreachable agents) are surfaced with clear errors at
  `Running agent registry validation...` early in the boot log.
- **Auto-discovery, not central registries.** New routine handlers
  via `@routine_handler()` decorator. New skills via filesystem walk.
  No central dict to forget to update.
- **Spec/status separation.** Things that ship in the repo
  (`configs/routines.json`, `configs/windows.json`, `configs/cameras.json`)
  are spec — declarative schema. Per-machine state
  (`resources/resource_routine_status.json`) is status — what's
  actually enabled on this user's machine. `git pull` doesn't clobber
  local toggles; local toggles don't push.
- **No backwards-compatibility shims.** When refactoring, update all
  callers. No legacy aliases. Exception: changes that affect database
  schemas or stored data — ask first.

## Things you can't yet drop-in extend

- **Event subscribers outside the routine system** — most should just
  become event-triggered routines (see `extending-emi-routines`),
  but the older `register_*_subscribers()` pattern still works for
  stateful services (e.g. `dayflow_scheduler` with internal debounce
  + mutex state).
- **Computed / pipeline-output resources** — `{{ resource_X }}` keys
  populated by background pipelines or scheduled compute. Static
  markdown resources ARE drop-in (see `extending-emi-resources`);
  it's just the dynamically-computed kinds that need code.

## See also

- `docs/architecture/00_OVERVIEW.md` — high-level architecture
- `docs/architecture/05_DAYFLOW.md` — autonomous workflow engine
- `docs/architecture/21_SKILLS.md` — skill-system design
- `CLAUDE.md` — repo-specific guidance for agentic coding sessions
