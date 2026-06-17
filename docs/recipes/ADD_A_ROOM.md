# Recipe: Add a new room

A room is a scoped conversation channel — separate identity, separate permissions, separate authority level. You add a new room when:

- You're integrating a new transport (a new Slack workspace, a new Telegram bot).
- You want a separate conversational context (a "the user's partner" room for a specific contact with restricted access).
- You're building a system-only channel (like `dayflow_orchestrator`).

A room is now **one file**: `app/assistant/rooms/<room_id>/ROOM.md` — YAML frontmatter (machine config) followed by a markdown body (prose injected into agent prompts). The old shape (7-9 separate `policy.json` / `permissions.json` / `access.json` / `resource_*.json` files) was collapsed into this single file; there is no backwards-compat fallback.

> **Authoritative sources:** the strict contract at `app/assistant/rooms/ROOM_CONTRACT.md` and the field reference at `skills/extending-emi-rooms/SKILL.md`. Read [03_ROOMS.md](../architecture/03_ROOMS.md) for the framework. This recipe is the orientation.

For canonical examples: `app/assistant/rooms/master_room/ROOM.md` (full-authority UI, authority 99), `app/assistant/rooms/katy/ROOM.md` (one-contact room), `app/assistant/rooms/dayflow_orchestrator/ROOM.md` (autonomous, no human user).

## The ROOM.md file

Frontmatter has three required top-level mappings — `policy`, `permissions`, `access`. The body is sectioned by H1 headers, each routing to a blackboard key agents read at prompt time.

```markdown
---
policy:
  policy_id: "room_policy::<room_id>::v1"
  manager_name: "room_manager"        # which manager handles inbound
  surface: "ui"                       # ui | slack | telegram | sms | system
  default_visibility: "owner_only"    # owner_only | room_shared
  default_context_id: "main"
  authority_level: 50                 # 1-100; master_room is 99
  history:
    scope: "time_bounded"
    max_hours: 24
  retention:
    write_unified_log: true
    write_kg: false
    allow_fact_extraction: false
  delivery:
    auto_send: true
    allow_initiation: false
  privacy:
    owner_only_memory_visible: true
    room_facts_only: false
  participant_identity:
    display_name: "User"
    aliases: []

permissions:
  tool_classes:
    informational: true
    transformational: true
    external_action: true
    sensitive: false
  allow_images: true

access:
  allowed_global_resources:
    - resource_user_data
  allowed_entity_cards: []
  pinned_entities: []
  blocked_entities: []
  rag_scopes: ["chat", "memory"]
  shared_chat_room_ids: []
---

# Identity

You are the assistant in this room. ...  (REQUIRED — at minimum the Identity section)

# Conversation

How to chat here: tone, length, follow-up policy.

# Safety

Safety / privacy / prompt-injection rules.
```

A room with just `# Identity` is valid; add the other sections as the room develops a personality. **Missing or malformed `ROOM.md`, or empty identity content, raises loudly** — the contract is intentionally strict.

## Frontmatter blocks

### `policy` — the load-bearing block

- `manager_name` — which Manager processes inbound (`master_room_manager`, `room_manager`, ...). Defaults to `room_manager` if omitted.
- `surface` — `"ui"`, `"slack"`, `"telegram"`, `"sms"`, or `"system"`.
- `default_visibility` — `"owner_only"` (private) vs `"room_shared"`.
- `authority_level` — the room's ceiling for tool authorization. `99` = master, `50` = restricted, `95` for system rooms like dayflow. Be conservative: a room can't override decisions made in a higher-authority room.
- `retention.write_unified_log` / `write_kg` / `allow_fact_extraction` — whether messages persist, get fed to the KG ingest pipeline, and have facts pulled. Most rooms are `write_kg: false`; only master and similarly-trusted rooms are `true`.
- `delivery.allow_initiation` — whether the room may start conversations (proactive outreach). `participant_identity.display_name` names the human in the room (used as `room_contact_name`).

### `permissions` — tool exposure

