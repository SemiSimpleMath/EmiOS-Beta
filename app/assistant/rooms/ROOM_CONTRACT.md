# Room Contract (Strict)

Each room directory under `app/assistant/rooms/<room_id>/` must include:

- `identity.md`
  - Identity and relationship to primary user.
  - Who Emi is in this room, purpose of this room, and how it differs from primary-user chat.
  - Personality/backstory context relevant to this room.

- `conversation.md`
  - Conversation mechanics and style.
  - Keep momentum, short text-message style, one follow-up max, avoid monologues.
  - Emoji policy or tone mechanics if needed.

- `safety.md`
  - Safety/guardrail rules for this room.
  - Privacy constraints, prompt-injection handling, sensitive topic constraints.
  - Code-level/behavioral constraints visible to planner.

- `room_facts.md`
  - Key facts about this room.
  - High-level room assumptions and operational facts.

- `participant_facts.md`
  - Key facts about participants in this room.
  - Relationship facts and participant-specific constraints.

- `permissions.json`
  - Allowed/blocked tools and tool-class constraints.
  - Media permissions such as `allow_images` (boolean).

- `policy.json`
  - Delivery semantics (`auto_send`, `allow_initiation`).
  - Approval categories.
  - Retention rules (`unified_log`, KG writes, fact extraction).
  - Optional `manager_name` override (defaults to `room_manager`).

- `access.json`
  - Visibility gate:
    - `allowed_entity_cards`
    - `allowed_global_resources`
    - `rag_scopes`
    - optional pinned entities

Notes:
- This contract is strict: no legacy file fallback.
- Missing required files should fail room load.
- Preferred room-id layout is surface-native and hierarchical:
  - `telegram/<chat_id>`
  - `slack/<channel_id>`
  - `sms/<sender_or_contact_id>`

