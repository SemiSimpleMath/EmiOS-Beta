# Service Layer

The service layer is the foundation everything else sits on. It owns the dependency-injection container, the in-process pub-sub bus, the per-invocation scope stack, the process-wide message log, the JSON resource cache, and the bootstrap that wires it all together. Every agent invocation, tool call, manager loop, and pipeline step traces back to one of the services described here. If you're tracing any inbound message through the codebase, you will hit `DI`, `EventHub`, `Blackboard`, `GlobalBlackBoard`, and `ResourceManager` within the first few hops.

## The DI container

Source: `app/assistant/ServiceLocator/service_locator.py`.

The container is intentionally tiny. It is a thread-safe `dict[str, object]` (`ServiceLocator`) plus a proxy object (`DI`) that turns attribute access into a registry lookup:

```python
from app.assistant.ServiceLocator.service_locator import DI
DI.event_hub.publish(message)
DI.tool_registry.get_tool(name)
DI.global_blackboard.add_msg(msg)
```

`DIProxy.__getattr__` raises `AttributeError("Service '<name>' not registered.")` for unknown keys (`service_locator.py:24`), so missing-service bugs surface immediately rather than as `None.method()` traces.

There is **no lazy initialization inside the container itself** — every service must be eagerly registered by `bootstrap.initialize_services` or `initialize_system`. "Lazy" in this codebase means deferred *import*, not deferred construction (the proxy reads from a populated registry).

### Canonical services

Registration phase is noted (Phase 1 = `bootstrap.py:initialize_services`, Phase 2 =
`initialize_system.py`); within a phase, order is top-to-bottom of the source.

| Service key                       | Purpose                                                                 | Phase                          |
|-----------------------------------|-------------------------------------------------------------------------|--------------------------------|
| `event_hub`                       | Pub-sub bus (`EventHub`) — non-blocking publish, worker pool.           | 1                              |
| `db_manager`                      | Single-writer SQLite coordinator.                                       | 1                              |
| `user_settings`                   | User flags / preferences storage (`UserSettingsManager`).               | 1                              |
| `afk_monitor`                     | Active-first idle detector (auto-started).                              | 1                              |
| `agent_registry`                  | Agent config discovery (`config.yaml` + prompts + form).                | 1                              |
| `tool_registry`                   | Tool catalog + MCP tool cache.                                          | 1                              |
| `agent_factory`                   | Constructs agents from registry entries.                                | 1                              |
| `manager_registry`                | Manager config catalog (file-system scan of `multi_agents/`).           | 1                              |
| `multi_agent_manager_factory`     | Builds per-call manager instances.                                      | 1                              |
| `orchestrator_registry`           | Orchestrator catalog, preloaded.                                        | 1                              |
| `orchestrator_instance_handler`   | Long-lived orchestrator instance bookkeeping.                           | 1                              |
| `orchestrator_factory`            | Builds orchestrators (parallel to managers).                            | 1                              |
| `data_conversion_module`          | Cross-format data conversion utilities.                                 | 1                              |
| `global_blackboard`               | Process-wide in-memory message log + system state summary.              | 1                              |
| `resource_manager`                | JSON/text resource cache backed by `resources/`.                        | 1                              |
| `skill_registry`                  | `SKILL.md` loader (`SkillRegistry`, walks `skills/<name>/SKILL.md`).    | 1                              |
| `skill_injector`                  | Per-skill `auto_inject_when` trigger evaluation (`SkillInjector`).      | 1                              |
| `reply_router`                    | Multi-transport reply dispatch (socketio, sms, slack, telegram).        | 1                              |
| `manager_invoker`                 | Canonical manager invocation entrypoint.                                | 1                              |
| `agent_components_factory`        | Wires `event_hub` + `resource_manager` + `blackboard` into agents.      | 1                              |
| `room_session_manager`            | Transport-agnostic room session orchestrator.                           | 1                              |
| `entity_catalog`                  | Fast entity-name lookup (singleton).                                    | 1                              |
| `env_registry`                    | `EnvRegistryService` — env/secret + scope-filtered account registry.    | 1                              |
| `scheduler`                       | `SchedulerService` (APScheduler BackgroundScheduler).                   | 1                              |
| `ticket_manager`                  | Ticket CRUD with state machine.                                         | 1                              |
| `background_task_manager`         | Daemon thread pool for routines.                                        | 1 (subsystem-gated)            |
| `dj_manager`                      | Music automation manager.                                               | 1 (subsystem-gated)            |
| `socket_manager`                  | WebSocket session registry; emits to clients.                           | 2                              |
| `music_afk_relay`                 | Bridges AFK state changes to music client.                              | 2                              |
| `event_relay`                     | `EmiEventRelay` — event-hub → SocketIO bridge.                          | 2                              |
| `ticket_dispatcher`               | `TicketDispatcherRegistry` — fans `proactive_suggestion` to surface adapters. | 2                        |
| `progress_curator`                | Curates agent progress facts for the progress UI.                       | 2                              |
| `chat_narrator`                   | `ChatNarrator` — narrates multi-agent activity into chat.               | 2                              |
| `mailbox`                         | Manager-runtime `Mailbox`.                                              | 2                              |
| `mam_instance_manager`            | `MAMInstanceManager` — multi-agent-manager instance bookkeeping.        | 2                              |
| `outbound_chat_publisher`         | `OutboundChatPublisher` — publishes assistant chat to transports.       | 2                              |
| `question_service`                | Ask-user question lifecycle.                                            | 2                              |
| `emi_result_handler`              | Handles results returned to the user (pre-instantiated agent).          | 2                              |
| `emi_reminder_handler`            | Reminder dispatch agent (pre-instantiated agent).                       | 2                              |
| `signal_router`                   | Reactive intake watcher (subscriber to the gut).                        | 2 (subsystem-gated)            |
| `ingest_service`                  | "The gut" — unified inbound intake.                                     | 2 (subsystem-gated)            |
| `pod_classifier_service`          | Declarative pod-minting from intake envelopes.                          | 2 (subsystem-gated)            |
| `task_ir_runner`                  | Compiled task-IR execution engine.                                      | 2                              |
| `dayflow_scheduler`               | Event-driven dayflow tick coordinator.                                  | 2 (subsystem-gated)            |

