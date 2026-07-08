# Mental Model

Read this before any architecture doc. It defines the words EmiOS uses for the things it does.

These are the seven concepts that, if you understand them, let you read every other doc without bouncing for vocabulary.

---

## 1. Agent

> An LLM with a job description, a prompt, a strict output shape, and an allowlist of tools and other agents it can call.

An agent is *not* a person and *not* an autonomous loop — it is one LLM call wrapped in scaffolding that:

- Renders a system prompt and a user prompt from Jinja2 templates.
- Calls the LLM and forces structured output (JSON / Pydantic schema).
- Writes the result fields onto a shared **Blackboard**.
- Returns control to whatever code invoked it.

That's it. An agent does not run tools, does not chain itself, does not escalate. It produces a *decision* — fields on the blackboard like `action`, `action_input`, `next_agent`, `chat_response`. **Other things in the loop turn those decisions into actions.**

There are three classes (in `agent_classes/`):

- **`Agent`** — single-turn decision (most common).
- **`Planner`** — decision that includes a multi-step plan (used for compiled tasks).
- **`MultiToolAgent`** — variant that can express a small DAG of tool calls.

Agents live one-per-directory under `app/assistant/agents/`. See **[01_AGENTS.md](../architecture/01_AGENTS.md)**.

---

## 2. Manager

> An orchestrator. It runs an agent loop, holds the shared state (Blackboard), enforces routing rules, and exits when done.

A manager is *not* an LLM — it is plain Python that says "first call agent X, then if X said `handoff_tf=true` route to agent Y, then call control node Z, repeat until exit." It owns the loop. Agents are its workers.

Two base classes:

- **`MultiAgentManager`** — the one manager class. A state map of agent → next-agent transitions (looked up each cycle by the deterministic `Delegator` agent), plus role bindings, tool scope, and a max-cycle budget.

Specialized managers (`emi_team_manager`, `kg_investigation_manager`, `kg_mutation_manager`, `devices_manager`, etc.) are built on top of these by editing the YAML config and supplying domain-specific agents. See **[02_MANAGERS.md](../architecture/02_MANAGERS.md)** and **[15_EMI_TEAM_AND_SCOPE.md](../architecture/15_EMI_TEAM_AND_SCOPE.md)**.

---

## 3. Planner

> An agent variant whose decision is a multi-step plan.

When you see "the planner" in a manager flow, it usually means an agent (often inheriting from the `Planner` class) whose job is to look at the task and emit a structured plan: "first call get_weather, then call check_calendar, then summarize." Each emi_team-derived manager has a planner agent at the start of its loop.

Don't confuse "planner" (an agent role) with `Planner` (the Python class). Most manager configs use `class_name: Agent` for their planner because the `Planner` class adds plan-validation overhead that isn't always wanted.

---

## 4. Control Node

> Deterministic Python in the agent loop. Reads blackboard state, decides what to do next, sets next_agent.

Control nodes interleave with agents in the same `state_map`. The manager treats them identically — both implement `action_handler(message)`. The difference is intent: agents introduce LLM nondeterminism, control nodes do not.

Examples:
- `chat_task_router_node` — reads `handoff_tf` from blackboard, routes to switchboard or final answer.
- `tool_caller` (~700 lines, the canonical dispatcher) — given an `action` field, executes the tool or invokes the called agent.
- `tool_result_handler` — pushes the tool result back to the calling agent.
- `final_answer_node` — flips the exit flag.

When you want to express "do X if condition Y" without burning an LLM call, write a control node. See **[04_CONTROL_NODES.md](../architecture/04_CONTROL_NODES.md)**.

---

## 5. Tool

> An executable capability. Talk to Gmail, write a file, query the KG, send a Slack message.

Each tool extends `BaseTool` and implements `execute(tool_message) -> ToolResult`. Tools are registered automatically by the **tool registry** (it scans `app/assistant/lib/tools/`). Each tool has:

- A core implementation in `lib/core_tools/<name>/`.
- A thin wrapper in `lib/tools/<name>/` with `tool_contract.json`, Pydantic argument forms, and Jinja prompt fragments (one for planner selection, one for argument generation).

Tools are invoked by `tool_caller`, never by agents directly. Agents *output* an `action` and `action_input`; the loop dispatches.

See **[07_TOOLS.md](../architecture/07_TOOLS.md)**.

---

## 6. Room

> A scoped conversation channel with its own identity, permissions, safety rules, and policy.

Each room (`master_room`, `jamie`, `slack/<channel>`, `telegram/<chat>`, `dayflow_orchestrator`, etc.) is a directory under `app/assistant/rooms/<room_id>/` with two files:

- `ROOM.md` — a single Markdown file whose YAML frontmatter holds the room's policy (manager, surface, retention, authority level), permissions (allowed tool classes), and access (visible resources/entities); the body maps onto the blackboard. See `rooms/ROOM_CONTRACT.md`.
- `scope.yaml` — the room's permission envelope (see Scope, below).

The `master_room` has authority level 99 (full access). Other rooms are narrower. The `manager_name` in `ROOM.md`'s frontmatter decides which manager handles the room's inbound messages.

Rooms are how EmiOS keeps a Slack conversation from accidentally seeing the user's private context, and how it knows the right "voice" to use per surface. See **[03_ROOMS.md](../architecture/03_ROOMS.md)**.

