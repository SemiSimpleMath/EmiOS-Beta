# EmiOS Documentation

A local-first personal AI assistant. Flask + SQLite + ChromaDB, multi-agent orchestration, knowledge-graph memory, multi-transport (UI / SMS / Slack / Telegram).

If you are new, start at **[Welcome](welcome/WELCOME.md)** and read in order.

## Welcome (read first if new)

| Page | What it gives you |
|------|--------------------|
| [Welcome](welcome/WELCOME.md) | Why EmiOS exists, dev setup, recommended read order |
| [Mental Model](welcome/MENTAL_MODEL.md) | Plain-English: what's a Manager / Agent / Planner / Room / Scope / Tool |
| [Glossary](GLOSSARY.md) | A–Z definitions of every term used in the codebase |

## Architecture (deep dives, in dependency order)

| # | Page | Subject |
|---|------|---------|
| 00 | [Overview](architecture/00_OVERVIEW.md) | The layered stack from transport to service |
| 01 | [Agents](architecture/01_AGENTS.md) | LLM decision units; config + prompts + form |
| 02 | [Managers](architecture/02_MANAGERS.md) | MultiAgentManager / RoomManager — agent-orchestrating only |
| 03 | [Rooms](architecture/03_ROOMS.md) | Scoped conversation channels and the room contract |
| 04 | [Control Nodes](architecture/04_CONTROL_NODES.md) | Deterministic state machines in the agent loop |
| 05 | [Dayflow](architecture/05_DAYFLOW.md) | Autonomous daily workflow engine |
| 06 | [Pipelines & Routines](architecture/06_PIPELINES_AND_ROUTINES.md) | Step-based pipelines + scheduled routine system |
| 07 | [Tools](architecture/07_TOOLS.md) | Tool contract, registry, scope filtering |
| 08 | [KG Chat Pipeline (legacy)](architecture/08_KG_CHAT_PIPELINE.md) | Historical reference; superseded by 09 |
| 09 | [KG Pipeline (current)](architecture/09_KG_PIPELINE.md) | Bucket-per-stage chat → KG ingest |
| 10 | [Service Managers](architecture/10_SERVICE_MANAGERS.md) | RoutineManager, BackgroundTaskManager, TicketManager, AFKMonitor, etc. — non-agent services |
| 11 | [Wiki Generator](architecture/11_WIKI_GENERATOR.md) | KG → markdown vault, nightly refresh, critic |
| 12 | [Entity Cards](architecture/12_ENTITY_CARDS.md) | Per-entity profile cards (model, generator, editor, admin) |
| 13 | [KG Investigation + Mutation](architecture/13_KG_INVESTIGATION_MUTATION.md) | Self-healing loop: findings → investigator → mutator |
| 14 | [Pods (datapod) System](architecture/14_PODS.md) | URI-addressable content units, the gut, classifier |
| 15 | [emi_team + Scope](architecture/15_EMI_TEAM_AND_SCOPE.md) | The general-worker manager pattern + permission model |
| 20 | [Routines Admin UI](architecture/20_ROUTINES_ADMIN.md) | The /routines page that manages the schedule |

## Recipes (how to add things)

| Page | What it walks you through |
|------|---------------------------|
| [Add an agent](recipes/ADD_AN_AGENT.md) | Directory layout, config, prompts, form |
| [Add a tool](recipes/ADD_A_TOOL.md) | BaseTool, tool_contract.json, registration |
| [Add a manager](recipes/ADD_A_MANAGER.md) | Either fresh or derived from emi_team |
| [Add a pipeline](recipes/ADD_A_PIPELINE.md) | PipelineStep, runner, scope, routine wiring |
| [Add a routine](recipes/ADD_A_ROUTINE.md) | Pick a runner type and a policy, wire to configs/routines.json |
| [Add a room](recipes/ADD_A_ROOM.md) | Room contract, identity, policy, transport routing |

## Reference

- **Task spec contract**: see `docs/architecture/TASK_CREATION_SESSION.md` — immutable specs with frontmatter
- **CLAUDE.md** (repo root) — coding conventions and dev principles
- **README.md** (repo root) — quickstart for end-users (not contributors)

## Conventions used in these docs

- **Code paths** are repo-relative: `app/assistant/agents/wiki_writer/config.yaml`
- **File:line citations** use the format `wiki_renderer.py:142` so you can `Ctrl-click` in your editor
- **`> Note:` callouts** flag sections where the code and the design intent differ, or where something is in flight
- **Style:** terse, code-grounded, no marketing — these are docs for engineers, not promotional material