There are no lazy entries. The proxy will fail loudly if an unregistered key is accessed before its bootstrap step has run, which is also why initialization order matters.

> Note: a few subsystems (`background_task_manager`, `dj_manager`, `signal_router`, `ingest_service`, `pod_classifier_service`, `dayflow_scheduler`) are gated by `subsystems.yaml` and may legitimately be absent. Callers must use `getattr(DI, "name", None)` for those.

## Bootstrap sequence

There are two phases, both invoked from `app/create_app.py`.

```
create_app()                                   # app/create_app.py
  Flask + SocketIO + DB engine
  initialize_all_tables()                      # creates SQLite tables
  initialize_services(app)                     # PHASE 1 — bootstrap.initialize_services
  app.DI = DI; DI.socket_io = socketio
  register_socket_handlers(socketio)
  initialize_system()                          # PHASE 2 — initialize_system.initialize_system
  socket_manager.start_stale_sweeper(...)
```

### Phase 1: `initialize_services` (`bootstrap.py`)

Order is load-bearing — each step depends on what came before:

1. `_seed_config_templates()` then `_seed_personal_resources()` — first-run seeding: copy `configs/templates/*.template.json` and personal `*.json.example` files into the writable data dir *before* any tool/agent/resource/oauth consumer reads them. (These run at the very top of `initialize_services`.)
2. `_auto_detect_default_llm_provider()` — sets `DEFAULT_LLM_PROVIDER` env var if exactly one provider key is present.
3. `event_hub` — `EventHub` constructed with `EMI_EVENT_WORKER_THREADS` workers (default 24). Dispatcher thread starts here.
4. `db_manager` — single-writer coordinator (`app/models/db_manager.py`).
5. `user_settings` — required by many downstream consumers.
6. `afk_monitor` — `.start()` is called immediately; this fires its background thread.
7. `agent_registry` then `tool_registry` — registry objects, then `load_tools()`, MCP server load, MCP cache load (`load_mcp_tool_cache` + `load_installed_mcp_tools`), `load_agents()`. Tools must exist before agents (agents reference allowed tools by name).
8. `agent_factory` — needs both registries above.
9. `manager_registry` (file-system scan), `multi_agent_manager_factory`.
10. `orchestrator_registry.preload_all()`, `orchestrator_instance_handler`, `orchestrator_factory`.
11. `data_conversion_module`.
12. `global_blackboard` — empty `GlobalBlackBoard()`. Nothing else can `add_msg` until this exists.
13. `resource_manager` — `ResourceManager()` then `load_all_from_directory("resources")`. This is a 3-pass loader (JSON → templates → text) and also publishes every value into `global_blackboard.state_dict` as a side effect, so `global_blackboard` must already exist. Dynamic resource *providers* are registered here too (`resource_accounts`, `resource_email_accounts`) along with the `resource_user_email` acting-as lock.
14. `skill_registry`, `skill_injector` — `SKILL.md` loader + trigger evaluator.
15. `reply_router`.
16. `manager_invoker` — wired with explicit `RequestPreprocessor(resource_manager=...)`.
17. `agent_components_factory` — wired with explicit `event_hub`, `resource_manager`, `global_blackboard`.
18. `room_session_manager` — wired with explicit `blackboard`, `event_hub`, `reply_router`, `resource_manager`, `manager_registry`, `multi_agent_manager_factory`, `manager_invoker`.
19. `entity_catalog` (singleton), then `env_registry` (`EnvRegistryService`).
20. `scheduler` — `SchedulerService(app)`. Auto-starts via `TimingEngine.__init__`.
21. Tickets DB init, Google OAuth DB init, `ticket_manager`. Stale tool-approval tickets are cleared from the previous session.
22. `background_task_manager`, `dj_manager` — gated by `subsystems.yaml`.
23. `atexit.register(shutdown_services)` — ensures clean LIFO shutdown.

