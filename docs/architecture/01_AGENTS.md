# Agents

An **Agent** is a self-contained LLM-powered decision unit. Each agent processes inputs via an LLM and produces structured outputs that drive the system's behavior. **Agents decide; control nodes and tools act** — an agent never executes a tool directly. It emits `action` + `action_input`; the flow controller and control nodes (ToolCaller etc.) dispatch.

## Agent Contract

Base class: `app/assistant/agent_classes/Agent.py`

`Agent` is a thin shell. Its public methods are delegators — the real
work lives in injected services on `self.components`
(`AgentComponents`, built by `AgentComponentsFactory`):

- `action_handler(message) -> ToolResult` — the only entry point. Runs
  four phases: `_prepare_execution_context` (unpack input, store msg,
  set `last_agent`, bump per-agent step counter) → `_build_messages`
  (`construct_prompt`) → `_execute_model` (`call_llm` with
  `config.structured_output`) → `_finalize_execution`
  (`process_llm_result`). Wrapped in busy/idle tracking + performance
  timing.
- `construct_prompt` / `get_system_prompt` / `get_user_prompt` →
  delegate to `components.prompt_builder`.
- `call_llm` → delegates to `components.llm_client.call_structured_output`.
- `process_llm_result` → validates the action contract, applies result
  to blackboard, creates the audit message, runs flow control, emits a
  progress fact. Returns `ToolResult(result_type="llm_result")`.
- `get_tools` / `get_visible_tools` / `get_allowed_nodes` /
  `get_tool_descriptions` → delegate to `self._tool_policy`.

Per-agent service objects constructed in `__init__`:

| Service | Module | Role |
|---------|--------|------|
| `ToolPolicyResolver` (`self._tool_policy`) | `services/tool_policy_resolver.py` | allowed tools, visible tools, allowed nodes, allowed actions |
| `ActionContractService` (`self._action_contract`) | `services/action_contract_service.py` | normalize + validate `action`/`action_input` |
| `AgentInputApplier` (`self._input_applier`) | `services/agent_input_applier.py` | unpack inbound message onto blackboard |
| `AgentResultApplier` (`self._result_applier`) | `services/agent_result_applier.py` | write LLM output to blackboard, build audit message |

Shared services reached via `self.components` (built per agent by
`AgentComponentsFactory.build_for_agent`): `StatusTracker`, `LLMClient`,
`ChatRequestNormalizer`, `ChatResponseBuilder`, `ChatPublisher`,
`HistoryFormatter`, `ActionValidator`, `FlowController`,
`ProgressEmitter`, `PromptBuilder`, `ContextInjector`, `EntityInjector`,
`PodInjector`, `ResourceResolver`. (`ChatMemoryRag` and
`KeywordResourceIndex` are module-level singletons used inside
`ContextInjector`, not components.)

## Directory Structure

Each agent lives under `app/assistant/agents/`:

```
agents/
  agent_name/
    config.yaml           # Agent metadata and configuration (required)
    prompts/
      system.j2           # System prompt (Jinja2, required)
      user.j2             # User prompt (Jinja2, required)
      description.j2      # Optional: rendered for {{ allowed_nodes }} listings
    agent_form.py         # Optional: Pydantic model for structured output
    input_schema.py       # Optional: Pydantic model for input validation
    .ignore               # Optional: presence skips this agent at load
```

`prompts/system.j2` and `prompts/user.j2` are **required** — the
registry raises `FileNotFoundError` if either is missing
(`agent_registry._load_prompts`). `description.j2` is optional.

Nested agents use the `::` naming convention. The canonical name is
`namespace::name`, derived from the directory path relative to
`agents/` (multi-level dirs join with `::`); a `name:` value that
already contains `::` is taken verbatim
(`agent_registry._load_all_agent_configs`).

```
agents/
  dayflow_orchestrator/
    action_selector/      # Canonical name: dayflow_orchestrator::action_selector
      config.yaml
      prompts/
      agent_form.py
```