`tool_classes` is the coarse gate (`informational` / `transformational` / `external_action` / `sensitive` booleans). `allow_images` toggles image input. Per-tool allowlist/blocklist and the authority/approval gates are applied by the scope layer (see [15_EMI_TEAM_AND_SCOPE.md](../architecture/15_EMI_TEAM_AND_SCOPE.md) and the room's `scope.yaml`), not hand-listed in this block.

### `access` — resource / card / cross-room visibility

- `allowed_global_resources` — which resource files the room can see. `["all"]` is a wildcard (master_room); most rooms list specific resources.
- `allowed_entity_cards` — which cards inject into prompts. `["all"]` for full access.
- `pinned_entities` / `blocked_entities` — force-in / filter-out entity cards (`blocked_entities` is the "this room shouldn't talk about X" lever).
- `rag_scopes` — which RAG corpora are searchable (`"chat"`, `"memory"`, `"doc"`).
- `shared_chat_room_ids` — cross-room visibility. If room A lists B here, B's chat appears in A's history. Almost never what you want for a new room — only for system rooms that legitimately need another room's context.

> `master_room`'s frontmatter also carries a few specials worth copying only when you mean them: `permissions.pod_scopes: [all]` (see pods from every room — other rooms default to their own), `policy.chat_compaction`, and `policy.mode_manager_overrides`.

## The body: H1 sections → blackboard keys

Each H1 header routes to a blackboard key the prompt context reads (order doesn't matter; multiple sections targeting the same key concatenate):

| H1 section            | Blackboard key                       | Required |
| --------------------- | ------------------------------------ | -------- |
| `# Identity`          | `room_identity`                      | yes      |
| `# Room context`      | `room_identity` (appended)           | optional |
| `# Conversation`      | `room_conversation`                  | optional |
| `# Safety`            | `room_safety`                        | optional |
| `# Room facts`        | `room_facts`                         | optional |
| `# Participant facts` | `room_participant_facts`             | optional |

The H1 line itself is preserved inside the injected text (it's part of the visible prompt structure). An **unknown** header is *not* dropped — it folds into the most-recently-recognized section (so a `# Your personality` under `# Identity` lands in `room_identity`). An unknown header that appears *before* any recognized section has no anchor and is skipped with a warning — so always lead with `# Identity`.

## Surface-native room ids

For inbound transports, room ids are derived deterministically and the directory nests accordingly:

- Telegram: `telegram/<chat_id>` (via `make_telegram_room_id`) → `app/assistant/rooms/telegram/<chat_id>/ROOM.md`
- Slack: `slack/<channel_id>` (via `make_slack_room_id`)
- SMS: `sms/<sender_or_contact_id>`
- UI / per-feature rooms: bare names like `master_room`, `doc_editor`, `dayflow_orchestrator`.

(Room ids using a `<prefix>::<rest>` form route to a shared config dir via the prefix map in `room_resource_loader.resolve_room_config_dir`.)

## Wire transport routing

Rooms are per-transport, but the wiring lives outside the room directory:

- **UI rooms** — registered in the WebSocket handshake; every UI client maps to a room id at connection time.
- **SMS rooms** — phone-number-to-room mapping in the Twilio inbound handler.
- **Slack rooms** — channel-to-room mapping (`configs/slack_room.json`).
- **Telegram rooms** — chat-id-to-room mapping in the Telegram webhook handler.

If `manager_name` points at an existing manager you're done; for a new domain-specific manager see [Add a manager](ADD_A_MANAGER.md) (and `skills/extending-emi-managers`).

## Test it

Restart Flask (loaders pick up the new file at startup), then load the context:

```bash
.venv\Scripts\python.exe -c "
import app.assistant.tests.test_setup
from app.assistant.rooms.room_resource_loader import load_room_context_for_manager
ctx = load_room_context_for_manager('<room_id>')
print('manager:', ctx['room_manager_name'])
print('identity:', ctx['room_identity'][:200])
print('policy authority:', ctx['room_policy'].get('authority_level'))
print('access:', ctx['room_access'])
"
```

Then send a message at the new transport and confirm it lands in the room (check `unified_log_2026` for rows with `room_id=<your_room_id>`).

## Common pitfalls

- **Missing or empty `# Identity`.** Room load raises at startup — every room must tell agents who it is. The error names the room.
- **Unknown header placed before `# Identity`.** It's dropped with a warning (no recognized section to anchor to). Lead with `# Identity`.
- **`authority_level` too high for the room's purpose.** A Slack channel at `99` opens tools you didn't mean to expose. Default to `50`; bump only when needed.
- **`retention.write_kg: true` on a room that gets noisy / off-topic chat.** The KG pipeline ingests the noise. Default `false` unless the room is curated.
- **Cross-room sharing without thinking.** `shared_chat_room_ids: ["master_room"]` exposes master_room's private chat to your new room. Almost never what you want.
- **Copying master_room wholesale.** Its `allowed_*: ["all"]`, `pod_scopes: [all]`, and authority 99 are deliberate owner-room privileges. Strip them down for a restricted room.

## See also

- [03_ROOMS.md](../architecture/03_ROOMS.md) — the room framework
- `app/assistant/rooms/ROOM_CONTRACT.md` — the strict file spec
- `skills/extending-emi-rooms/SKILL.md` — field reference + examples
- [Add a manager](ADD_A_MANAGER.md) — if you need a new room manager too