### Phase 2: `initialize_system` (`initialize_system.py`)

This phase is for things that need the registry from Phase 1 already present:

1. `manager_registry.preload_all()` — eager-loads every manager's config + agent set, then builds the display-name registry from each manager's `display_name`.
2. `validate_all(agent_registry)` — validates every agent definition (raises on bad config).
3. `register_kg_embedding_sync()` — installs the KG embedding chokepoint (production process only; skipped under `USE_TEST_DB`).
4. `socket_manager` — registered here, *not* in Phase 1, because socket emits can't happen until SocketIO is bound to the Flask app.
5. `music_afk_relay`, `event_relay`.
6. `ticket_dispatcher` — `TicketDispatcherRegistry` with the socketio / telegram / slack / sms `TicketSurfaceAdapter`s registered, then `subscribe_to_event_hub()`.
7. `progress_curator`, `chat_narrator`, `mailbox`, `mam_instance_manager`, `outbound_chat_publisher`, `question_service`.
8. Two pre-instantiated agents (`emi_result_handler`, `emi_reminder_handler`) — stateful and shared, constructed once via `agent_factory.create_agent(...)` and registered as services rather than rebuilt per call.
9. **Native singleton pre-warm** (on the boot thread, *before* routine fan-out): self-heal corrupt chroma collections, then build the ChromaDB client + KG collections (`get_chroma_manager()`), the embedder (`embed_text("warmup")`), and the belief chroma collection. First-time init of these native libs is not thread-safe — warming serially here prevents the concurrent-first-init crash. Then **LLM SDK pre-warm**: import the Gemini / Anthropic SDKs for whichever providers have a real (non-placeholder) key, so the first agent call isn't cold.
10. `get_routine_manager().refresh()` (if `routine_manager` enabled) — wires event-triggered routine subscriptions immediately so the first event in the post-boot window isn't dropped.
11. Subsystem-gated services: `signal_router`, `ingest_service` (the gut) with its subscribers (`signal_router.handle_envelope`, `pod_classifier_service`), `dayflow_scheduler`.
12. `task_ir_runner` — `ensure_event_subscription()` wires it onto the bus.
13. `register_ticket_answer_listener()` — routes ticket responses for ticket-mode pending questions back into the subconscious answer loop.

The legacy `SlackInterface` polling adapter is gone — Slack inbound is handled
exclusively by the `/slack/events` webhook (`app/routes/slack_events.py`); the
poller was retired 2026-05-05 to stop duplicate-message processing.

After Phase 2, the stale-socket sweeper starts (`create_app.py`).

### Shutdown

`shutdown_services` (`bootstrap.shutdown_services`) runs at process exit in LIFO order: `dj_manager` → `background_task_manager` → `scheduler` → `afk_monitor` → `event_hub` → SQLAlchemy engines. Stopping consumers before the bus prevents handlers from firing into a torn-down hub. Engines come last so any pending commits from those handlers can land.