## config.yaml

Fields actually read by the loader (`agent_registry.py`) and runtime
services. All but `name`, `class_name`, and the two prompts are
optional.

```yaml
name: agent_name                    # Canonical name (may include ::)
class_name: Agent                   # Class file in agent_classes/ (see Variants)

llm_params:
  llm_provider: openai              # openai | gemini | anthropic
  engine: gpt-5.4-nano              # Model name
  model_tier: nano                  # Tier hint
  temperature: 0.1

# --- Tool / node policy (permission CEILING, not a grant) ---
allowed_tools: [tool1, tool2]       # List or "all"
except_tools: [tool3]               # Exclude from allowed_tools
allowed_nodes: [agent1, agent2]     # Delegation targets (list or "all")
except_nodes: [agent3]

# --- Structured output / action contract ---
structured_output:                  # JSON schema; superseded by agent_form.py
  type: object
  properties: {}
action_required: true               # If true, output MUST carry a valid action
append_fields: [progress_report]    # Output keys that append instead of replace
global_output_keys: [field1]        # Output keys written to global (vs local) scope

# --- Prompt / context ---
system_context_items: [...]         # Keys injected into system.j2
user_context_items: [...]           # Keys injected into user.j2
required_context_items: [task]      # Refuse to run if any resolves empty (hard guard)
strict_template: false              # true => StrictUndefined (undefined var raises)

# --- Entity card injection (EntityInjector) ---
entity_scan_keys: [incoming_message, task, recent_history]   # currently INERT — detection scans the whole rendered prompt
entity_card_level: 1                # View level (0..4) for entity_card key
entity_card_sections: [level_0, contact]   # Overrides level for `entity_card` only
entity_render_fields: [...]         # Override which entity_* fields render

# --- Keyword-triggered resource injection (ContextInjector) ---
enable_keyword_resource_injection: true     # opt in to sidecar *.triggers.json index
keyword_scan_context_keys: [task, incoming_message]   # what to scan (default these two)
task_keyword_resources:             # per-agent keyword -> resource_id map
  doordash: resource_doordash_guidelines

# --- Skills (Anthropic Agent Skills) ---
skills: [critic-handling]           # Static skills always loaded (gated by requires_scope)
accept_auto_skills: true            # Allow SkillInjector to auto-inject by trigger

events: [event_name]                # Event handlers to register (EmiReminderHandler etc.)
prompt_debug: {system: true, user: true, results: true}   # Debug logging flags
```

Note: there is no `allowed_resources` field — resource access is
governed by scope policy and the `resource_*` context keys.

## agent_form.py

Defines the structured output schema using Pydantic.
`agent_registry._load_agent_form` imports the file under a unique
per-path module name (so Pydantic forward-refs resolve), then selects:
the class named **`AgentForm`** if present, otherwise the **first
`BaseModel` subclass** found. The result becomes the agent's
`structured_output` and **takes precedence over** any
`structured_output` in `config.yaml` (a warning is logged if both
exist).

```python
from pydantic import BaseModel, Field

class AgentForm(BaseModel):
    chat_response: str = Field(..., description="Direct reply to user")
    handoff_tf: bool = Field(default=False, description="Route to switchboard")
    switchboard_task: str = Field(default="", description="Task for switchboard")
```

## Prompt Templates

Templates are Jinja2, rendered by `PromptBuilder`
(`services/prompt_builder.py`) against a shared environment rooted at
the `agents/` directory (so templates can
`{% import "shared/macros/..." %}`). `strict_template: true` swaps in a
`StrictUndefined` environment.

**system.j2** — defines the agent's role and rules.
**user.j2** — provides the current request context.

Two prompt guards backstop empty input (a blank prompt reads to an LLM
as conservative judgment, not an error):

1. **`required_context_items`** (hard) — `enforce_required_context_items`
   raises `PromptRenderError` if any declared item resolves empty.
