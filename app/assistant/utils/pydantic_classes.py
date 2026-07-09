from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Dict, Any, Optional, List, Union, Literal
from datetime import datetime, timezone
import uuid

from app.assistant.pod_store.contracts import PodHeader


class ScopeBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScopeHistoryPolicy(ScopeBaseModel):
    """DECLARED, NOT ENFORCED (2026-07-07 scope audit): no runtime reads any
    field of this policy — history assembly is owned by RoomHistoryBuilder and
    per-agent context items. Declaring values here changes nothing. Kept so the
    dimension has a home when a consumer is wired; wire the consumer FIRST,
    then author values."""
    mode: Literal["none", "recent_only", "summary_plus_recent"] = "recent_only"
    source: Literal["scope_local", "unified_log"] = "unified_log"
    include_room_scoped: bool = False
    lookback_hours: Optional[int] = None
    max_messages: Optional[int] = None
    max_chars_per_message: Optional[int] = None


class ScopeResourcePolicy(ScopeBaseModel):
    allowed_global_resources: List[str] = Field(default_factory=list)
    allowed_room_resources: List[str] = Field(default_factory=list)
    denied_resources: List[str] = Field(default_factory=list)
    resource_groups: List[str] = Field(default_factory=list)


class ScopeToolRule(ScopeBaseModel):
    """Per-manager tool-surface rule that fires when the named manager runs.

    Keyed by manager name in ScopeToolPolicy.per_manager. The rule applies
    only if that manager actually runs somewhere in the call tree under
    this scope — entries for managers that never run are no-ops.

    - ``allow`` (Optional[List[str]]): when present (even empty), REPLACES
      the manager's native allowed_tools with this list. Use when you want
      to narrow the manager to a specific subset. When ``None``, the
      manager's natives apply unchanged.
    - ``block`` (List[str]): subtracted from the manager's effective tool
      surface (after ``allow`` narrowing). Surgical removal of specific
      leaves while keeping the rest of the manager's natives.
    """
    allow: Optional[List[str]] = None
    block: List[str] = Field(default_factory=list)


class ScopeToolPolicy(ScopeBaseModel):
    allowed_tools: List[str] = Field(default_factory=lambda: ["all"])
    blocked_tools: List[str] = Field(default_factory=list)
    requires_approval_tools: List[str] = Field(default_factory=list)
    allow_external_side_effects: bool = False
    # Per-manager rules. Lookup at manager startup time: when manager X
    # runs anywhere in this scope's call tree, scope.tools.per_manager[X]
    # narrows / trims X's effective surface. Unmentioned managers run with
    # their native allowed_tools unchanged. See ScopeToolRule.
    per_manager: Dict[str, ScopeToolRule] = Field(default_factory=dict)

    @field_validator("allowed_tools")
    @classmethod
    def validate_allowed_tools_semantics(cls, v: List[str]) -> List[str]:
        cleaned = [str(item).strip() for item in (v or []) if isinstance(item, str) and str(item).strip()]
        if "all" in cleaned and len(cleaned) > 1:
            raise ValueError("tools.allowed_tools cannot mix 'all' with specific tool names.")
        return cleaned


class ScopePodPolicy(ScopeBaseModel):
    """Which pod scopes this room can see/fetch.

    Pods are tagged with a ``scope_id`` (typically the room_id where they
    were minted). A room declares which pod scopes its agents can read,
    independent of authority. Sensitivity gating (min_authority) is a
    SEPARATE concern — this controls cross-room privacy, not body access.

    Special tokens:
      - ``"all"``: see all pods regardless of scope_id (owner surfaces)
      - ``"self"``: see only pods whose scope_id matches this room's room_id

    Mixing "all" with explicit room_ids is invalid (just use "all"). Mixing
    "self" with explicit room_ids is fine — that expands self with extras.
    Default (empty list) = ``["self"]`` behavior — the safest, most
    restrictive option.
    """
    allowed_scopes: List[str] = Field(default_factory=lambda: ["self"])

    @field_validator("allowed_scopes")
    @classmethod
    def validate_allowed_scopes_semantics(cls, v: List[str]) -> List[str]:
        cleaned = [str(item).strip() for item in (v or []) if isinstance(item, str) and str(item).strip()]
        if "all" in cleaned and len(cleaned) > 1:
            raise ValueError("pods.allowed_scopes cannot mix 'all' with specific room_ids.")
        return cleaned or ["self"]