## EventHub

Source: `app/assistant/event_hub/event_hub.py`. The DI key matches the module name (`event_hub`).

### Interface

```python
event_hub.register_event(event_key: str, handler: Callable[[Message], None]) -> None
event_hub.unregister_event(event_key: str, handler: Optional[Callable] = None) -> None
event_hub.publish(message: Message) -> None
event_hub.set_receiver_status(receiver: str, is_busy: bool) -> None
event_hub.is_receiver_busy(receiver: str) -> bool
```

`publish` is non-blocking: it enqueues the message onto a bounded `queue.Queue(maxsize=5000)` with a 5-second `put` timeout (`event_hub.py:193`). If the dispatcher is wedged for more than 5s the message is dropped with an ERROR log. Duplicate handler registrations raise (`event_hub.py:88`).

### Dispatch model

A single dispatcher thread (`event-handler-hub-dispatcher`) drains the queue and calls `_deliver` for each message. `_deliver` looks up handlers for `message.event_topic` and submits them to a `MonitoredThreadPoolExecutor` with `worker_threads` workers (default 24, override `EMI_EVENT_WORKER_THREADS=20..30`). **Handlers run on worker pool threads, not the publisher's thread.** Treat all handlers as concurrent.

If `message.receiver` is set and the receiver is currently marked busy, the message is buffered in a per-receiver mailbox (FIFO, max 5000 per receiver, oldest dropped on overflow). When the receiver is marked available, up to `drain_per_available_edge` (default 256) buffered messages are delivered (`event_hub.py:139`).

Handler exceptions never crash the hub — `_safe_invoke` logs `CRITICAL` and continues (`event_hub.py:232`).

### Standard topics

| Topic                              | Publisher (examples)                                            | Subscriber                                              |
|------------------------------------|-----------------------------------------------------------------|---------------------------------------------------------|
| `socket_emit`                      | Many control nodes, `chat_publisher`, `NotifyUser`, `LLMClient` | `EmiEventRelay.socket_emit_handler`                     |
| `repo_update`                      | `event_repository`, calendar/todo/scheduler tools               | `EmiEventRelay.notify_ui_of_repo_update`, `DayflowScheduler._on_repo_update` |
| `proactive_suggestion`             | Suggestion producers                                            | `TicketDispatcherRegistry._on_proactive_suggestion` (fans out to every registered `TicketSurfaceAdapter.dispatch()`) |
| `proactive_suggestion_update`      | Suggestion producers                                            | `TicketDispatcherRegistry._on_proactive_suggestion_update` (fans out to `TicketSurfaceAdapter.notify_update()`) |
| `agent_progress_fact`              | `tool_caller`, `tool_result_handler`, `progress_emitter`        | `EmiEventRelay.agent_progress_emit_handler`             |
| `agent_progress_emit`              | UI progress emitters                                            | `EmiEventRelay.agent_progress_emit_handler`             |
| `afk_state_changed`                | `AFKMonitor`                                                    | `MusicAfkRelay`, `DayflowScheduler._on_afk_state_changed` |
| `dayflow_tick`                     | `dayflow_tick.py`                                               | `DayflowScheduler` / dayflow runtime                    |
| `dayflow_ticket_responded`         | `ticket_service`                                                | `DayflowScheduler._on_ticket_responded`                 |
| `task_request`                     | `manager_interface` tool                                        | task IR / manager invoker glue                          |
| `settings_changed`                 | `routes/user_settings`                                          | (no required subscriber — silenced by hub allow-list)   |

`settings_changed` is on a small allow-list of topics that may have zero subscribers without a warning (`event_hub.py:184`). Any other topic with no subscribers logs a warning when published.

### Sync vs async semantics

Everything past `publish` is asynchronous from the publisher's POV. There is no built-in request/response correlation — agents that need a reply use `request_id` and a separate response topic, or call directly through `manager_invoker.invoke`. `set_receiver_status` is the only built-in mechanism for serializing delivery to a particular consumer.

## Blackboard

Source: `app/assistant/lib/blackboard/Blackboard.py`.

The `Blackboard` is **per-manager-invocation** state (one per `MultiAgentManager.request_handler` call). It manages two things:

