---
name: extending-emi-rooms
description: How to add a new room to EmiOS. A room is a bounded conversation workspace with its own manager, tools, identity, and policy. Use when the task involves creating a new chat surface, channel, or scoped agent environment.
license: Apache-2.0
metadata:
  author: emi-team
  version: "1.0"
  auto_inject_when:
    task_keywords:
      - "new room"
      - "add room"
      - "create room"
      - "chat surface"
      - "extend emi rooms"
---

# Adding a new room

Rooms are scoped conversation channels in
`app/assistant/rooms/<room_id>/`. Each has its own identity,
permissions, manager binding, and conversation policy. Adding one
today touches a few files; the unification work to make this purely
declarative is on the roadmap.

## Files to create

```
app/assistant/rooms/<room_id>/
├── access.json          # required — permissions + entitled resources
├── identity.json        # required — display name, icon, persona
├── policy.json          # optional — conversation policy (gate, formatting)
└── room_facts.md        # optional — facts the room's chat_gate sees
```

Plus, depending on what you want the room to do:

- A **manager** under `app/assistant/multi_agents/<name>/` if the
  room needs domain-specific orchestration. See `extending-emi-managers`.
- A **chat_gate agent** under
  `app/assistant/agents/<room_id>/chat_gate/` if the room needs its
  own conversation gate. See `extending-emi-agents`.
- A **route** in `app/routes/` if the room needs its own URL surface
  (e.g. `/my-room` page).

## access.json

```json
{
  "allowed_global_resources": [
    "resource_user_data",
    "resource_assistant_data"
  ],
  "allowed_entity_cards": ["all"],
  "pinned_entities": [],
  "rag_scopes": ["chat", "memory"],
  "shared_chat_room_ids": [],
  "chat_ingestion_entitled_rooms": []
}
```

Resources NOT listed are invisible to agents in this room. Same for
entity cards. The `rag_scopes` controls what the chat-RAG path can
recall.

## identity.json

```json
{
  "display_name": "My Room",
  "icon": "fa-comments",
  "authority_level": 50,
  "persona": "Brief description of the room's character / purpose."
}
```

`authority_level` is 1-100. Master room is 99. A room with low
authority can't override decisions made in higher-authority rooms.

## policy.json (optional)

```json
{
  "chat_gate_engagement": "always",
  "default_response_style": "concise",
  "approval_required_tools": []
}
```

## Wiring the manager

If the room uses an existing manager (e.g. `emi_team_manager`), no
extra wiring is needed — `master_room::switchboard` routes by
intent. If the room needs its own manager, see
`extending-emi-managers`.

For the room to OWN a manager binding, your room's
`identity.json` can declare:

```json
"manager": "my_room_manager"
```

## Conversation surface

If the room is reachable through a non-web transport (Slack,
Telegram, SMS), the transport adapter routes inbound messages to
the room by `room_id`. See:

- Slack: `app/routes/slack_events.py`
- Telegram: `app/routes/telegram_webhook.py`
- SMS: `app/routes/sms_inbound.py`

The room_id in the inbound message determines which room handles it.

## After dropping the files

1. Restart Flask.
2. Verify with the `Room Session Manager` log line at startup.
3. Test by sending a message into the room (via web UI / Slack /
   etc.) and watching the chat_gate fire.

## Canonical examples

- Master conversation room:
  `app/assistant/rooms/master_room/access.json`
- Doc-editing room with a mode router:
  `app/assistant/rooms/doc_editor/`
- Code-CLI bridge room:
  `app/assistant/rooms/emi_code_room/`
- Inbound SMS-mapped room:
  `app/assistant/rooms/katy/` (one-contact room)
- Dayflow's autonomous room:
  `app/assistant/rooms/dayflow_orchestrator/`

## Notes

- Rooms are NOT auto-discovered today — adding one requires editing
  the rooms registry. Until that lands, copy a canonical room and
  modify; the registry pulls from filesystem on startup.
- A room is the natural home for room-specific facts. Put them in
  `room_facts.md`; they're injected into the chat_gate when the
  room is active.
- Don't put user-personal facts in the room (those go in resources).
  Room facts are about the SHAPE of the conversation in this room
  ("this room is just for code patches", etc.).
- See also: `extending-emi-managers` for the agent-team that powers
  the room.
