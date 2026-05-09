# Room Contract (Strict)

Each room directory under `app/assistant/rooms/<room_id>/` must contain
exactly one file:

```
<room_id>/
└── ROOM.md
```

The file is YAML frontmatter (machine-readable config) followed by a
markdown body (human-editable prose injected into agent prompts).

## Frontmatter

Three required top-level mappings:

```yaml
---
policy:
  policy_id: "room_policy::<room_id>::v1"
  manager_name: "room_manager"        # which manager handles this room
  surface: "ui"                       # ui | slack | telegram | sms | …
  default_visibility: "owner_only"
  default_context_id: "main"
  authority_level: 50                 # 1-100 (master_room is 99)
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
  allowed_global_resources: []
  allowed_entity_cards: []
  pinned_entities: []
  blocked_entities: []
  rag_scopes: ["chat", "memory"]
  shared_chat_room_ids: []
---
```

## Body

H1 sections route to the named blackboard keys agents read at prompt
time. Order doesn't matter; multiple sections targeting the same key
concatenate. Unknown headers are silently ignored.

| H1 section            | Blackboard key                       |
| --------------------- | ------------------------------------ |
| `# Identity`          | `room_identity`                      |
| `# Room context`      | `room_identity` (appended)           |
| `# Conversation`      | `room_conversation`                  |
| `# Safety`            | `room_safety`                        |
| `# Room facts`        | `room_facts` (optional)              |
| `# Participant facts` | `room_participant_facts` (optional)  |

`# Identity` content is required — every room must tell agents who
it is. Other sections are optional.

## Loading rules

- The loader (`room_resource_loader.load_room_context_for_manager`)
  reads `ROOM.md`, splits frontmatter into the three blocks, splits
  body by H1 headers, and returns the same flat dict consumers
  expected from the legacy multi-file shape — no caller changes.
- Missing or malformed `ROOM.md` raises loudly.
- Missing `# Identity` content raises loudly.
- Unknown frontmatter or body sections are ignored, not errors.

## Surface-native room ids

- Telegram: `telegram/<chat_id>` (created via `make_telegram_room_id`)
- Slack: `slack/<channel_id>` (created via `make_slack_room_id`)
- SMS: `sms/<sender_or_contact_id>`
- UI / per-feature rooms: bare names like `master_room`, `doc_editor`,
  `kg_dev_room`, `emi_code_room`, `task_create`, `dayflow_orchestrator`.

## Migration history

Pre-2026-05-09: each room had 7-9 separate JSON files (policy.json,
permissions.json, access.json, plus six `resource_*.json` wrappers
each carrying a single `content: string` field). That shape was
collapsed into a single `ROOM.md` per room as part of the
extensibility refactor. No backwards-compat fallback — adding a
new room means writing one ROOM.md.

See `skills/extending-emi-rooms/SKILL.md` for the field reference.