1. **Scope stack** (`self.scopes: List[dict]`): a LIFO stack of dictionaries. Index 0 is the global scope; each agent-to-agent call pushes a new top scope.
2. **Message log** (`self.messages: List[Message]`): a flat per-invocation transcript, auto-tagged with the current `scope_id`.

### Scope semantics

- `get_state_value(key, default=None)` — searches **top-down** through the scopes (most recent first), returning the first hit (`Blackboard.py:186`).
- `update_state_value(key, value)` — writes to the **current top scope only** (`Blackboard.py:193`).
- `update_global_state_value(key, value)` — writes to the bottom scope (`Blackboard.py:197`).
- `append_state_value(key, value)` — top-scope append; auto-initializes to `[]`; lists are extended, scalars appended.
- `add_msg(msg)` — appends to the global message log, stamps `msg.scope_id` with the current scope, and assigns a monotonic `metadata.history_id` plus the lifecycle flag set (`history_deleted`, `history_hidden`, `history_pinned`, `history_summarized`).

### Push / pop

```python
blackboard.push_call_context(calling_agent, called_agent, scope_id)
# called agent runs; it can read parent state via get_state_value (top-down search)
# but its writes go to the new top scope only
blackboard.pop_call_context()  # destroys the local scope, returns control to parent
```

The call stack stores `(caller, callee, scope_id)` tuples (`Blackboard.py:289`). Pop is paired one-for-one with push — this is the contract control nodes rely on for clean delegation isolation.

### Worked example: agent A calls agent B

```
push_call_context("A", "B", "scope-xyz")
  scopes = [global, A_local, B_local={}]   # B_local is empty
  call_stack = [(A->parent), (A->B, "scope-xyz")]

# Inside B:
get_state_value("task")        # found in global → returned
update_state_value("draft", x) # writes to B_local only
add_msg(some_msg)              # msg.scope_id = "scope-xyz"

pop_call_context()
  scopes = [global, A_local]   # B_local discarded
  # A still sees its own draft if it had one, never B's draft
```

### Reset

`reset_blackboard()` (`Blackboard.py:55`) is **critical for manager reuse** — it rebuilds the initial scope dict, clears the call stack, and resets `state_dict`. `MultiAgentManagerFactory` reuses manager instances in some paths, so a stale scope stack from a prior invocation would leak across requests if reset were skipped.

## Global Blackboard

Source: `app/assistant/global_blackboard/global_blackboard.py`.

`GlobalBlackBoard` is a **process-wide** in-memory message log + system-state summary. It is not a stack — it is a single flat list of `Message` objects guarded by an `RLock`.

### Memory bound

The log is capped at `_MAX_MESSAGES = 20000`; on overflow it trims to `_TRIM_TARGET = 15000` most-recent messages and logs a warning (`global_blackboard.py:71`). The `unified_log` SQLite table holds the durable record, so trimming the in-memory copy is lossless for any agent that queries history through the DB.

### What writes to it

- `agent_classes/NotifyUser.py:48` — every user-visible chat message.
- `agent_classes/EmiResultHandler.py:65,152` — task results and synthesized user messages.
- `control_nodes/master_room_chat_task_router_node.py:142` — guard messages on the master room path.
- `control_nodes/chat_task_router_node.py:170` — outbound chat messages.
- `context_engine/context_memo.py:110` — context memo writes.
- `lib/tools/one_shot_tool_runner/one_shot_tool_runner.py:441` — synthesized one-shot user messages.
- `ResourceManager._publish_to_global_blackboard` (`resource_manager.py:68`) — every resource value is mirrored into `global_blackboard.state_dict` under its `resource_id` so callers that read straight from the blackboard (rather than going through `ResourceManager`) still see fresh values.

### What reads from it

- `manager_runtime` / `RoomSessionManager` for cross-invocation chat-history rebuild.
- `context_memo`, `entity_card_injector`, `dj_manager`, daily-context generator — anywhere that needs recent chat slices without hitting SQLite.
- `get_recent_chat_since_utc(...)` (`global_blackboard.py:196`) is the canonical retrieval API for routing/history building. Defaults are conservative (only `is_chat=True`, excludes meta tags via `DEFAULT_EXCLUDED_CHAT_TAGS`, excludes summarized messages, excludes slash commands unless explicitly allowed).

`add_msg` auto-tags messages produced under a non-`normal` `room_mode` (e.g. `doc_creation_mode`) into `sub_data_type` so dayflow can exclude them via tag filters without touching call sites.