2. **Skeleton guard** (generic, zero-config) — `enforce_skeleton_guard`
   compares the rendered user prompt against the same template rendered
   with *no* context; an exact match means no data reached the template
   and raises. Static templates (no Jinja constructs) are exempt.

`PromptBuilder.construct_prompt` also: appends out-of-band mailbox
`@`-messages as an authoritative-newest block (`_append_runtime_injections`),
and interleaves `datapod:image:` pod URIs (capped to the 4 most recent)
plus legacy `[emi_image:...]` / `[mcp_image_path:...]` markers as inline
image blocks in the content array. Prompts reach the provider **verbatim
UTF-8** — an earlier ASCII-normalization pass was removed 2026-07-08
(context-injection audit C1: it flattened diacritics and deleted emoji).

### Context Injection

Keys in `system_context_items` / `user_context_items` are resolved by
`ContextInjector.generate_injections_block`
(`services/context_injector.py`):

- **`resource_*`** → `ContextInjector.resolve_resource` → ResourceManager
  via `ResourceResolver.get_global_resource`, gated by `scope_context`
  (degrades to `""` when scope policy blocks it on restricted surfaces).
- **Computed defaults** (always present): `date_time`, `day_of_week`,
  `action_count`, `room_contact_name`, `current_speaker_name`,
  `skills` (name→body dict), `auto_injected_skill_names`,
  `incoming_message`, and — when the message carries them — `agent_input`,
  `task`, `information`.
- **`tool_descriptions`** → `get_tool_descriptions()` (visible tools only).
- **`allowed_nodes`** → list of `{name, description}` (description.j2 rendered).
- **Special keys** with bespoke resolvers (~14): `recent_history`,
  `latest_exchange`, `prior_history` (scope-local message logs);
  `chat_memory` (RAG recall via `chat_memory_rag.recall`, room-scoped —
  raises loudly if declared without a `room_id`);
  `chat_nudges` (`pending_questions.pick_question_for_nudge`);
  `user_bio_context`; `health_status_summary`;
  `referenced_pods` / `referenced_pods_block` (via `pod_injector`);
  `recent_dayflow_items`, `recent_dayflow_tickets`;
  `context_activation_memo`; `location_summary`; `geoguessr_*`.
  Any unrecognized key falls back to `blackboard.get_state_value(key)`.
- **Skills** are resolved through `SkillInjector`
  (`DI.skill_injector`) and `DI.skill_registry` via
  `resolve_skills` / `_resolve_skills_with_provenance` — four paths
  (static `config.skills`, auto-injected when `accept_auto_skills`,
  caller-supplied `skills_input`, scope-stamped
  `scope.skills.always_inject`), each passing a universal
  `skill_gate_passes(requires_scope)` gate.
- **Keyword resources**: `task_keyword_resources` (per-agent map) and,
  when `enable_keyword_resource_injection` is set, the global
  `KeywordResourceIndex` (sidecar `resource_*.triggers.json` scanned at
  startup) inject matched resources by scanning `keyword_scan_context_keys`.
- **Entity injection** (`EntityInjector`,
  `services/entity_injector.py`): a two-pass render detects entities in
  the **whole rendered user prompt** (pass 1 with blank `entity_*` keys),
  merges room-seeded entities (`room_pinned_entities` /
  `room_allowed_entity_cards`), applies room-blocked and scope-policy
  narrowing, then re-renders with the `entity_*` keys filled from
  entity_card_v2 (`get_entity_card_for_prompt_injection_level`, honoring
  `entity_card_level` / `entity_card_sections`). Note: `entity_scan_keys`
  in config is currently **inert** for this path — detection is not
  scoped to it (the scan-key helpers exist but have no production
  caller).

## Tool Access Control

Agents don't call tools — they output `action` + `action_input` for the
control nodes to dispatch. `ToolPolicyResolver` enforces two distinct
sets:

