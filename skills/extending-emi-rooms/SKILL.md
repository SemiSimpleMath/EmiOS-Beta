---
name: extending-emi-rooms
description: How to add a new room to EmiOS. A room is a bounded conversation workspace with its own manager, tools, identity, and policy — all declared in a single ROOM.md file (frontmatter + markdown body). Use when the task involves creating a new chat surface, channel, or scoped agent environment.
license: Apache-2.0
metadata:
  author: emi-team
  version: "2.0"
  auto_inject_when:
    task_keywords:
      - "new room"
      - "add room"
      - "create room"
      - "chat surface"
      - "extend emi rooms"
---

# Adding a new room

Every room is **one file**:

```
app/assistant/rooms/<room_id>/
└── ROOM.md
```

Frontmatter holds the structured config (policy, permissions, access).
Body holds the prose injected into agent prompts (identity, conversation
style, safety rules, facts) — sectioned by H1 headers.

## ROOM.md template

```markdown
---
policy:
  policy_id: "room_policy::<room_id>::v1"
  manager_name: "room_manager"        # which manager handles this room
  surface: "ui"                       # ui | slack | telegram | sms
  default_visibility: "owner_only"
  default_context_id: "main"
  authority_level: 50                 # 1-100; master_room is 99
  history:
    scope: "time_bounded"
    max_hours: 24
  retention:
    write_unified_log: true
    write_kg: true
    allow_fact_extraction: true
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
    - resource_assistant_data
  allowed_entity_cards: ["all"]
  pinned_entities: []
  blocked_entities: []
  rag_scopes: ["chat", "memory"]
  shared_chat_room_ids: []
---

# Identity

You are Emi in this room. ... (REQUIRED — at least the Identity section)

# Conversation

How to chat in this room: tone, length, follow-up policy, etc.

# Safety

Safety / privacy / prompt-injection rules.

# Room facts

Things this room assumes about itself.

# Participant facts

Who's in this room, relationship to the user.
```

## H1 → blackboard key mapping

Body sections route to the named keys agents read at prompt time:

| H1 header             | Blackboard key                       | Required |
|-----------------------|--------------------------------------|----------|
| `# Identity`          | `room_identity`                      | yes      |
| `# Room context`      | `room_identity` (appended)           | optional |
| `# Conversation`      | `room_conversation`                  | optional |
| `# Safety`            | `room_safety`                        | optional |
| `# Room facts`        | `room_facts`                         | optional |
| `# Participant facts` | `room_participant_facts`             | optional |

Unknown headers are silently ignored — useful for working notes
inside ROOM.md that aren't meant to ship to agents.

## After writing the file

1. Restart Flask. Room loaders pick up the new file at startup.
2. The room is callable via `room_id` matching the directory name
   (or `<surface>/<id>` for surface-native rooms).
3. Test by sending a message into the room (UI, Slack, Telegram, etc.).

## Surface-native room ids

For inbound transports, room ids are derived deterministically:

- Telegram: `telegram/<chat_id>` via `make_telegram_room_id`
- Slack:    `slack/<channel_id>`  via `make_slack_room_id`
- SMS:      `sms/<sender_or_contact_id>`

Room directories nest accordingly: `app/assistant/rooms/telegram/<chat_id>/ROOM.md`.

## Wiring the manager

If `manager_name` references an existing manager (`emi_team_manager`,
`personal_admin_manager`, etc.), you're done. If you need a new
domain-specific manager, see `extending-emi-managers`.

## Canonical examples

- Master conversation room (authority 99, full surface):
  `app/assistant/rooms/master_room/ROOM.md`
- Doc-editing room with mode-router:
  `app/assistant/rooms/doc_editor/ROOM.md`
- Code-CLI bridge:
  `app/assistant/rooms/emi_code_room/ROOM.md`
- One-contact room (Slack/Telegram personality):
  `app/assistant/rooms/katy/ROOM.md`
- Autonomous orchestrator room (no human user):
  `app/assistant/rooms/dayflow_orchestrator/ROOM.md`
- Surface-native (Telegram chat):
  `app/assistant/rooms/telegram/7295968126/ROOM.md`

## Notes

- Authority levels are meaningful: a room can't override decisions
  made in higher-authority rooms. Master room is 99. Be conservative
  with new high-authority rooms.
- `allowed_global_resources: ["all"]` is a wildcard for trusted
  rooms. Most rooms list specific resources for principled access.
- `blocked_entities` filters entity cards from agent context — useful
  for "this room shouldn't talk about X" patterns.
- The Identity section is the minimum; you can ship a usable room
  with just that. Add sections as the room develops a clearer
  personality and policy.
- See `app/assistant/rooms/ROOM_CONTRACT.md` for the strict spec.
