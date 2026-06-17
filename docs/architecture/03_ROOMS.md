# Rooms

A **Room** is a scoped conversation channel that defines identity, rules, safety,
permissions, and policy for a specific communication context (a UI chat, a Slack
channel, a Telegram chat, an SMS thread, or an internal system surface).

## Room Directory Structure

Each room lives under `app/assistant/rooms/<room_id>/` and contains **one config
file** plus an optional permission file:

```
<room_id>/
  ROOM.md       # YAML frontmatter (policy / permissions / access) + markdown body
  scope.yaml    # (optional) authoritative permission scope — see SCOPE.md
```

The canonical contract is `rooms/ROOM_CONTRACT.md` (strict). Pre-2026-05-09 each
room was 7-9 separate JSON files (`policy.json`, `permissions.json`, `access.json`,
plus six `resource_*.json` wrappers); that shape was collapsed into a single
`ROOM.md` with no backwards-compat fallback. (`master_room/` still has the old
loose `.json` files on disk, but the loader reads only `ROOM.md`.)

### ROOM.md frontmatter

Three required top-level mappings — `policy`, `permissions`, `access`:

- **`policy`** — `policy_id`, `manager_name` (defaults to `room_manager` if
  absent), `surface`, `default_visibility`, `default_context_id`,
  `authority_level`, `history` (`scope`, `max_hours`), `retention`
  (`write_unified_log`, `write_kg`, `allow_fact_extraction`), `delivery`
  (`auto_send`, `allow_initiation`), `privacy`, `participant_identity`. Optional:
  `chat_compaction`, `mode_manager_overrides`, `default_room_mode`.
- **`permissions`** — `tool_classes` (informational / transformational /
  external_action / sensitive), `allow_images`, `pod_scopes`, and optional
  `per_manager` rules. (When a `scope.yaml` is present it is authoritative for
  permissions — see below.)
- **`access`** — `allowed_global_resources`, `allowed_entity_cards`,
  `pinned_entities`, `blocked_entities`, `rag_scopes`, `shared_chat_room_ids`,
  and (for ingesting rooms) `chat_ingestion_entitled_rooms` + `ingestion_pod_kinds`.

### ROOM.md body → blackboard keys

The markdown body is human-editable prose injected into agent prompts. H1
sections map to the named blackboard keys agents read at prompt time (order
doesn't matter; multiple sections targeting one key concatenate; the H1 line is
preserved in the injected text; unknown headers fold into the most-recent
recognized section). Per `ROOM_CONTRACT.md` and
`room_resource_loader._HEADER_TO_BB_KEY`:

| H1 section            | Blackboard key                      |
|-----------------------|-------------------------------------|
| `# Identity`          | `room_identity`                     |
| `# Room context`      | `room_identity` (appended)          |
| `# Conversation`      | `room_conversation`                 |
| `# Safety`            | `room_safety`                       |
| `# Room facts`        | `room_facts` (optional)             |
| `# Participant facts` | `room_participant_facts` (optional) |

`# Identity` content is required — every room must declare who it is. A missing
or malformed `ROOM.md`, or missing identity content, raises loudly
(`load_room_context_for_manager`).

### scope.yaml (permission scope)

A room may add a sibling `scope.yaml` declaring its permission scope (the
unified-scope model — see `docs/architecture/SCOPE.md`). When present it is the
**authoritative source** for the permission bucket (`tools`, `pods`, `resources`,
`entities`, `cards`, `writes`, `approval`, plus `delivery.auto_send` /
`allow_initiation`) and **replaces** the ROOM.md-derived equivalents wholesale
(`room_scope_builder._overlay_scope_yaml_permission`). Identity, room-behavior
(history / retention / execution), and derived fields (allowed_reply_types,
skills) stay builder-computed. Rooms without a `scope.yaml` are unaffected.
`scope.yaml` is PERMISSION ONLY — identity fields (`scope_id`, `owner_id`,
`actor_id`, `surface`, `reply_to`, …) are stamped per request at load time, never
authored in the file.

## Room Types

`manager_name` defaults to `room_manager` when unset; `authority_level` is per
ROOM.md (or the surface default when omitted).

| Room                  | Surface  | Authority | Manager                        |
|-----------------------|----------|-----------|--------------------------------|
| `master_room`         | ui       | 99        | `master_room_manager`          |
| `dayflow_orchestrator`| system   | 95        | `dayflow_orchestrator_manager` |
| `emi_code_room`       | ui       | 60        | `emi_code_room_manager`        |
| `kg_dev_room`         | ui       | 50        | `kg_dev_room_manager`          |
| `task_create`         | ui       | 50        | `task_spec_manager`            |
| `doc_editor`          | ui       | 50        | `doc_editor_manager`           |
| `slack/<channel_id>`  | slack    | 70        | `room_manager`                 |
| `telegram/<chat_id>`  | telegram | 40        | `room_manager`                 |
| `phil`, `1234`        | sms      | default   | `room_manager`                 |

