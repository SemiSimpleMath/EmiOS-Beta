---
policy:
  policy_id: room_policy::dayflow_orchestrator::v1
  manager_name: dayflow_orchestrator_manager
  surface: system
  default_visibility: owner_only
  default_context_id: orchestrator
  authority_level: 95
  history:
    exclude_data_types:
    - agent_result
    - agent_request
  retention:
    write_unified_log: false
    write_kg: false
    allow_fact_extraction: false
  delivery:
    auto_send: false
    allow_initiation: true
  privacy:
    owner_only_memory_visible: true
    room_facts_only: false
  participant_identity:
    display_name: Dayflow Orchestrator
    aliases:
    - dayflow_orchestrator
    - orchestrator
permissions:
  tool_classes:
    informational: true
    transformational: true
    external_action: true
    sensitive: true
  # dayflow is an owner-internal system surface — it operates autonomously on
  # behalf of the user across all surfaces (slack, master_room, telegram, sms,
  # routine outputs). It needs cross-room pod visibility to correlate events
  # (e.g., notice an unanswered slack message + a calendar conflict + a
  # weekly_meal_planner pod). Sensitivity-band gating (min_authority) still
  # applies to per-pod content like SSN regardless.
  pod_scopes: [all]
  allow_images: false
access:
  allowed_global_resources:
  - resource_user_data
  - resource_expected_calendar
  - resource_daily_context_generator_output
  - resource_daily_context
  - resource_user_health_status
  - resource_user_beliefs
  - resource_health_inference_output
  - resource_sleep_output
  - resource_sleep_segments_output
  - resource_afk_statistics_output
  - resource_tracked_activities_output
  - resource_dayflow_status
  - resource_dayflow_orchestrator_input_messages
  - resource_dayflow_routine
  - resource_dayflow_routine_latest
  - resource_assistant_data
  - resource_user_email
  - resource_weather
  - resource_current_location
  - resource_orchestrator_user_prefs
  allowed_entity_cards:
  - all
  pinned_entities: []
  rag_scopes:
  - chat
  - memory
  shared_chat_room_ids: []
  chat_ingestion_entitled_rooms:
  - master_room
  ingestion_pod_kinds:
  - kind: image
    source_kind: ring_doorbell_significant
  - kind: image
    source_kind: ring_bedroom_notable
---

# Room context

- This is an internal orchestration room for dayflow planning and execution coordination.
- Operate as a system planner and dispatcher for dayflow intents.
- Coordinate action-oriented follow-ups without duplicating already-handled items.
- Preserve strict room and policy boundaries.
- This room is not a direct end-user chat surface.

# Conversation

- Keep responses operational and concise.
- Prefer explicit action-oriented phrasing for downstream delegates.
- Avoid conversational fluff unless a user-facing response is explicitly required.
- If no output is required, use no-op semantics instead of filler text.

# Safety

- Do not bypass approval requirements for sensitive external actions.
- Do not fabricate tool outcomes, execution status, or user approvals.
- Prefer deterministic delegation for structured execution tasks.
- Respect room-scoped visibility and policy constraints.
- Fail loudly on invalid or missing required context.

# Room facts

- This room coordinates with master room context through explicit scope and policy.
- It prefers deduplicated actions backed by a persistent action ledger.
- It can delegate to specialist managers and tools for execution.
- Primary owner: Jukka. This room may consume owner-relevant context but should not leak cross-room data.