### State dict

`get_state_value(key, default)` / `update_state_value(key, value)` / `append_state_value(key, value)` give a flat `state_dict` for arbitrary cross-component values. This is *not* the `Blackboard` scope stack — it is process-global and unscoped. `system_state_summary` is a separate dict-of-lists keyed by category (`news`, `weather`, `calendar`, `scheduler`, `email`, `todo_task`).

## ResourceManager

Source: `app/resource_manager/resource_manager.py`.

ResourceManager is the canonical access point for files under `resources/`. Each file is registered by its **filename stem** as a `resource_id`:

```
resources/user/resource_user_data.json     →  resource_id="resource_user_data"
resources/assistant/assistant_core.json    →  resource_id="assistant_core"
resources/email_instructions.md            →  resource_id="email_instructions"
```

JSON files load as Python objects (`dict`/`list`); `.md` / `.txt` / no-extension files load as raw strings (`resource_manager.py:333`).

### Loader passes (`load_all_from_directory`)

1. **JSON pass** — every `.json` is read, cached, and mirrored into `global_blackboard`.
2. **Template pass** — `compile_templates()` renders Jinja templates under `resources/templates/**` against the JSON resources loaded in pass 1, writing concrete output to the same relative path under `resources/`. `StrictUndefined` is enforced (`resource_manager.py:270`).
3. **Text pass** — `.md`/`.txt`/no-extension files are loaded as concrete resources. Templated content (containing `{{` or `{%`) raises — concrete resources must be fully resolved (`resource_manager.py:54`).

Skip rules: hidden files/dirs, `tmp`/`tests`/`__pycache__`/`templates`/`day_context`/`pointers` directories, and `README*`.

### Read API

```python
value = DI.resource_manager.get_resource(
    scope_context=scope_ctx,        # ScopeContext or dict-like with resources policy
    resource_id="resource_user_data",
    required=True,                  # raise if missing
)
```

`scope_context.resources` must declare `allowed_global_resources` (use `["all"]` to bypass) and may declare `denied_resources`. Denied resources raise `PermissionError`; not-allowed resources also raise. There is no fallback — scope policy is enforced at the read site.

`get_resources(scope_context, resource_ids=[...], required=False)` is a convenience wrapper.

### Staleness check

Every `get_resource` call does one `os.stat()` against the backing file and auto-reloads if the file's `mtime` is newer than the cached `mtime` (`resource_manager.py:97`). This eliminates the bug class where a pipeline writes a resource file directly (bypassing `update_resource`) and agents keep reading the boot-time cached copy. Cost is microseconds per read; resources updated in-memory only (via `persist=False`) skip this check entirely because they have no anchored mtime.

### Write API

```python
DI.resource_manager.update_resource(resource_id, value, persist=True)
```

- `persist=True` (default): atomic write via tempfile + `os.replace`, per-file lock, `os.fsync`. Cache is updated and re-anchored to the new mtime so future external writes are still detected.
- `persist=False`: in-memory only. Used for status/snapshot resources that are rebuilt on every tick and never need to survive a restart. Canonical examples:
  - `RoutineManager._save_state_unlocked` calls `update_resource(resource_id, self._state, persist=False)` after writing the JSON file directly via `write_json_file`, so the file is the source of truth and the cache is just kept in sync (`routine_manager.py:213`).
  - `ManagerInvoker._publish_invocation_status` writes `resource_manager_invocation_status` with `persist=False` (`manager_invoker.py:69`).
  - `RoutineManager.get_runtime_concurrency_status` likewise (`routine_manager.py:877`).
  - `get_weather` writes `resource_weather` (`get_weather.py:234`).

`refresh_resource(resource_id)` force-reloads a single resource from disk.

### Atomic write semantics

`_write_file` (`resource_manager.py:342`) acquires a per-path `RLock`, writes to `.<name>.<rand>` tempfile in the same directory, `flush` + `fsync`, then `os.replace` onto the final path. Concurrent writers cannot corrupt files; readers either see the old or the new file, never a half-written one.

## Message / ToolMessage / ToolResult

Source: `app/assistant/utils/pydantic_classes.py`.

These three Pydantic models are the wire format for everything that flows between agents, tools, managers, the event hub, and the blackboards.

### `Message`

The base envelope. ~50 fields; the load-bearing ones:

