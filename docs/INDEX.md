# EmiOS Documentation

A local-first personal AI assistant. Flask + SQLite + ChromaDB, multi-agent orchestration, knowledge-graph memory, multi-transport (UI / SMS / Slack / Telegram).

If you are new, start at **[Welcome](welcome/WELCOME.md)**, then read [Overview](architecture/00_OVERVIEW.md).

## Welcome (read first if new)

| Page | What it gives you |
|------|--------------------|
| [Welcome](welcome/WELCOME.md) | Why EmiOS exists, dev setup, recommended read order |
| [Mental Model](welcome/MENTAL_MODEL.md) | Plain-English: Manager / Agent / Planner / Room / Scope / Tool |
| [Glossary](GLOSSARY.md) | A–Z definitions of every term used in the codebase |

## Architecture

### Core runtime
| # | Page | Subject |
|---|------|---------|
| 00 | [Overview](architecture/00_OVERVIEW.md) | The layered stack + a map of every subsystem |
| 01 | [Agents](architecture/01_AGENTS.md) | LLM decision units; config + prompts + form + runtime services |
| 02 | [Managers](architecture/02_MANAGERS.md) | MultiAgentManager — the agent-orchestrating loop |
| 03 | [Rooms](architecture/03_ROOMS.md) | Scoped conversation channels; the single-`ROOM.md` contract |
| 04 | [Control Nodes](architecture/04_CONTROL_NODES.md) | Deterministic nodes in the agent loop; ToolCaller |
| 07 | [Tools](architecture/07_TOOLS.md) | Tool contract + registry + the four-layer access gate |
| 10 | [Service Managers](architecture/10_SERVICE_MANAGERS.md) | RoutineManager, BackgroundTaskManager, TicketManager, … (non-agent services) |
| 17 | [Service Layer](architecture/17_SERVICE_LAYER.md) | DI, EventHub, Blackboard, ResourceManager; two-phase bootstrap |
| 18 | [Transports](architecture/18_TRANSPORTS.md) | UI/SMS/Slack/Telegram adapters + the Room Session Manager |

### Autonomy & scheduling
| # | Page | Subject |
|---|------|---------|
| 05 | [Dayflow](architecture/05_DAYFLOW.md) | Autonomous daily workflow engine + item lifecycle |
| 06 | [Pipelines & Routines](architecture/06_PIPELINES_AND_ROUTINES.md) | Step pipelines + the per-file routine system (triggers/on_error) |
| 24 | [Routine Inventory](architecture/24_ROUTINE_INVENTORY.md) | What every routine in `configs/routines/public/` does |
| 20 | [Routines Admin UI](architecture/20_ROUTINES_ADMIN.md) | The `/routines` page that manages the schedule |
| — | [Subconscious](architecture/SUBCONSCIOUS.md) | Concerns register, proactive outreach, digest, proposer/arbiter lanes |

### Memory & knowledge
| # | Page | Subject |
|---|------|---------|
| 09 | [KG Pipeline](architecture/09_KG_PIPELINE.md) | Bucket-per-stage chat → KG ingest + the promoter |
| 13 | [KG Mutator Tools](architecture/13_KG_MUTATOR_TOOLS.md) | Typed mutator handlers + `kg_revision_log` audit |
| 22 | [KG Health](architecture/22_KG_HEALTH_COMPONENTS.md) | Self-healing loop: scans → investigate → execute → verdicts |
| 23 | [Node Importance](architecture/23_NODE_IMPORTANCE.md) | Edge LLM-rating + deterministic node-importance derivation |
| 11 | [Wiki Generator](architecture/11_WIKI_GENERATOR.md) | KG → per-entity wiki, growth, synthetic-fact drain |
| 12 | [Entity Cards](architecture/12_ENTITY_CARDS.md) | Per-entity "what's true now" cards (v2: sections/bullets, critic, refresh) |
| 16 | [Belief Engine](architecture/16_BELIEF_ENGINE.md) | Confidence-tracked beliefs: evolve-in-place, decay, dedup, tagging, retrieval |
| 14 | [Pods](architecture/14_PODS.md) | URI-addressable artifacts; gut, classifier, fetch, scope+authority gate |
| 14b | [Pods: media lifecycle](architecture/14b_PODS_MEDIA_LIFECYCLE.md) | End-to-end: image upload → pod → KG edge → "find and email" |
| 19 | [Resources](architecture/19_RESOURCES.md) | Scope-gated + dynamic context resources agents read |

