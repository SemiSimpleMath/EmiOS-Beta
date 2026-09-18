# Room Contract (Strict)

Each room directory under `app/assistant/rooms/<room_id>/` contains two
files:

```
<room_id>/
├── ROOM.md        # room behaviour + prose (this document)
└── scope.yaml     # the permission scope (unified-scope refactor)
```

`ROOM.md` is YAML frontmatter (machine-readable config) followed by a
markdown body (human-editable prose injected into agent prompts).

`scope.yaml` is PERMISSION ONLY — what callers in this room may do and
see (`approval.authority_level`, `tools` incl. `per_manager` narrowing,
`pods`, `resources`, `entities`, `writes`, `delivery`). Room/session
behaviour stays here in ROOM.md; identity (`scope_id`, `owner_id`,
`actor_id`, `surface`, `reply_to`) is stamped per request at load time and
is never authored. `room_bootstrap._TEMPLATE_FILES` provisions both files
for every new room and raises if either template is absent; every room in
the repo has both. See `docs/architecture/SCOPE.md` for scope semantics.

### How the two files combine (verified in `room_scope_builder`)

Several concepts appear in BOTH files — authority level, write flags, delivery,
allowed resources and entity cards. They are not rivals; they are an **overlay**:

1. `room_scope_builder` builds the room's `ScopeContext` from **ROOM.md**'s
   `policy` / `permissions` / `access` blocks.
2. If the room has a `scope.yaml`, `_overlay_scope_yaml_permission` then
   **replaces these blocks wholesale** with the file's:
   `tools`, `pods`, `resources`, `entities`, `cards`, `writes`, `approval` —
   plus `delivery.auto_send` and `delivery.allow_initiation` field-by-field.
3. Everything else stays builder-computed: identity, room behaviour
   (history / retention / execution), `delivery.allowed_reply_types`, and skills.
4. A room with **no** `scope.yaml` keeps the ROOM.md-derived permissions — the
   overlay is a no-op. Every room in the repo has one today, so in practice
   `scope.yaml` governs permission and ROOM.md governs behaviour and prose.

Separately, `load_room_context_for_manager` returns ROOM.md's three blocks as
`room_policy` / `room_permissions` / `room_access` for prompt context.

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
concatenate. Order DOES matter for unrecognized headers — see below.

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
- Unknown FRONTMATTER keys are ignored, not errors.
- Unknown BODY sections are **not** ignored: an unrecognized H1 is folded
  into the most recently recognized section, so prose under a
  `# Working notes` heading placed after `# Identity` is appended to
  `room_identity` and reaches agent prompts. Dropping them silently had
  hidden whole authored personality / engagement-policy blocks, so the
  loader keeps them instead. An unknown header before any recognized
  section has no anchor and is dropped with a warning. ROOM.md is not a
  scratchpad.
- A `policy:` / `permissions:` / `access:` block that is present but not a
  mapping raises. `load_room_context_for_manager` tolerates a block being
  absent, but `load_room_policy` / `load_room_access` — used by
  orchestrators and schedulers building a scope outside the session path —
  raise when theirs is missing. Author all three.
- A room id containing `::` resolves its config directory from the prefix
  via `_ROOM_CONFIG_PREFIX_MAP` (`task_spec::…` reads `task_create/`).
- Defaults when unset: `manager_name` → `room_manager`, `policy_id` →
  `room_policy::<room_id>`, `surface` → `unknown`, `default_visibility` →
  `room_shared`, contact name → titleized last path segment, else `User`.

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