| Field                  | Purpose                                                                  |
|------------------------|--------------------------------------------------------------------------|
| `id`                   | UUID4 — used as the unified-log primary key.                             |
| `data_type`            | High-level classification (`agent_msg`, `tool_result`, `chat`, …).        |
| `sub_data_type`        | List of routing/scoping tags (e.g. `["chat", "slash_command"]`).         |
| `sender` / `receiver`  | Agent/manager/service names (or `"User"`).                               |
| `content`              | Free-text body.                                                          |
| `data`                 | Arbitrary structured payload.                                            |
| `agent_input`          | What an agent sees as its input (string or dict).                        |
| `event_topic`          | Set by publishers before `event_hub.publish(...)`.                       |
| `metadata`             | Open dict — used heavily for `history_id`, `item_id`, `room_mode`, etc.  |
| `request_id`           | Correlates a request across hops (used by `manager_invoker`).            |
| `scope_id`             | Set by `Blackboard.add_msg` from the current call-context scope.         |
| `room_*`               | Room-scoped messaging contract (room_id, surface, visibility, policy).   |
| `room_speaker_*`       | Multi-speaker room identity.                                             |
| `transport_*`          | Transport-level identifiers (sms id, slack channel, telegram thread).    |
| `scope_context`        | Canonical `ScopeContext` for scoped entities (history/resources/tools).  |
| `referenced_pods`      | Pod headers hydrated from message text by `PodInjector`.                 |

Constructed in: transport handlers, control nodes, agent runtime, tools. Parsed everywhere downstream. Pydantic validation is strict; do not bypass the constructor.

### `ToolResult`

```python
class ToolResult(BaseModel):
    result_type: Optional[str] = None
    content: str = ""
    data_list: Optional[List[Dict[str, Any]]] = []
    data: Optional[Any] = None
```

Returned by every tool. `result_type` keys into `RESULT_TYPE_HANDLERS_NEW` for dispatch. **Per project memory: `content` must never be truncated** — agents need the full payload to make decisions.

### `ToolMessage`

`Message` plus tool-specific fields: `tool_name`, `tool_data` (arguments), `tool_result`. Used by `ToolCaller` and `ToolResultHandler` to round-trip a tool invocation through the agent loop.

## Runtime registry

Source: `app/assistant/runtime/runtime_registry.py`.

The runtime registry tracks every monitored thread and executor in the process so the concurrency dashboard can answer "what is running right now?". It is process-global (`get_runtime_registry()` lazy-singleton, `runtime_registry.py:306`).

### Helpers

```python
from app.assistant.runtime import start_monitored_thread, MonitoredThreadPoolExecutor

thread = start_monitored_thread(
    owner="event_hub",
    name="event-handler-hub-dispatcher",
    target=self.process_queue,
    daemon=True,
    kind="dispatcher_loop",
    metadata={"component": "event_hub"},
)

pool = MonitoredThreadPoolExecutor(
    name="event-handler-hub",
    owner="event_hub",
    max_workers=24,
)
```

`start_monitored_thread` wraps `target` so the registry is notified on start, on heartbeat (via `mark_thread_heartbeat`), and on stop (with optional error string). `MonitoredThreadPoolExecutor` wraps `concurrent.futures.ThreadPoolExecutor` and tracks per-task submitted/started/finished/failed counts plus saturation ratio.

### Dashboard

- `GET /api/runtime/concurrency` — JSON snapshot combining `routine_manager`, `manager_invoker.get_invocation_status()`, and `runtime_registry.snapshot()`.
- `GET /debug/runtime/concurrency` — HTML dashboard (`templates/runtime_concurrency.html`).

The snapshot also enumerates `threading.enumerate()` and reports `unregistered_threads` — anything alive in the Python runtime but not registered with the registry. This catches third-party libraries (APScheduler workers, requests connection pools) without forcing them to register.

## Common pitfalls