**Allowance** (`get_tools`) — what the agent may *call*:
1. Start from `allowed_tools` (`"all"` ⇒ every registered tool).
2. Subtract `except_tools`; drop tools not in the registry (warn).
3. If `task_allowed_tools` (blackboard) is a list, intersect — but a
   manager-scoped `dynamic_allowed_tools` list can expand that allowset
   first.
4. Subtract `task_except_tools` (blackboard), then `dynamic_denied_tools`.
5. If no `task_allowed_tools` was set, *add* `dynamic_allowed_tools`
   (registry-valid only).

`dynamic_allowed_tools` is a generic blackboard override (MCP-injected
tools are one source, not the definition).

**Visibility** (`get_visible_tools`) — the subset shown in prompts: the
intersection of the blackboard `visible_tools` list with the allowed
set. An empty intersection shows nothing (it never falls back to the
full allowed set); narrowable agents keep `find_tool` visible so this is
never a dead end. `get_tool_descriptions` / `get_tool_arguments_prompt`
operate on visible tools.

**Nodes** (`get_allowed_nodes`): `allowed_nodes` (`"all"` ⇒ every agent
minus the intrinsic `return_control`/`done`), minus `except_nodes`,
keeping only callable agents (`is_callable_agent` — instantiated names
win once any instances exist).

## Agent Variants

`class_name` names a file in `app/assistant/agent_classes/`; the
registry loads the class of the same name (`_load_agent_class`).
Available classes:

| Class | Purpose |
|-------|---------|
| `Agent` | Standard single-turn LLM decision (base) |
| `Planner` | Multi-step planning with plan validation |
| `MultiToolAgent` | DAG-based multi-tool execution |
| `Delegator` | Manager's routing agent (the entry binding) |
| `OneShotAgent` | Single fire-and-return decision |
| `ToolArguments` / `ToolArgumentsPlaywright` | Tool-argument assembly agents |
| `PlaywrightAgent` / `PlaywrightCritic` / `PlaywrightWatchdog` | Browser-automation roles |
| `SummaryAgent` | Conversation/room summarization |
| `EmiReminderHandler` | Event-driven handler (registers via `events:`) |

## How to Add a New Agent

1. Create directory: `app/assistant/agents/<name>/` (or nested under a namespace).
2. Write `config.yaml` with `name`, `class_name`, `llm_params`, tools/nodes, context items.
3. Write `prompts/system.j2` (role) and `prompts/user.j2` (request context) — both required.
4. Optionally write `agent_form.py` (Pydantic `AgentForm`) and/or `input_schema.py`.
5. Reference the agent in a manager's `state_map` / `role_bindings` / `allowed_nodes` to wire it into a flow.
6. `AgentRegistry` auto-discovers it on startup (`rglob` over `agents/`) — no registration code needed.

## Key Files

| File | Purpose |
|------|---------|
| `agent_classes/Agent.py` | Base class — thin delegator to component services |
| `agent_classes/Planner.py`, `MultiToolAgent.py`, ... | Class variants (see table) |
| `agent_registry/agent_registry.py` | Auto-discovery; config/prompt/form/class loading |
| `agent_registry/agent_factory.py` | Instantiation with components |
| `agent_runtime/factories/agent_components_factory.py` | Builds `AgentComponents` |
| `agent_runtime/services/prompt_builder.py` | Jinja2 rendering + prompt guards + image interleave |
| `agent_runtime/services/context_injector.py` | Context key resolution (resources, history, skills, keyword resources) |
| `agent_runtime/services/entity_injector.py` | Entity-card injection (two-pass detect + render) |
| `agent_runtime/services/tool_policy_resolver.py` | Allowed/visible tools + allowed nodes |
| `agent_runtime/services/action_contract_service.py` | Action contract normalize + validate |
| `agent_runtime/services/llm_client.py` | Structured LLM call interface |
| `agent_runtime/services/flow_controller.py` | Action routing |