class ScopeEntityPolicy(ScopeBaseModel):
    # ENFORCED: enabled + allowed_entity_cards bind at the injection point
    # (EntityInjector._narrow_entities_by_scope, 2026-07-08): enabled=false
    # -> no cards; a non-empty allowlist -> only those entities render.
    # Entity SEEDING reads the room_* context keys from room_resource_loader.
    # NOT ENFORCED: the two lookback knobs — no consumers.
    enabled: bool = True
    allowed_entity_cards: List[str] = Field(default_factory=list)
    pinned_entities: List[str] = Field(default_factory=list)
    entity_lookback_messages: Optional[int] = None
    entity_lookback_seconds: Optional[int] = None


class ScopeCardPolicy(ScopeBaseModel):
    """DECLARED, NOT ENFORCED (2026-07-07 scope audit): no runtime consumer.
    Wire the consumer FIRST, then author values."""
    enabled: bool = True
    allowed_cards: List[str] = Field(default_factory=list)
    max_cards_per_turn: Optional[int] = None
    max_total_chars: Optional[int] = None


class ScopeWritePolicy(ScopeBaseModel):
    # ENFORCED: write_unified_log + write_kg (projected to runtime data;
    # kg_investigator's finding processor gates on write_kg). NOT ENFORCED:
    # allow_fact_extraction + writable_state_keys — projected but never read
    # (2026-07-07 scope audit).
    write_unified_log: bool = True
    write_kg: bool = False
    allow_fact_extraction: bool = False
    writable_state_keys: List[str] = Field(default_factory=list)


class ScopeDeliveryPolicy(ScopeBaseModel):
    # ENFORCED: auto_send (room ingress / mode-exit / persistence / post_room
    # + chat_task_router read the projected knob). NOT ENFORCED:
    # allow_initiation + allowed_reply_types — projected but never read
    # (2026-07-07 scope audit).
    auto_send: bool = True
    allow_initiation: bool = False
    allowed_reply_types: List[str] = Field(default_factory=list)


class ScopeApprovalPolicy(ScopeBaseModel):
    authority_level: int = Field(default=0, ge=0, le=100)


class ScopeRetentionPolicy(ScopeBaseModel):
    """DECLARED, NOT ENFORCED (2026-07-07 scope audit): no runtime reads any
    field — persistence behavior lives in the room services and ROOM.md policy.
    Wire the consumer FIRST, then author values."""
    persist_chat: bool = True
    persist_tool_results: bool = True
    allow_context_summarization: bool = True
    redact_before_persist: bool = False


class ScopeExecutionPolicy(ScopeBaseModel):
    """DECLARED, NOT ENFORCED (2026-07-07 scope audit): no runtime reads any
    field — turn/tool/timeout limits live in the manager runtime today. Wire
    the consumer FIRST, then author values."""
    max_turns: Optional[int] = None
    max_tool_calls: Optional[int] = None
    timeout_seconds: Optional[int] = None
    allowed_models: List[str] = Field(default_factory=list)


class ScopeDelegationPolicy(ScopeBaseModel):
    """Reserved dimension — carries no fields and no runtime consumer
    (2026-07-07 scope audit)."""
    pass


class ScopeSkillsPolicy(ScopeBaseModel):
    """Scope-level skill injection — applies to every agent run inside the scope.

    Stamped by the scope builder (chat ingress, routine declaration, parent
    sub-manager) based on principal, mode, room, or any other scope-shaping
    signal. Propagates through nested agent calls because ScopeContext does.

    Composes with the existing per-agent / keyword / per-invocation paths;
    context_injector.resolve_skills() merges all four into one dict (with
    denied_skills as the final filter).
    """
    always_inject: List[str] = Field(default_factory=list)
    denied_skills: List[str] = Field(default_factory=list)