- **Don't reach into `DI` from module top-level.** The DI proxy reads from a registry that is empty until `initialize_services` runs. Top-level `DI.event_hub` resolves at import time → `AttributeError`. Always import `DI` and dereference it inside a function call. Standalone scripts must `import app.assistant.tests.test_setup` before any project import (per `CLAUDE.md`).
- **Don't write to `ResourceManager` from a hot path.** `update_resource(persist=True)` does a fsync'd atomic file write per call. Use `persist=False` for status/snapshot resources that are rebuilt every tick (see `RoutineManager._save_state_unlocked` for the canonical pattern).
- **Don't hold a `Blackboard` reference across LLM calls.** Per the project rule "no DB lock over LLM calls", the same logic applies to scope state — an LLM call can take many seconds and the blackboard's lock-free scope stack is only safe within a single manager invocation. Read what you need, call the LLM, then re-read.
- **`event_hub.publish` is fire-and-forget.** No reply, no acknowledgement, no ordering across topics. Use `request_id` and a paired response topic if you need correlation, or call `DI.manager_invoker.invoke(...)` directly for synchronous request/response.
- **Handler exceptions are swallowed by the hub** (logged `CRITICAL`, never re-raised — `event_hub.py:232`). If a handler must signal failure upstream, it has to publish an error topic itself.
- **Subsystem-gated services may not exist.** `signal_router`, `ingest_service`, `pod_classifier_service`, `dayflow_scheduler`, `background_task_manager`, `dj_manager` are all gated by `subsystems.yaml`. Use `getattr(DI, "name", None)` and check.
- **`Blackboard` (per-invocation) and `GlobalBlackBoard` (process-wide) are different classes.** Same `add_msg` / `get_state_value` method names, completely different scopes. Read what you're handed before assuming.
- **Resource updates from a third-party tool aren't seen until the next read.** The mtime-based staleness check kicks in on the next `get_resource` call (microsecond cost), but if a long-running consumer cached the value it won't see the update until it re-reads.

## How to add a new service to DI

```python
# 1. Construct the service somewhere in initialize_services or initialize_system,
#    after any services it depends on are already registered.
my_service = MyService(event_hub=event_hub, resource_manager=resource_manager)

# 2. Register under a stable key.
ServiceLocator.register("my_service", my_service)

# 3. Consume via the proxy.
from app.assistant.ServiceLocator.service_locator import DI
DI.my_service.do_thing()
```

Place initialization in **Phase 1** (`bootstrap.initialize_services`) if other Phase-1 services depend on it. Place in **Phase 2** (`initialize_system`) if it needs `socket_manager` or any Phase-2 service. If the service starts a background thread, prefer `start_monitored_thread` so it shows up in the runtime dashboard.

## How to add a new EventHub topic

```python
# Producer side — set event_topic on a Message and publish.
msg = Message(
    event_topic="my_new_topic",
    sender="my_component",
    receiver="downstream_service",   # optional; required for receiver-busy buffering
    data={"payload": ...},
)
DI.event_hub.publish(msg)

# Consumer side — register a handler at startup (typically in initialize_system
# or in your service's __init__).
DI.event_hub.register_event("my_new_topic", self._on_my_new_topic)

def _on_my_new_topic(self, message: Message) -> None:
    # Runs on a worker pool thread. Treat as concurrent.
    ...
```

Register **before** any producer publishes — a topic with no subscribers logs a warning unless it is on the silent allow-list (currently only `settings_changed`, `event_hub.py:184`). If your handler does heavy work, delegate to your own queue/thread; the worker pool is shared by every event.

## Key files

| File                                                                | Role                                       |
|---------------------------------------------------------------------|--------------------------------------------|
| `app/assistant/ServiceLocator/service_locator.py`                   | DI container + `DI` proxy                  |
| `app/bootstrap.py`                                                  | Phase 1 service registration + shutdown    |
| `app/assistant/initialize_system.py`                                | Phase 2 service registration               |
| `app/create_app.py`                                                 | Flask wiring; calls both phases            |
| `app/assistant/event_hub/event_hub.py`              | Pub-sub bus implementation                 |
| `app/assistant/lib/blackboard/Blackboard.py`                        | Per-invocation scope stack + message log   |
| `app/assistant/global_blackboard/global_blackboard.py`              | Process-wide message log + state dict      |
| `app/resource_manager/resource_manager.py`                          | JSON/text resource cache + atomic writes   |
| `app/assistant/utils/pydantic_classes.py`                           | `Message`, `ToolMessage`, `ToolResult`, `ScopeContext` |
| `app/assistant/manager_runtime/manager_invoker.py`                  | Canonical manager invocation entrypoint    |
| `app/assistant/runtime/runtime_registry.py`                         | Monitored thread + executor registry       |
| `app/routes/runtime_monitor.py`                                     | `/api/runtime/concurrency` + dashboard     |
