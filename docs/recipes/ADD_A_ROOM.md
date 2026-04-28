# Recipe: Add a new room

A room is a scoped conversation channel — separate identity, separate permissions, separate authority level. You add a new room when:

- You're integrating a new transport (a new Slack workspace, a new Telegram bot).
- You want a separate conversational context (a "Justin" room for a specific user with restricted access).
- You're building a system-only channel (like `dayflow_orchestrator`).

Read [03_ROOMS.md](../architecture/03_ROOMS.md) and the contract spec at `app/assistant/rooms/ROOM_CONTRACT.md` first.

## Directory layout

```
app/assistant/rooms/<room_id>/
  resource_identity.json
  resource_room_context.json
  resource_conversation.json
  resource_safety.json
  resource_room_facts.json
  resource_participant_facts.json
  policy.json
  permissions.json
  access.json
```

All nine files are required. Missing files fail room load — the contract is intentionally strict so accidents (e.g., forgot the safety file) surface immediately.

For canonical examples, look at `app/assistant/rooms/master_room/` (full-authority UI) and `app/assistant/rooms/justin/` (restricted UI room).

## File-by-file

### `resource_identity.json`

Who Emi *is* in this room. Affects voice, persona, what the assistant calls itself.

```json
{
  "resource_id": "resource_identity",
  "room_id": "<room_id>",
  "name": "Emi",
  "role_in_room": "Personal assistant for <user_name>",
  "voice": "casual, conversational, brief",
  "relationship": "<one-sentence description of who Emi is to whoever's in this room>"
}
```

### `resource_room_context.json`

Additional context appended to identity. Useful for room-specific framing ("this is the Slack channel where we coordinate the household.").

### `resource_conversation.json`

Conversation style/mechanics — turn-taking, formality, message length defaults.

### `resource_safety.json`

Safety guardrails for the room. What topics are off-limits, what disclaimers must accompany certain answers.

### `resource_room_facts.json` and `resource_participant_facts.json`

Static room facts and per-participant facts respectively. Pre-loaded into agent prompts to avoid having to re-state context every turn.

### `policy.json`

Most load-bearing of the lot. Defines which manager handles inbound, what surface this room presents on, retention rules.

```json
{
  "policy_id": "room_policy::<room_id>::v1",
  "manager_name": "room_manager",
  "surface": "ui",
  "default_visibility": "owner_only",
  "authority_level": 50,
  "retention": {
    "write_unified_log": true,
    "write_kg": false,
    "allow_fact_extraction": false
  }
}
```

- `manager_name` — which Manager processes inbound (`master_room_manager`, `room_manager`, ...).
- `surface` — `"ui"`, `"sms"`, `"slack"`, `"telegram"`, or `"system"`.
- `default_visibility` — `"owner_only"` (private) vs `"shared"` (visible cross-room when configured).
- `authority_level` — what tools this room can authorize. `99` is master, `50` is restricted, `95` for system rooms like dayflow.
- `retention.write_kg` — whether messages from this room get fed to the KG ingest pipeline. Most rooms are `false`; only master and similarly-trusted rooms are `true`.
- `retention.allow_fact_extraction` — paired with `write_kg`; whether the resolver pulls facts from this room's chat.

### `permissions.json`

Tool allowlist / blocklist for the room.

```json
{
  "allowed_tools": ["get_weather", "get_time"],
  "blocked_tools": ["send_email", "kg_create_node"]
}
```

If `allowed_tools` is missing, the room manager's defaults apply. If both lists are present, blocked wins.

### `access.json`

Resource and entity card visibility, plus cross-room sharing.

```json
{
  "allowed_entity_cards": ["entity_card::primary_user"],
  "allowed_global_resources": ["resource_user_data"],
  "rag_scopes": ["chat", "doc"],
  "shared_chat_room_ids": ["master_room"],
  "chat_ingestion_entitled_rooms": [],
  "resource_subscriptions": [
    {"resource_id": "resource_calendar", "trigger_mode": "keyword", "always_inject": false}
  ]
}
```

- `allowed_entity_cards` — which cards inject into prompts. Use `["all"]` for full access (master_room).
- `allowed_global_resources` — which resource files the room can see. `["all"]` for master.
- `shared_chat_room_ids` — cross-room visibility. If room A includes B here, B's chat appears in A's history with `cross_room_tf: true`.
- `chat_ingestion_entitled_rooms` — rooms whose chat gets ingested into THIS room's dayflow context.
- `resource_subscriptions` — declarative resource injection rules (keyword-triggered vs always-inject).

## Wire transport routing

Rooms are per-transport, but the wiring lives outside the room directory:

- **UI rooms**: registered in the WebSocket handshake (`app/socket_handlers.py`). Every UI client maps to a room id at connection time.
- **SMS rooms**: phone-number-to-room mapping in the Twilio inbound handler.
- **Slack rooms**: channel-to-room mapping in `configs/slack_room.json`.
- **Telegram rooms**: chat-id-to-room mapping in the Telegram webhook handler.

See [18_TRANSPORTS.md](../architecture/18_TRANSPORTS.md) (when written) for the full per-transport wiring.

## Test it

```bash
.venv\Scripts\python.exe -c "
import app.assistant.tests.test_setup
from app.assistant.rooms.room_resource_loader import load_room_resources
ctx = load_room_resources('<room_id>')
print('identity:', ctx['identity'])
print('policy:', ctx['policy'])
print('access:', ctx['access'])
"
```

Then send a message at the new transport and confirm it lands in the room (check `unified_log_2026` for new rows with `room_id=<your_room_id>`).

## Common pitfalls

- **Forgot a required file.** Room load fails at startup. The error names the missing file — fix and restart.
- **`authority_level` too high for the room's purpose.** A Slack channel with `99` opens up tools you didn't mean to expose. Default to `50`; bump only when needed.
- **`retention.write_kg: true` on a room that gets noisy / off-topic chat.** The KG pipeline ingests the noise. Default to `false` unless the room is curated.
- **Cross-room sharing without thinking.** `shared_chat_room_ids: ["master_room"]` exposes master_room's private chat to your new room. Almost never what you want — only use for system rooms that legitimately need master context.
- **Permissions list overrides allowlist resolution.** If you set `blocked_tools` and the room manager has its own default allowlist, blocked wins. Test the actual tool catalog the room sees by triggering a chat and checking the agent's prompt debug output.

## See also

- [03_ROOMS.md](../architecture/03_ROOMS.md) — the room contract
- `app/assistant/rooms/ROOM_CONTRACT.md` — the formal file spec
- [18_TRANSPORTS.md](../architecture/18_TRANSPORTS.md) — wiring rooms to transports
- [Add a manager](ADD_A_MANAGER.md) — if you need a new room manager too