class ScopeContext(ScopeBaseModel):
    schema_version: Literal["scope_context_v1"] = "scope_context_v1"

    # Identity and tenancy
    scope_id: str
    owner_id: str
    actor_id: str
    surface: str
    room_id: Optional[str] = None
    room_context_id: Optional[str] = None
    visibility: Literal["owner_only", "room_shared", "global_shared"] = "owner_only"
    policy_id: Optional[str] = None

    # On-behalf-of dimension: whose principal does Emi execute under for this
    # scope? Default "user" = the primary human user (Jukka); Emi reads his
    # email, sends from his address, etc. "emi" = Emi acts on her own behalf
    # (her own Gmail, her own social accounts, signups for her own tooling).
    # Future principals are anticipated — Katy, Peter, an external client —
    # each with their own accounts / voice / skill bundle / memory scope /
    # authority gates. Hence string-typed; validation happens at runtime
    # against a principals registry rather than at the schema level, so a
    # new principal doesn't force a ScopeContext schema migration.
    #
    # Stamped at chat ingress by a keyword detector ("act as you", "as
    # yourself", "act as Katy", etc.) or explicitly by routines/pipelines
    # that declare on-behalf-of work. Tools that take from_account /
    # for_account consult this when no explicit arg is passed; explicit
    # arg always wins.
    acting_as: str = "user"

    # Transport reply-to: enough info for any layer downstream of ingress
    # to send a chat message back to the originating surface (UI socket,
    # slack channel + thread, sms number, telegram chat). Set by surface
    # inbound services; propagates through chained sub-managers because
    # ScopeContext propagates. ChatNarrator and other outbound publishers
    # read this to route. Shape mirrors what each surface produces today
    # (``{type, room_id, room_surface, ...surface-specific fields}``).
    reply_to: Optional[Dict[str, Any]] = None

    # Scope dimensions
    history: ScopeHistoryPolicy = Field(default_factory=ScopeHistoryPolicy)
    resources: ScopeResourcePolicy = Field(default_factory=ScopeResourcePolicy)
    tools: ScopeToolPolicy = Field(default_factory=ScopeToolPolicy)
    pods: ScopePodPolicy = Field(default_factory=ScopePodPolicy)
    entities: ScopeEntityPolicy = Field(default_factory=ScopeEntityPolicy)
    cards: ScopeCardPolicy = Field(default_factory=ScopeCardPolicy)
    writes: ScopeWritePolicy = Field(default_factory=ScopeWritePolicy)
    delivery: ScopeDeliveryPolicy = Field(default_factory=ScopeDeliveryPolicy)
    approval: ScopeApprovalPolicy = Field(default_factory=ScopeApprovalPolicy)
    retention: ScopeRetentionPolicy = Field(default_factory=ScopeRetentionPolicy)
    execution: ScopeExecutionPolicy = Field(default_factory=ScopeExecutionPolicy)
    delegation: ScopeDelegationPolicy = Field(default_factory=ScopeDelegationPolicy)
    skills: ScopeSkillsPolicy = Field(default_factory=ScopeSkillsPolicy)


# Define the Message class using Pydantic
class Message(BaseModel):
    data_type: Optional[str] = None  # e.g., 'init_agent', 'agent_msg', 'tool_result', 'agent_selection_request', 'agent_selection_response'
    # Additional classification flexibility:
    # This is intentionally a LIST so a message can carry multiple tags for routing/scoping.
    # Example: ["chat", "slash_command", "music"]
    sub_data_type: List[str] = Field(default_factory=list)
    sender: Optional[str] = None  # Name of the agent or 'User'
    receiver: Optional[str] = None
    content: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    task: Optional[str] = ""
    ask_user_id: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    scope_id: Optional[str] = None
    group_id: Optional[int] = None
    information: Optional[str] = ""
    request_id: Optional[str] = None
    role: Optional[str] = None
    notification: Optional[bool] = False
    is_chat: Optional[bool] = False
    agent_input: Optional[Union[str, Dict[str, Any]]] = None
    event_topic: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None  # For storing additional metadata like entity names
    test_mode: Optional[bool] = False
    memo_mode: Optional[bool] = False

    # Room-scoped messaging contract (optional, backward-compatible)
    room_id: Optional[str] = None
    room_surface: Optional[str] = None
    room_context_id: Optional[str] = None
    room_visibility: Optional[str] = None  # owner_only | room_shared
    room_policy_id: Optional[str] = None

    # Per-message room semantics
    room_message_direction: Optional[str] = None  # inbound | outbound
    room_initiated_by: Optional[str] = None  # user | agent | system
    room_delivery_mode: Optional[str] = None  # auto_send | approved | draft

    # Multi-speaker room identity
    room_speaker_id: Optional[str] = None
    room_speaker_name: Optional[str] = None
    room_speaker_role: Optional[str] = None  # owner | participant | assistant | system
    room_speaker_external_id: Optional[str] = None
    room_actor_id: Optional[str] = None

    # Transport-level identifiers
    transport_message_id: Optional[str] = None
    transport_from: Optional[str] = None
    transport_to: Optional[str] = None
    transport_channel_id: Optional[str] = None
    transport_thread_id: Optional[str] = None

    # Canonical scope contract for scoped entities.
    scope_context: Optional[ScopeContext] = None

    # Pod references hydrated from message text (populated by PodInjector).
    # Non-empty list means hydration has already run for this message.
    referenced_pods: List[PodHeader] = Field(default_factory=list)