---

## 7. Scope (`ScopeContext`)

> The permission envelope on every Message. Says who's calling, what they can read, what they can write.

`ScopeContext` is a Pydantic struct attached to every `Message` that flows through the system. It carries:

- `actor_id`, `surface`, `room_id` — identity
- `authority_level` (in `ScopeApprovalPolicy`) — used by approval gates
- `allowed_global_resources` (in `ScopeResourcePolicy`) — what resource files this scope can read
- `write_kg`, `write_unified_log`, etc. (in `ScopeWritePolicy`) — what side-effects are permitted

**The narrowing-only rule**: a manager's `scope_contract` can only narrow inbound permissions, never widen them. So if you want a manager that needs to write to the KG, the *caller* must pass a Message whose scope already grants `write_kg=True`. See **[15_EMI_TEAM_AND_SCOPE.md](../architecture/15_EMI_TEAM_AND_SCOPE.md)** for the worked example.

When an agent tries to use a tool, that scope is checked by a **four-layer gate**: the allowed-tools list is a *ceiling*, then visibility, then an authority floor, then (for sensitive actions) an approval step. You rarely touch this directly — but when a tool "isn't available" to an agent that should have it, this gate is usually why. The canonical reference is **[SCOPE.md](../architecture/SCOPE.md)**.

---

## How they all fit together (the canonical chat flow)

```
User types in the chat UI
  → SocketIO delivers to RoomSessionManager
    → builds InboundEnvelope + loads master_room context
      → ManagerInvoker.invoke(master_room_manager, message)
        → master_room_manager (a MultiAgentManager) starts its loop:
            agent: chat_gate           ← LLM decides: reply | handoff | dayflow_delegate
            control: chat_task_router  ← deterministic routing on the decision
            agent: switchboard         ← if handoff: pick a tool/manager
            control: switchboard_args  ← normalize tool arguments
            control: tool_caller       ← execute the tool (or invoke another manager)
            control: tool_result_hand. ← push result back into the loop
            control: final_answer      ← exit when done
        → returns ToolResult
      → RoomSessionManager persists + delivers reply via WebSocket
  → user sees the reply
```

Three things to notice:
1. The **manager** owns the loop. It's plain Python.
2. **Agents** make decisions — they emit fields, they don't take actions.
3. **Control nodes** turn decisions into actions and route to the next step.

That's the entire skeleton. Every other manager (kg_mutation_manager, dayflow_orchestrator, weekly_insights_pipeline, etc.) is a variation on this same pattern with different agents and different control nodes.

---

## Things that confuse newcomers

- **`Agent` (Python class) vs "the agent" (a directory under `agents/`).** The class lives in `agent_classes/Agent.py`. An "agent" in everyday speech is a *config* (yaml + prompts + form) that gets instantiated *with* the class.

- **`emi_team_manager` is a manager, not a team of human-named agents.** It's a general-purpose worker shaped delegator → planner → tool_caller → critic → summary. Specialized managers reuse its delegator + summary while swapping in their own planner + final_answer. See **[15_EMI_TEAM_AND_SCOPE.md](../architecture/15_EMI_TEAM_AND_SCOPE.md)**.

- **Pipelines vs Routines.** A *pipeline* is sequential step-based code that runs once when invoked. A *routine* is the *schedule* that decides when to invoke a pipeline (or a tool, task, function, job). Pipelines live in `app/assistant/pipelines/`; routines live in `configs/routines.json`. See **[06_PIPELINES_AND_ROUTINES.md](../architecture/06_PIPELINES_AND_ROUTINES.md)**.

- **Wiki vs Entity Cards.** Both surface KG knowledge per-entity, but they're different artifacts. The **wiki** is markdown files in a vault (`<your-home>/EmiWiki`) — long-form, narrative, browseable. **Entity cards** are rows in the `entity_cards` SQLite table — short, structured, edited inline in the UI. They are produced by separate pipelines but share the same KG source. See **[11_WIKI_GENERATOR.md](../architecture/11_WIKI_GENERATOR.md)** and **[12_ENTITY_CARDS.md](../architecture/12_ENTITY_CARDS.md)**.

- **The Knowledge Graph is a SQLite + ChromaDB pair.** Structural facts (nodes, edges, taxonomy) live in SQLite tables (`kg_node_metadata`, `kg_edge_metadata`, etc.). Semantic embeddings (for similarity search) live in ChromaDB. Code refers to "the KG" as one thing — but under the hood it's two stores kept in sync.

- **Blackboard ≠ Global Blackboard.** The per-invocation **Blackboard** is a scoped state stack that one manager invocation owns; it disappears when the manager exits. The **Global Blackboard** (`DI.global_blackboard`) is a process-wide message log that survives across invocations.

- **Pods are not pages.** A *pod* (named "datapod" in code) is a URI-addressable unit of content (e.g., `datapod://unified_log/<id>`) — a way for agents to pass references instead of full text. A wiki *page* is a markdown file. Don't conflate. See **[14_PODS.md](../architecture/14_PODS.md)**.

---

## Where to go next

You should now be able to read **[00_OVERVIEW](../architecture/00_OVERVIEW.md)** without bouncing for vocabulary. After that, work through the architecture docs in order. Keep **[Glossary](../GLOSSARY.md)** open in a tab.
