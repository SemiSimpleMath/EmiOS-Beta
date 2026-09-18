---
name: extending-emi-rooms
description: How to add a new room to EmiOS. A room is a bounded conversation workspace with its own manager, tools, identity, and policy — declared in ROOM.md (frontmatter + markdown body) plus scope.yaml (the permission scope). Use when the task involves creating a new chat surface, channel, or scoped agent environment.
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

Every room is **two files**:

```
app/assistant/rooms/<room_id>/
├── ROOM.md        # room behaviour + the prose agents read
└── scope.yaml     # the permission scope
```

`ROOM.md` frontmatter holds the structured room config (policy, permissions,
access); its body holds the prose injected into agent prompts (identity,
conversation style, safety rules, facts), sectioned by H1 headers.

`scope.yaml` is **permission only** — what callers in this room may DO and SEE.
It arrived with the unified-scope refactor, and `room_bootstrap` copies **both**
files into every new room (`_TEMPLATE_FILES = ("ROOM.md", "scope.yaml")`), raising
if either template is missing. All 11 rooms in the repo have both; so do both
surface templates. A room with only `ROOM.md` is not a complete room.

Its top-level blocks:

```yaml
approval:
  authority_level: 50
tools:
  allowed_tools: [all]              # or an explicit list
  allow_external_side_effects: true
  per_manager:                      # narrows ONE manager's direct pool
    emi_team_manager:
      allow: [personal_admin_manager, web_manager, ask_kg]
pods:
  allowed_scopes: [all]
resources:
  allowed_global_resources: [all]
  resource_groups: [chat, memory]
entities:
  enabled: true
  allowed_entity_cards: [all]
writes:
  write_unified_log: true
  write_kg: true
  allow_fact_extraction: true
delivery:
  auto_send: true
  allow_initiation: true
```

Identity (`scope_id`, `owner_id`, `actor_id`, `surface`, `reply_to`) is stamped
per request at load time and is **never authored** in `scope.yaml`.

> Permissive settings must be declared explicitly. `write_kg`,
> `allow_fact_extraction`, `pods: [all]` and `entities: [all]` are more permissive
> than the model defaults — omitting them silently downgrades the room to the
> fail-closed floor. In particular, a `scope.yaml` with **no `tools.allowed_tools`
> gets `[]` — no tools at all**, not "all" (`loader._apply_fail_closed_floor`).
> Read `docs/architecture/SCOPE.md` before authoring one.

**How the two files combine.** `room_scope_builder` builds the room's scope from
ROOM.md's `policy`/`permissions`/`access`, then — if `scope.yaml` exists — replaces
these blocks wholesale with the file's: `tools`, `pods`, `resources`, `entities`,
`cards`, `writes`, `approval`, plus `delivery.auto_send` and
`delivery.allow_initiation`. Identity, room behaviour (history / retention /
execution), `delivery.allowed_reply_types` and skills stay builder-computed. A room
without `scope.yaml` keeps ROOM.md's permissions, so the overlay is a no-op — but
every room in the repo ships one.

Identity fields (`scope_id`, `owner_id`, `actor_id`, `room_id`, `room_context_id`,
`reply_to`, `acting_as`, `policy_id`) may **not** be authored in `scope.yaml`; they
are stamped per request, and any found in the file are dropped with a warning.
Unknown keys are rejected outright (`extra="forbid"`).

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

> **Unknown headers are NOT ignored — do not put private notes in ROOM.md.**
> An unrecognized H1 is folded into the **most recently recognized** section, so a
> `# Working notes` heading placed after `# Identity` is appended to
> `room_identity` and ships straight into the room's prompts. This is deliberate:
> silently dropping unknown sections had hidden whole authored personality and
> engagement-policy blocks from room prompts, so the loader now keeps them.
>
> An unknown header appearing *before* any recognized section has no anchor — it
> is dropped with a warning. Either way, ROOM.md is not a scratchpad.

## After writing the file

1. Restart Flask. Room loaders read the files on demand, per room id.
2. The room is callable via `room_id` matching the directory name
   (or `<surface>/<id>` for surface-native rooms).
3. Test by sending a message into the room (UI, Slack, Telegram, etc.).

A room id containing `::` resolves its config directory from the **prefix**, via
`_ROOM_CONFIG_PREFIX_MAP` in `room_resource_loader` (`task_spec::…` reads
`task_create/`). Room ids are validated against `^[A-Za-z0-9._:/-]+$` and no
segment may be empty, `.` or `..`.

Failure modes worth knowing: a missing or malformed `ROOM.md` **raises**, and so
does a body with no identity content — `# Identity` (or `# Room context`) is the
one section a room cannot omit. A missing `policy:` block is tolerated by the
prompt-context loader but **raises** for callers that use `load_room_policy`
(orchestrators and schedulers building a scope outside the session path), so
author it. Unset fields fall back: `manager_name` → `room_manager`, `surface` →
`unknown`, `default_visibility` → `room_shared`.

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
- KG development room: `app/assistant/rooms/kg_dev_room/ROOM.md`
- Mode-scoped task room: `app/assistant/rooms/task_create/ROOM.md`
- Autonomous orchestrator room (no human user, authority 95):
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