### Permissions & capabilities
| # | Page | Subject |
|---|------|---------|
| — | [Scope](architecture/SCOPE.md) | **Canonical** scope reference: the four-layer gate, loader, courier |
| 15 | [emi_team + Scope](architecture/15_EMI_TEAM_AND_SCOPE.md) | The general-worker manager pattern + the ceiling narrowing model |
| 21 | [Skills](architecture/21_SKILLS.md) | Injected capabilities gated by `requires_scope` / `scope_gate` |
| — | [Secrets & Accounts](architecture/SECRETS_ACCOUNTS.md) | Locked secrets model; accounts as scope-gated dynamic resources |

### Subsystem references
| Page | Subject |
|------|---------|
| [Meal Planning](architecture/MEAL_PLANNING.md) | The meal subsystem end-to-end (belief lane, distiller, arbiter) |
| [KG Pipeline diagram](architecture/diagrams/00_KG_PIPELINE_OVERVIEW.md) | One-screen visual of the KG ingest stages |

## Recipes (how to add things)

| Page | What it walks you through |
|------|---------------------------|
| [Add an agent](recipes/ADD_AN_AGENT.md) | Directory layout, config.yaml, prompts, form |
| [Add a tool](recipes/ADD_A_TOOL.md) | tool_contract.json, prompts, registration, the access gate |
| [Add a manager](recipes/ADD_A_MANAGER.md) | Fresh or derived from emi_team; flow_config + scope_contract |
| [Add a pipeline](recipes/ADD_A_PIPELINE.md) | Step protocol, runner, registry, routine wiring |
| [Add a routine](recipes/ADD_A_ROUTINE.md) | Pick a runner + trigger/policy; one file under `configs/routines/public/` |
| [Add a room](recipes/ADD_A_ROOM.md) | The single-`ROOM.md` contract + `scope.yaml` |

## Reference

- **Task spec contract**: `docs/architecture/TASK_CREATION_SESSION.md` + `TASK_SKILL_DESIGN.md`
- **Prompt style**: `docs/prompt_style_guide.md`
- **CLAUDE.md** (repo root) — coding conventions and dev principles
- **README.md** (repo root) — quickstart for end-users

## Design notes & historical

These are kept for context but are **not** current-state references (each carries a banner):
- [SCOPE_AUDIT](architecture/SCOPE_AUDIT.md), [SCOPE_REFACTOR](architecture/SCOPE_REFACTOR.md) — the scope investigation + refactor plan (shipped via the overlay path; see SCOPE.md for current behavior)
- [Entity Cards v2 — design](architecture/12_ENTITY_CARDS_V2_DESIGN.md) — shipped; superseded by 12_ENTITY_CARDS.md
- [KG Cold-Start Spec](KG_COLD_START_SPEC.md) — speculative, not wired
- `docs/design/*` — forward-looking design notes (cli_emi, coding_agent_for_emi, me_lens, …)

## Conventions used in these docs

- **Code paths** are repo-relative: `app/assistant/agents/wiki_writer/config.yaml`
- **Cite symbols, not line numbers** — reference `ToolCaller` / `proposal_promoter.run_promoter` rather than `foo.py:142` (line numbers rot; symbols don't)
- **`> Note:` callouts** flag where code and design intent differ or something is in flight
- **Style:** terse, code-grounded, no marketing — docs for engineers
