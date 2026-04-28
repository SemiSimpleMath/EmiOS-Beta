# Rooms

A **Room** is a scoped conversation channel that defines identity, rules, safety, permissions, and policy for a specific communication context.

## Room Directory Structure

Each room lives under `app/assistant/rooms/<room_id>/`:

```
<room_id>/
  resource_identity.json          # Who Emi is, relationship to user
  resource_room_context.json      # Additional context appended to identity
  resource_conversation.json      # Conversation style/mechanics
  resource_safety.json            # Safety guardrails
  resource_room_facts.json        # Key facts about the room
  resource_participant_facts.json # Facts about participants
  policy.json                     # Delivery, retention, authority
  permissions.json                # Tool permissions, media rights
  access.json                     # Resource/entity access lists
```

This contract is strict — missing required files should fail room load. See `app/assistant/rooms/ROOM_CONTRACT.md`.

## Room Types

| Room | Surface | Authority | Manager |
|------|---------|-----------|---------|
| `master_room` | UI | 99 (full) | `master_room_manager` |
| `justin` | UI | default | `room_manager` |
| `jamie` | Telegram | default | `room_manager` |
| `slack/<channel_id>` | Slack | default | `room_manager` |
| `telegram/<chat_id>` | Telegram | default | `room_manager` |
| `dayflow_orchestrator` | system | 95 | dayflow pipeline |

## Master Room

The `master_room` is the primary user interface with special privileges:

- **Authority level 99** — full access to all resources and entity cards
- **Owner-only visibility** — messages not shared to other rooms
- **Dayflow delegation** — chat_gate can delegate tasks to the dayflow orchestrator
- **Mode overrides**: planning mode, task creation mode, doc creation mode, game mode
- **24-hour context window** for scoped history

### Master Room chat_gate vs Room chat_gate

| Feature | `room::chat_gate` | `master_room::chat_gate` |
|---------|-------------------|--------------------------|
| Direct reply | Yes | Yes |
| Switchboard handoff | Yes | Yes |
| Dayflow delegation | No | Yes |
| Model | gpt-5.4 | gemini-3-flash-preview (faster) |

## policy.json

```json
{
  "policy_id": "room_policy::master_room::v1",
  "manager_name": "master_room_manager",
  "surface": "ui",
  "default_visibility": "owner_only",
  "authority_level": 99,
  "retention": {
    "write_unified_log": true,
    "write_kg": true,
    "allow_fact_extraction": true
  }
}
```

## access.json

Controls what resources and entities the room can see:
- `allowed_entity_cards`: List of entity card IDs
- `allowed_global_resources`: List of resource IDs
- `rag_scopes`: RAG search scopes
- `shared_chat_room_ids`: Cross-room chat visibility (e.g., `["master_room"]`)
- `chat_ingestion_entitled_rooms`: Rooms whose chat is ingested for dayflow context
- Resource subscriptions with trigger modes (keyword, rag, both) and always_inject flags

## Room Session Manager

`app/assistant/room_session_manager/room_session_manager.py` (~1600 lines)

Handles the full lifecycle of room-scoped communication:

### Inbound Processing
1. Transport message -> `InboundEnvelope` (surface, room_id, speaker, content, metadata)
2. Slash command extraction (`/plan`, `/done`, etc.)
3. Session mode routing (planning, task creation, doc creation, game)
4. Room context loading (identity, conversation, safety, facts)

### Manager Invocation
1. Determine room manager (from `policy.json` or mode override)
2. Build scoped message history (cross-room aware)
3. Inject allowed resources (keyword-based or always_inject)
4. Call `ManagerInvoker.invoke(manager, message)`

### Outbound Delivery
1. Format reply for surface (SMS, Slack, Telegram, UI)
2. Persist to global blackboard
3. Optionally persist to unified_log (per retention policy)
4. Deliver via transport adapter

### Message Persistence Modes
- `global_blackboard_only` — default for Slack
- `global_blackboard_and_unified_log` — default for UI/SMS/Telegram

### Specialized Modes
```
"normal"              -> source_agent from flow_config
"planning_mode"       -> master_room::plan_mode
"task_creation_mode"  -> master_room::task_spec_writer
"doc_creation_mode"   -> master_room::doc_writer
"game_mode"           -> master_room::geoguessr_host
```

## Cross-Room Communication

- Rooms can reference each other via `shared_chat_room_ids` in `access.json`
- Cross-room messages get excerpt formatting with `cross_room_tf: true` metadata
- Dayflow orchestrator ingests master_room chat as context-only items

## Dayflow Blocking

When the user chats in master_room, `block_dayflow_orchestrator_for_master_chat()` pauses dayflow for 180 seconds to avoid simultaneous interaction.

## How to Add a New Room

1. Create directory: `app/assistant/rooms/<room_id>/`
2. Create all required files per the room contract (see above)
3. Configure `resource_identity.json` with Emi's role in this room
4. Set `policy.json` with manager, surface, authority level, retention rules
5. Set `access.json` with allowed resources and entity cards
6. Set `permissions.json` with allowed/blocked tools
7. Configure transport routing if needed (Slack channel map, Telegram chat ID, etc.)

## Key Files

| File | Purpose |
|------|---------|
| `rooms/ROOM_CONTRACT.md` | Room file contract specification |
| `rooms/room_resource_loader.py` | Loads room resource files |
| `rooms/room_bootstrap.py` | Room creation/bootstrap |
| `room_session_manager/room_session_manager.py` | Session lifecycle management |
| `room_session_manager/contracts.py` | InboundEnvelope, OutboundIntent |
| `room_session_manager/services/room_ingress_service.py` | Inbound routing |
| `room_session_manager/services/room_message_persistence_service.py` | Message persistence |
| `agents/room/chat_gate/` | Default room chat gate agent |
| `agents/master_room/chat_gate/` | Master room chat gate agent |
| `control_nodes/master_room_chat_task_router_node.py` | Master room routing |
| `control_nodes/post_room_finalize_node.py` | Post-room processing |
