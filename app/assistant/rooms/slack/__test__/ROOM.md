---
policy:
  policy_id: room_policy::slack/__test__::v1
  surface: slack
  default_visibility: room_shared
  default_context_id: main
  authority_level: 30
  retention:
    write_unified_log: true
    write_kg: false
    allow_fact_extraction: false
  delivery:
    auto_send: true
    allow_initiation: true
  privacy:
    owner_only_memory_visible: false
    room_facts_only: false
  participant_identity:
    display_name: TestFriend
permissions:
  tool_classes:
    informational: true
    transformational: true
    external_action: false
    sensitive: false
  allowed_tools: null
  blocked_tools: []
  per_manager:
    emi_team_manager:
      allow:
        - web_manager
        - pod_search
        - pod_fetch
        - ask_user
        - find_tool
        - install_tool
        - read_skill
        - discover_skills
        - read_tool_result
    web_manager:
      block:
        - http_request
        - oauth_token_refresh
  allow_images: true
access:
  allowed_global_resources:
  - resource_user_data
  - resource_assistant_data
  - resource_chat_guidelines
  - resource_assistant_personality
  - resource_weather
  allowed_entity_cards:
  - all
  pinned_entities: []
  rag_scopes:
  - chat
  - memory
---

# Identity

- You are Emi, an assistant helping the primary user.
- This room is a synthetic Slack channel used by the room test harness.
- Speak as Emi. The other participant is named "TestFriend".

# Room context

- Surface: Slack.
- Room id: slack/__test__.
- Channel id: __test__.
- This room is a fixture, not a real Slack channel.

# Conversation

- Default action is silence. Respond only when addressed.
- Keep replies short.

# Safety

- Do not invent tool results.
- Refuse unsafe requests.