## Master Room

The `master_room` is the owner's primary UI surface with the highest trust:

- **Authority level 99** — full tool surface, all entity cards
  (`allowed_entity_cards: [all]`), KG writes + fact extraction, external side
  effects, and `pod_scopes: [all]` (cross-room owner visibility — it sees pods
  minted by every other room). Most of these are MORE permissive than the
  fail-closed defaults and are declared explicitly in `scope.yaml`; omitting them
  would silently downgrade the room.
- **Owner-only visibility** (`default_visibility: owner_only`).
- **Dayflow delegation** — `master_room::chat_gate` can hand off to the dayflow
  orchestrator (see Dayflow Blocking).
- **Mode overrides** — `planning_mode → master_room_planning_manager`,
  `game_mode → master_room_game_manager` (frontmatter `mode_manager_overrides`).
- **24-hour history window** (`history.scope: time_bounded`, `max_hours: 24`).

The lone `per_manager` rule in `master_room/scope.yaml` narrows `emi_team_manager`'s
direct tool pool to its delegate-managers + operational tools (a cost reduction
on its tool-narrower prompt, not a capability cut — leaf tools stay reachable
through those managers).

### Master Room chat_gate vs Room chat_gate

| Feature             | `room::chat_gate`         | `master_room::chat_gate`            |
|---------------------|---------------------------|-------------------------------------|
| Direct reply        | Yes                       | Yes                                 |
| Switchboard handoff | Yes                       | Yes                                 |
| Dayflow delegation  | No                        | Yes                                 |
| LLM (tier)          | openai / `powerful`       | gemini / `mini` (`gemini-3-flash-preview`, faster) |

(`config.yaml` declares a provider + `model_tier`; tiers resolve via
`app/configs/llm_classes_dict.py` — `mini`+`gemini` → `gemini-3-flash-preview`.)

## Room Session Manager

`app/assistant/room_session_manager/room_session_manager.py` orchestrates the
full lifecycle of room-scoped communication. The work is decomposed across the
`room_session_manager/services/` package (~22 focused services):

### Inbound
- **`room_ingress_service.py`** (`RoomIngressService`) — the inbound spine.
  Extracts slash commands, routes @mentions and active mode sessions, builds the
  `request_data` blackboard payload and the manager `Message`, and invokes the
  room manager. Holds the slash/mention routers.
- **`room_slash_command_router.py`** — handles `/plan`, `/done`, `/end`,
  `/cancel`, `/task`, `/doc`, `/play` (geoguessr), `/actas`, `/pod expand`.
  Returns a `SlashCommandResult` (early-return reply or normalized body to
  continue the pipeline).
- **`room_mention_router.py`** — intercepts `@<worker> <body>` and delivers to an
  active worker's mailbox, short-circuiting the manager pipeline.
- **`pod_command.py`** — deterministic `/pod expand <prefix>` (recent-room match
  + `pod_utils.read_pod_gated`, no LLM).
- **`question_answer_ingress_service.py`** — parses `ANSWER <qid> ...` channel
  replies back into pending questions.

### Context build
- **`room_history_builder.py`** (`RoomHistoryBuilder`) — reads room history
  purely from `unified_log_2026` (the global blackboard is no longer consulted),
  cross-room aware (`shared_chat_room_ids`), 24h-capped, dedup + pin/suppress
  honoring, with dayflow-specific include/exclude tag rules.
- **`room_scope_builder.py`** — builds the canonical `ScopeContext` from
  policy/permissions/access, then overlays `scope.yaml` if present.
- **`room_resource_injection_service.py`** — keyword/always-inject resource
  subscription matching into a prompt-safe block.
- **`room_policy_service.py`** — stateless resolvers: manager name, authority
  level, **persistence mode**, unified-log gating, and **chat_compaction**.

### Modes & sessions
- **`room_binding_session_service.py`** — generic `surface::room::context` mode
  session store; **`plan_session_service`**, **`task_creation_session_service`**,
  **`doc_creation_session_service`**, **`geoguessr_session_service`** subclass it.
- **`actas_session_service.py`** — sticky `/actas` principal binding per room.
- **`room_mode_exit_service.py`** — writes a brief summary note when a mode
  (planning / task / doc / game) ends so later chat turns have context.

### Outbound & persistence
- **`post_room_service.py`** — extracts reply text and builds the
  `OutboundIntent` from the manager result.
- **`room_message_persistence_service.py`** — persists inbound/outbound messages.
- **`room_metadata_repair_service.py`** — guarantees every emitted chat message
  carries `room_id` / `room_surface` / `room_context_id`.