class PlanStruct(Message):
    plan: str
    current_step: str
    action: str
    action_input: str
    status: int
    complete: bool

class ToolResult(BaseModel):
    result_type: Optional[str] = None
    content: str = ""
    data_list: Optional[List[Dict[str, Any]]] = Field(default_factory=list)
    data: Optional[Any] = None


class ToolMessage(Message):  # Assuming Message is similar to BaseModel
    tool_name: str  # Name of the tool being invoked
    tool_data: Optional[Dict[str, Any]] = None # Arguments passed to the tool
    tool_result: Optional[ToolResult] = None  # Result of the tool execution
    request_id: Optional[str] = None


class PipelineToolOp(BaseModel):
    name: Optional[str] = None
    arguments: Optional[Dict[str, Any]] = None
    action_input: Optional[Any] = None
    calling_agent: Optional[str] = None
    kind: Optional[str] = None  # tool | agent | control_node


class PipelineState(BaseModel):
    stage: Optional[str] = None
    pending_tool: Optional[PipelineToolOp] = None
    last_tool_result_ref: Optional[Dict[str, str]] = None
    last_tool_result_meta: Optional[Dict[str, Any]] = None
    resume_target: Optional[str] = None
    flags: Dict[str, Any] = Field(default_factory=dict)
    scratch: Dict[str, Any] = Field(default_factory=dict)


RESULT_TYPE_HANDLERS = {
    "fetch_email": "handle_fetch_email_result",
    "SearchResult": "handle_search_result",
    "SendEmailTool": "handle_send_email_result",
    "search1": "handle_search1_result",
    "scrape": "handle_scrape_result",
    "success": "handle_success_result",
    "error": "handle_error_result",
    "final_answer": "handle_final_answer",
    "calendar_events": "handle_get_calendar_event_result",
    "scheduler_events": "handle_fetch_scheduler_events_result",
    "weather": "handle_get_weather_result",
    "news": "handle_get_news_result",
    "todo_tasks": "handle_get_todo_tasks",
    "ask_user_response": "handle_ask_user_response",
    "tool_success": "handle_tool_success",
    "tool_failure": "handle_tool_failure"

}

RESULT_TYPE_HANDLERS_NEW = {
    "fetch_email": "handle_fetch_email_result",
    "search_result": "handle_search_result",
    "send_email": "handle_send_email_result",
    "search1": "handle_search1_result",
    "scrape": "handle_scrape_result",
    "success": "handle_success_result",
    "error": "handle_error_result",
    "final_answer": "handle_final_answer",
    "calendar_events": "handle_calendar_event_result",
    "scheduler_events": "handle_scheduler_events_result",
    "weather": "handle_weather_result",
    "news": "handle_news_result",
    "todo_tasks": "handle_todo_tasks_result",
    "ask_user_response": "handle_ask_user_response",
    "tool_success": "handle_tool_success",
    "tool_failure": "handle_tool_failure"
}



class EventMessage(Message):
    event_payload: Dict[str, Any]

class UserMessageData(BaseModel):
    feed: Optional[str] = None
    chat: Optional[str] = None
    widget_data: Optional[List[Dict[str,Any]]] = None
    sound: Optional[str] = None
    importance: Optional[int] = 2
    generic_type: Optional[str] = None
    tts: Optional[bool] = False
    tts_text: Optional[str] = None

class UserMessage(Message):
    user_message_data: UserMessageData


def require_scope_context(message: Message) -> ScopeContext:
    scope = getattr(message, "scope_context", None)
    if isinstance(scope, ScopeContext):
        return scope
    raise ValueError("Missing required scope_context on scoped message.")


# Not sure if these are needed putting these here for safe keeping

class EmailData(BaseModel):
    id: str  # Unique identifier for the email
    sender: Optional[str] = None
    receiver: Optional[List[str]] = None
    cc: Optional[List[str]] = None
    bcc: Optional[List[str]] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    datetime_received: Optional[str] = None  # ISO 8601 format
    has_attachment: Optional[bool] = None


class CalendarEventData(BaseModel):
    id: str  # Unique identifier for the event
    title: Optional[str] = None
    description: Optional[str] = None
    start_time: Optional[str] = None  # ISO 8601 format
    end_time: Optional[str] = None  # ISO 8601 format
    time_zone: Optional[str] = None
    location: Optional[str] = None
    attendees: Optional[List[str]] = None
    recurrence_rule: Optional[str] = None
    link: Optional[str] = None
    metadata: Optional[Dict] = None  # Extra information for flexibility