### Async chat compaction
- **`room_chat_summary.py`** + **`room_summary_service.py`** — opt-in, per-room,
  background history compaction (one thread per room, non-blocking lock). A room
  enables it via `policy.chat_compaction: {enabled: true, summary_agent: ...}`;
  absent/disabled means the room is never summarized. The summary agent may be
  room-specific (e.g. `master_room::room_summary`) or the generic `room_summary`.

### Mode → manager routing

A room declares mode→manager mappings in ROOM.md `policy.mode_manager_overrides`.
At turn prep (`RoomSessionManager._prepare_turn_context`) the inbound
`room_mode` (normalized by `RoomIngressService._normalize_room_mode`) selects an
override manager; e.g. in `master_room`:

```yaml
mode_manager_overrides:
  planning_mode: master_room_planning_manager
  game_mode: master_room_game_manager
```

Within a manager, the mode also selects the entry agent via the manager's
`flow_config.flow.<mode>.source_agent` (`RoomIngressService._resolve_mode_source_agent`).
Task and doc creation are NOT master_room modes — `/task` and `/doc` redirect to
the dedicated `task_create` / `doc_editor` rooms (their own managers).

### Message persistence modes

Resolved by `room_policy_service` from the room's persistence mode:
- `global_blackboard_only`
- `global_blackboard_and_unified_log`

Unified-log persistence additionally respects the room's `retention.write_unified_log`.

## Cross-Room Communication

- Rooms reference each other via `access.shared_chat_room_ids`; the history
  builder pulls those rooms' messages into context (cross-room dedup applied).
- `access.chat_ingestion_entitled_rooms` lists rooms whose chat a consuming room
  ingests (the dayflow orchestrator ingests `master_room` chat as context-only).
- `access.ingestion_pod_kinds` declares pod kinds (by `kind` + `source_kind`) a
  room ingests — e.g. dayflow ingests `image` pods from `ring_doorbell_significant`.
- `pod_scopes` controls which rooms' pods a room may read (`[self]` default,
  `[all]` for owner surfaces).

## Dayflow Blocking

When the user chats in `master_room`, ingress calls
`block_dayflow_orchestrator_for_master_chat()`
(`dayflow_orchestrator/orchestrator_status.py`), which stamps a
`blocked_until_utc` on the orchestrator status `MASTER_ROOM_BLOCK_SECONDS`
(currently 180s) into the future, so dayflow doesn't act while the user is
actively chatting.

## How to Add a New Room

1. Create `app/assistant/rooms/<room_id>/`.
2. Write one `ROOM.md`: frontmatter (`policy` / `permissions` / `access`) + a
   body with at least `# Identity`.
3. Optionally add `scope.yaml` for an authoritative permission scope.
4. Set `policy.manager_name` (or rely on the `room_manager` default), `surface`,
   `authority_level`, retention, and mode overrides.
5. For surface-routed rooms, mint the id via `make_telegram_room_id` /
   `make_slack_room_id`; `room_bootstrap.py` copies the matching `_templates/`
   (`slack_standard` / `telegram_standard`) ROOM.md + scope.yaml into place.

See `skills/extending-emi-rooms/SKILL.md` for the field reference.

## Surface-native room ids

- Telegram: `telegram/<chat_id>` (`make_telegram_room_id`)
- Slack: `slack/<channel_id>` (`make_slack_room_id`)
- SMS: `sms/<sender_or_contact_id>`
- UI / per-feature rooms: bare names — `master_room`, `doc_editor`, `kg_dev_room`,
  `emi_code_room`, `task_create`, `dayflow_orchestrator`.

## Key Files

| File                                                       | Purpose                                            |
|------------------------------------------------------------|----------------------------------------------------|
| `rooms/ROOM_CONTRACT.md`                                   | The strict single-`ROOM.md` contract               |
| `rooms/room_resource_loader.py`                            | Reads `ROOM.md` → flat context dict + blackboard keys |
| `rooms/room_bootstrap.py`                                  | Surface room-id minting + template copy            |
| `room_session_manager/room_session_manager.py`             | Session lifecycle orchestration                    |
| `room_session_manager/contracts.py`                        | `InboundEnvelope`, `OutboundIntent`                |
| `room_session_manager/services/room_ingress_service.py`    | Inbound spine (slash/mention/mode routing)         |
| `room_session_manager/services/room_history_builder.py`    | unified_log-backed history build                   |
| `room_session_manager/services/room_scope_builder.py`      | `ScopeContext` build + scope.yaml overlay          |
| `room_session_manager/services/room_policy_service.py`     | Manager/authority/persistence/compaction resolvers |
| `room_session_manager/services/room_slash_command_router.py` | Slash command dispatch                           |
| `agents/room/chat_gate/`, `agents/master_room/chat_gate/`  | Default / master room chat gates                   |
| `control_nodes/post_room_finalize_node.py`                 | Post-room finalize (closes source dayflow items)   |
