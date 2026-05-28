from __future__ import annotations

import os
import uuid
from typing import Any, Dict

from app.assistant.rooms.room_resource_loader import load_room_context_for_manager
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import (
    Message,
    ScopeApprovalPolicy,
    ScopeContext,
    ScopeResourcePolicy,
    ScopeToolPolicy,
)
from app.assistant.utils.surfaces import DEFAULT_AUTHORITY_BY_SURFACE

logger = get_logger(__name__)

_SCOPE_HISTORY_LOOKBACK_HOURS: int = 48
_SCOPE_HISTORY_MAX_MESSAGES: int = 60


def _as_non_empty_str(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _is_all_marker(values: list[str]) -> bool:
    return len(values) == 1 and values[0] == "all"


def _validate_allowed_tools_list(*, values: list[str], field_name: str) -> list[str]:
    cleaned = [v for v in values if isinstance(v, str) and v.strip()]
    if "all" in cleaned and len(cleaned) > 1:
        raise ValueError(f"{field_name} cannot mix 'all' with specific tool names.")
    return cleaned


def _intersect_allowed_tools(parent: list[str], child: list[str]) -> list[str]:
    parent_clean = _validate_allowed_tools_list(values=parent, field_name="parent.allowed_tools")
    child_clean = _validate_allowed_tools_list(values=child, field_name="child.allowed_tools")
    if _is_all_marker(child_clean):
        return parent_clean
    if _is_all_marker(parent_clean):
        return child_clean
    want = set(child_clean)
    return [x for x in parent_clean if x in want]


def _intersect(left: list[str], right: list[str]) -> list[str]:
    want = set(right)
    return [x for x in left if x in want]


def _resolve_allowed_tools_from_data(data: dict[str, Any], key: str = "task_allowed_tools") -> list[str]:
    # Unspecified/null inherits parent scope.
    if key not in data or data.get(key) is None:
        return ["all"]
    raw = data.get(key)
    if not isinstance(raw, list):
        raise ValueError(f"{key} must be a list when provided, got {type(raw).__name__}.")
    # Explicit [] means no rights.
    return _validate_allowed_tools_list(values=_as_str_list(raw), field_name=key)


def _coerce_authority_level(value: Any, *, field_name: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer between 0 and 100.")
    try:
        level = int(value)
    except Exception:
        raise ValueError(f"{field_name} must be an integer between 0 and 100.")
    if level < 0 or level > 100:
        raise ValueError(f"{field_name} must be between 0 and 100.")
    return level


def _default_authority_for_surface(*, surface: str) -> int:
    surface_norm = _as_non_empty_str(surface).lower()
    return DEFAULT_AUTHORITY_BY_SURFACE.get(surface_norm, 0)


def build_system_scope_context(
    *,
    owner_id: str,
    actor_id: str,
    surface: str,
    room_id: str | None = None,
    room_context_id: str | None = None,
    visibility: str = "owner_only",
    policy_id: str | None = None,
    scope_id: str | None = None,
    authority_level: int = 0,
) -> ScopeContext:
    rid = _as_non_empty_str(room_id)
    rcid = _as_non_empty_str(room_context_id)
    scid = _as_non_empty_str(scope_id) or f"scope::{_as_non_empty_str(surface) or 'system'}::{uuid.uuid4()}"
    scope_dict: Dict[str, Any] = {
        "schema_version": "scope_context_v1",
        "scope_id": scid,
        "owner_id": _as_non_empty_str(owner_id) or "system",
        "actor_id": _as_non_empty_str(actor_id) or "system_actor",
        "surface": _as_non_empty_str(surface) or "system",
        "room_id": rid or None,
        "room_context_id": rcid or None,
        "visibility": visibility if visibility in {"owner_only", "room_shared", "global_shared"} else "owner_only",
        "policy_id": _as_non_empty_str(policy_id) or None,
        "history": {
            "mode": "summary_plus_recent",
            "source": "unified_log",
            "include_room_scoped": False,
            "lookback_hours": _SCOPE_HISTORY_LOOKBACK_HOURS,
            "max_messages": None,
            "max_chars_per_message": None,
        },
        "resources": {
            "allowed_global_resources": ["all"],
            "allowed_room_resources": [],
            "denied_resources": [],
            "resource_groups": [],
        },
        "tools": {
            "allowed_tools": ["all"],
            "blocked_tools": [],
            "requires_approval_tools": [],
            "allow_external_side_effects": False,
        },
        "entities": {
            "enabled": True,
            "allowed_entity_cards": [],
            "pinned_entities": [],
            "entity_lookback_messages": None,
            "entity_lookback_seconds": None,
        },
        "cards": {"enabled": True, "allowed_cards": [], "max_cards_per_turn": None, "max_total_chars": None},
        "writes": {
            "write_unified_log": True,
            "write_kg": False,
            "allow_fact_extraction": False,
            "writable_state_keys": [],
        },
        "delivery": {
            "auto_send": True,
            "allow_initiation": False,
            "allowed_reply_types": [_as_non_empty_str(surface)] if _as_non_empty_str(surface) else [],
        },
        "approval": {
            "authority_level": _coerce_authority_level(
                authority_level,
                field_name="scope.approval.authority_level",
            ),
        },
        "retention": {
            "persist_chat": True,
            "persist_tool_results": True,
            "allow_context_summarization": True,
            "redact_before_persist": False,
        },
        "execution": {"max_turns": None, "max_tool_calls": None, "timeout_seconds": None, "allowed_models": []},
        "delegation": {},
    }
    return ScopeContext.model_validate(scope_dict)


class ScopeAdapter:
    """
    Manager ingress scope adapter and scope factory.

    Responsibilities:
    1) Resolve effective scope contract for manager invocation.
    2) Inherit from parent (message scope) or derive room-root scope.
    3) Apply manager-level narrowing contract (if configured).
    4) Return a normalized Message with scope_context attached.

    Factory methods (``for_sub_manager``, ``for_courier_call``,
    ``for_system_routine``) are call-site seams introduced in the scope-audit
    migration (see ``docs/architecture/SCOPE_AUDIT.md``). They centralize
    scope construction so the semantic shift in Step 5 (authority-cap +
    local tools, replacing tool-list intersection) lands in one place
    instead of 40+. Today these factories preserve current behavior; the
    only thing they change is WHERE scope construction lives.
    """

    # ------------------------------------------------------------------
    # Scope factories (call-site seams for the SCOPE_AUDIT.md migration)
    # ------------------------------------------------------------------

    def for_sub_manager(
        self,
        *,
        parent_scope: ScopeContext | None,
        child_manager_name: str,
    ) -> ScopeContext | None:
        """Construct the ScopeContext to attach to a Message bound for a child
        sub-manager (i.e. the manager-as-tool delegation pattern).

        Today: returns ``parent_scope`` verbatim. Manager-level narrowing
        still happens on the receiving side via ``apply()`` →
        ``_apply_manager_narrowing()`` (which contains the May 5 fix's
        REPLACE-when-declared logic + the related gap when the child's
        scope_contract.tools.allowed_tools is undeclared).

        Future (Step 5 of the migration): builds a fresh scope where
            child.authority = min(parent.authority, child.declared)
            child.tools.allowed_tools is LOCAL (read from child manager
                                                config at use-time)
            child.tools.blocked_tools = parent.blocked ∪ child.blocked
            child identity fields (acting_as, owner_id, room_id, reply_to,
                ...) inherited from parent verbatim

        See docs/architecture/SCOPE_AUDIT.md section 7 for the principle.
        """
        return parent_scope

    @staticmethod
    def for_courier_call(
        *,
        tool_name: str,
        request_id: str | None = None,
        authority_level: int = 100,
        owner_id: str = "jukka",
        actor_id_suffix: str | None = None,
        purpose: str = "courier",
    ) -> ScopeContext:
        """Construct a fresh elevated scope for a tool's internal courier
        work — typically pod projection fetches for secret unsealing.

        Used by tools like ``http_request``, ``execute_code``,
        ``web_type_secret``, ``oauth_token_refresh`` when they need to
        resolve a ``datapod:auth.bearer:.../full`` reference to its
        underlying secret. Authority 100 grants the admin bypass per
        ``compute_approval_reasons`` — required so the courier fetch
        doesn't trigger an approval ticket per secret resolution.

        Today: constructs the same fresh ScopeContext shape that the
        4 courier sites construct inline today (authority=100,
        room=tool_name, allowed_global_resources=["all"]). Step 5 may
        refine the authority bypass logic, but the courier surface area
        is small and stable.

        ``authority_level`` defaults to 100 (admin bypass for elevated
        courier work). A few sites use 50 for non-elevated pod reads
        (e.g. checking token expiry); pass explicitly when needed.

        ``purpose`` distinguishes sub-call shapes within one tool — e.g.
        oauth_token_refresh uses ``purpose="expiry_check"`` for its
        non-elevated read and ``purpose="courier"`` (default) for the
        actual refresh. Becomes part of scope_id for trace visibility.
        """
        suffix = actor_id_suffix if actor_id_suffix is not None else f"request_id={request_id or '?'}"
        actor_id_val = f"{tool_name}:{suffix}" if suffix else tool_name
        return ScopeContext(
            scope_id=f"scope::{tool_name}::{purpose}",
            owner_id=owner_id,
            actor_id=actor_id_val,
            surface="system",
            room_id=tool_name,
            approval=ScopeApprovalPolicy(authority_level=authority_level),
            resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
            tools=ScopeToolPolicy(),
        )

    @staticmethod
    def for_internal_invocation(
        *,
        scope_id: str,
        actor_id: str,
        owner_id: str = "system",
    ) -> ScopeContext:
        """Construct a fresh system-internal scope for tool-driven agent
        invocations (e.g. ``invoke_agent`` calling an agent without a
        chat-originating manager context).

        Caller specifies both ``scope_id`` and ``actor_id`` since the
        invocation contexts vary (invoke_agent uses ``invoke_agent::<agent>``
        as scope_id and ``invoke_agent`` as actor_id; future callers may
        differ).

        Today: matches the existing invoke_agent ScopeContext shape
        (owner_id="system", surface="internal", broad resource access).
        Step 5 will revisit whether system-internal scopes need their
        own authority floor — today they default to authority 0 via
        ScopeApprovalPolicy() default, relying on the tool registry's
        approval gates to handle individual call risk.
        """
        return ScopeContext(
            scope_id=scope_id,
            owner_id=owner_id,
            actor_id=actor_id,
            surface="internal",
            resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
        )

    @staticmethod
    def for_system_routine(
        *,
        routine_id: str,
        owner_id: str,
        actor_id: str | None = None,
        authority_level: int = 0,
        room_id: str | None = None,
        room_context_id: str | None = None,
        visibility: str = "owner_only",
        policy_id: str | None = None,
        scope_id: str | None = None,
    ) -> ScopeContext:
        """Construct a fresh ScopeContext for an autonomous routine run
        (no chat-origin context — subconscious sweeps, KG maintenance,
        wiki refresh, daily-insights pipeline, etc.).

        ``routine_id`` is captured for telemetry / policy lookup; today
        it's stamped into ``policy_id`` if no explicit policy_id is given,
        so /tokens and decision logs can trace authority back to the
        triggering routine.

        Today: thin wrapper around ``build_system_scope_context()`` with
        the routine name folded into policy_id. Tier-2 sites
        (subconscious/run_*.py, routine_handlers/*, kg_investigator,
        kg_maintenance_pipeline, wiki_generator, importance/scoring,
        pod_store/image_pod_enrichment, etc.) currently construct fresh
        ``ScopeContext(...)`` instances directly. Step 2 migrates those
        to this method; Step 5 enables per-routine policy lookup (e.g.
        the routine's declared authority floor).
        """
        effective_actor_id = actor_id if actor_id else owner_id
        effective_policy_id = policy_id if policy_id else f"routine:{routine_id}"
        return build_system_scope_context(
            owner_id=owner_id,
            actor_id=effective_actor_id,
            surface="system",
            room_id=room_id,
            room_context_id=room_context_id,
            visibility=visibility,
            policy_id=effective_policy_id,
            scope_id=scope_id,
            authority_level=authority_level,
        )

    # ------------------------------------------------------------------
    # Manager ingress (existing API — unchanged)
    # ------------------------------------------------------------------

    def apply(self, *, manager_name: str, manager_config: dict[str, Any] | None, message: Message) -> Message:
        if not isinstance(message, Message):
            raise TypeError(f"[{manager_name}] ScopeAdapter expected Message, got {type(message)}")

        scope = self._resolve_scope_context(manager_name=manager_name, manager_config=manager_config or {}, message=message)

        narrowed = self._apply_manager_narrowing(scope=scope, manager_name=manager_name, manager_config=manager_config or {})
        data = self._project_scope_to_runtime_data(
            base_data=message.data if isinstance(message.data, dict) else {},
            scope=narrowed,
        )

        return message.model_copy(update={"scope_context": narrowed, "data": data})

    def _resolve_scope_context(
        self,
        *,
        manager_name: str,
        manager_config: dict[str, Any],
        message: Message,
    ) -> ScopeContext:
        raw_scope = getattr(message, "scope_context", None)
        if isinstance(raw_scope, ScopeContext):
            return raw_scope
        if isinstance(raw_scope, dict):
            return ScopeContext.model_validate(raw_scope)
        if raw_scope is not None:
            raise ValueError(
                f"[{manager_name}] Invalid scope_context type at manager ingress: "
                f"type={type(raw_scope).__name__} module={type(raw_scope).__module__}. "
                "Expected ScopeContext or dict."
            )

        room_scope = self._derive_room_scope(message=message)
        if room_scope is not None:
            return room_scope

        data = message.data if isinstance(message.data, dict) else {}
        data_scope = data.get("scope_contract")
        if isinstance(data_scope, dict):
            return ScopeContext.model_validate(data_scope)

        if self._strict_scope_enabled():
            if self._allow_strict_mode_system_derivation(message=message):
                logger.info(
                    "[%s] Missing explicit scope_context at manager ingress; deriving system scope from task-scoped request data under strict mode.",
                    manager_name,
                )
                return self._derive_system_scope(manager_name=manager_name, message=message)
            raise ValueError(
                f"[{manager_name}] Missing scope_context at manager ingress while strict scope mode is enabled. "
                f"scope_context_type={type(raw_scope).__name__}"
            )
        logger.info("[%s] No explicit scope_context found at manager ingress; deriving system scope.", manager_name)
        return self._derive_system_scope(manager_name=manager_name, message=message)

    @staticmethod
    def _strict_scope_enabled() -> bool:
        raw = str(os.environ.get("SCOPE_CONTRACT_STRICT", "false") or "false").strip().lower()
        return raw in {"1", "true", "yes", "on"}

    @staticmethod
    def _allow_strict_mode_system_derivation(*, message: Message) -> bool:
        data = message.data if isinstance(message.data, dict) else {}
        task_file = _as_non_empty_str(data.get("task_file"))
        has_scope_contract_seed = isinstance(data.get("scope_contract"), dict)
        has_resource_contract = bool(_as_str_list(data.get("allowed_global_resources")))
        return bool(task_file or has_scope_contract_seed or has_resource_contract)

    def _derive_room_scope(self, *, message: Message) -> ScopeContext | None:
        room_id = _as_non_empty_str(getattr(message, "room_id", None))
        if not room_id:
            data = message.data if isinstance(message.data, dict) else {}
            room_id = _as_non_empty_str(data.get("room_id"))
        if not room_id:
            return None

        room_ctx = load_room_context_for_manager(room_id)
        if not isinstance(room_ctx, dict):
            raise ValueError(f"Invalid room context for room_id={room_id!r}")

        room_policy = room_ctx.get("room_policy") if isinstance(room_ctx.get("room_policy"), dict) else {}
        room_permissions = room_ctx.get("room_permissions") if isinstance(room_ctx.get("room_permissions"), dict) else {}
        room_access = room_ctx.get("room_access") if isinstance(room_ctx.get("room_access"), dict) else {}
        retention = room_policy.get("retention") if isinstance(room_policy.get("retention"), dict) else {}
        delivery = room_policy.get("delivery") if isinstance(room_policy.get("delivery"), dict) else {}

        data = message.data if isinstance(message.data, dict) else {}
        reply_to = message.metadata.get("reply_to") if isinstance(message.metadata, dict) and isinstance(message.metadata.get("reply_to"), dict) else {}
        actor_id = (
            _as_non_empty_str(getattr(message, "room_speaker_external_id", None))
            or _as_non_empty_str(data.get("sender_id"))
            or _as_non_empty_str(data.get("sender_name"))
            or _as_non_empty_str(getattr(message, "sender", None))
            or "unknown_actor"
        )

        surface = (
            _as_non_empty_str(getattr(message, "room_surface", None))
            or _as_non_empty_str(room_ctx.get("room_surface"))
            or _as_non_empty_str(reply_to.get("type"))
            or "unknown"
        )

        scope_dict: Dict[str, Any] = {
            "schema_version": "scope_context_v1",
            "scope_id": f"scope::{surface}::{room_id}::{uuid.uuid4()}",
            "owner_id": room_id,
            "actor_id": actor_id,
            "surface": surface,
            "room_id": room_id,
            "room_context_id": (
                _as_non_empty_str(getattr(message, "room_context_id", None))
                or _as_non_empty_str(room_ctx.get("room_context_id"))
                or "main"
            ),
            "visibility": _as_non_empty_str(getattr(message, "room_visibility", None)) or _as_non_empty_str(room_ctx.get("room_visibility")) or "room_shared",
            "policy_id": _as_non_empty_str(getattr(message, "room_policy_id", None)) or _as_non_empty_str(room_ctx.get("room_policy_id")),
            "history": {
                "mode": "summary_plus_recent",
                "source": "unified_log",
                "include_room_scoped": True,
                "lookback_hours": _SCOPE_HISTORY_LOOKBACK_HOURS,
                "max_messages": None,
                "max_chars_per_message": None,
            },
            "resources": {
                "allowed_global_resources": _as_str_list(room_ctx.get("room_allowed_global_resources")) or ["all"],
                "allowed_room_resources": [],
                "denied_resources": [],
                "resource_groups": _as_str_list(room_ctx.get("room_rag_scopes")),
            },
            "tools": {
                "allowed_tools": _resolve_allowed_tools_from_data(data),
                "blocked_tools": _as_str_list(data.get("task_except_tools")),
                "requires_approval_tools": [],
                "allow_external_side_effects": bool(room_permissions.get("tool_classes", {}).get("external_action", False))
                if isinstance(room_permissions.get("tool_classes"), dict)
                else False,
            },
            "entities": {
                "enabled": True,
                "allowed_entity_cards": _as_str_list(room_access.get("allowed_entity_cards")),
                "pinned_entities": _as_str_list(room_access.get("pinned_entities")),
                "entity_lookback_messages": None,
                "entity_lookback_seconds": None,
            },
            "cards": {"enabled": True, "allowed_cards": [], "max_cards_per_turn": None, "max_total_chars": None},
            "writes": {
                "write_unified_log": bool(retention.get("write_unified_log", True)),
                "write_kg": bool(retention.get("write_kg", False)),
                "allow_fact_extraction": bool(retention.get("allow_fact_extraction", False)),
                "writable_state_keys": [],
            },
            "delivery": {
                "auto_send": bool(delivery.get("auto_send", True)),
                "allow_initiation": bool(delivery.get("allow_initiation", False)),
                "allowed_reply_types": [surface],
            },
            "approval": {
                "authority_level": _coerce_authority_level(
                    room_policy.get(
                        "authority_level",
                        _default_authority_for_surface(
                            surface=surface,
                        ),
                    ),
                    field_name="room_policy.authority_level",
                ),
            },
            "retention": {
                "persist_chat": bool(retention.get("persist_chat", True)),
                "persist_tool_results": True,
                "allow_context_summarization": True,
                "redact_before_persist": False,
            },
            "execution": {"max_turns": None, "max_tool_calls": None, "timeout_seconds": None, "allowed_models": []},
            "delegation": {},
        }
        return ScopeContext.model_validate(scope_dict)

    def _derive_system_scope(self, *, manager_name: str, message: Message) -> ScopeContext:
        data = message.data if isinstance(message.data, dict) else {}
        meta = message.metadata if isinstance(message.metadata, dict) else {}
        reply_to = meta.get("reply_to") if isinstance(meta.get("reply_to"), dict) else {}
        request_id = _as_non_empty_str(getattr(message, "request_id", None)) or _as_non_empty_str(data.get("request_id"))
        surface = (
            _as_non_empty_str(getattr(message, "room_surface", None))
            or _as_non_empty_str(reply_to.get("type"))
            or "system"
        )
        owner_id = _as_non_empty_str(data.get("owner_id")) or "system"
        actor_id = (
            _as_non_empty_str(data.get("actor_id"))
            or _as_non_empty_str(getattr(message, "sender", None))
            or "system_actor"
        )
        scope = build_system_scope_context(
            owner_id=owner_id,
            actor_id=actor_id,
            surface=surface,
            room_id=_as_non_empty_str(getattr(message, "room_id", None)) or None,
            room_context_id=_as_non_empty_str(getattr(message, "room_context_id", None)) or None,
            visibility=_as_non_empty_str(getattr(message, "room_visibility", None)) or "owner_only",
            policy_id=_as_non_empty_str(getattr(message, "room_policy_id", None)) or None,
            scope_id=f"scope::{surface}::{manager_name}::{request_id or uuid.uuid4()}",
            authority_level=_coerce_authority_level(
                data.get("authority_level", 0),
                field_name="data.authority_level",
            ),
        )
        payload = scope.model_dump()
        payload["resources"]["allowed_global_resources"] = _as_str_list(data.get("allowed_global_resources"))
        payload["resources"]["allowed_room_resources"] = _as_str_list(data.get("allowed_room_resources"))
        payload["resources"]["resource_groups"] = _as_str_list(data.get("rag_scopes"))
        payload["tools"]["allowed_tools"] = _resolve_allowed_tools_from_data(data)
        payload["tools"]["blocked_tools"] = _as_str_list(data.get("task_except_tools"))
        payload["tools"]["allow_external_side_effects"] = bool(data.get("allow_external_side_effects", False))
        payload["entities"]["allowed_entity_cards"] = _as_str_list(data.get("allowed_entity_cards"))
        payload["entities"]["pinned_entities"] = _as_str_list(data.get("pinned_entities"))
        payload["cards"]["allowed_cards"] = _as_str_list(data.get("allowed_cards"))
        payload["writes"]["write_unified_log"] = bool(data.get("write_unified_log", True))
        payload["writes"]["write_kg"] = bool(data.get("write_kg", False))
        payload["writes"]["allow_fact_extraction"] = bool(data.get("allow_fact_extraction", False))
        payload["writes"]["writable_state_keys"] = _as_str_list(data.get("writable_state_keys"))
        payload["delivery"]["auto_send"] = bool(data.get("auto_send", True))
        payload["delivery"]["allow_initiation"] = bool(data.get("allow_initiation", False))
        payload["delivery"]["allowed_reply_types"] = _as_str_list(data.get("allowed_reply_types"))
        payload["retention"]["persist_chat"] = bool(data.get("persist_chat", True))
        payload["retention"]["persist_tool_results"] = bool(data.get("persist_tool_results", True))
        payload["execution"]["max_turns"] = data.get("max_turns")
        payload["execution"]["max_tool_calls"] = data.get("max_tool_calls")
        payload["execution"]["timeout_seconds"] = data.get("timeout_seconds")
        payload["execution"]["allowed_models"] = _as_str_list(data.get("allowed_models"))
        return ScopeContext.model_validate(payload)

    def _apply_manager_narrowing(
        self,
        *,
        scope: ScopeContext,
        manager_name: str,
        manager_config: dict[str, Any],
    ) -> ScopeContext:
        narrowing = manager_config.get("scope_contract")
        if not isinstance(narrowing, dict):
            return scope

        try:
            payload = scope.model_dump()
            tools_cfg = narrowing.get("tools") if isinstance(narrowing.get("tools"), dict) else {}
            if tools_cfg:
                if "allowed_tools" in tools_cfg:
                    allowed = _as_str_list(tools_cfg.get("allowed_tools"))
                    # Manager's own scope_contract is authoritative for its
                    # own leaves. The parent's ``allowed_tools`` describes
                    # what the PARENT directly calls — it should NOT
                    # narrow what a sub-manager can do internally.
                    # Allowing a manager implicitly allows the tools that
                    # manager declares it uses.
                    #
                    # Restrictions still propagate via ``blocked_tools``:
                    # parent denylist + sub-manager denylist + room policy
                    # denylist all union below. So a Slack room can block
                    # specific tools even inside a delegated web_manager
                    # call, but it doesn't have to enumerate every leaf
                    # the sub-manager uses just to allow the manager.
                    payload["tools"]["allowed_tools"] = allowed
                if "blocked_tools" in tools_cfg:
                    blocked = _as_str_list(tools_cfg.get("blocked_tools"))
                    payload["tools"]["blocked_tools"] = list({*payload["tools"].get("blocked_tools", []), *blocked})
                if "requires_approval_tools" in tools_cfg:
                    requires_approval = _as_str_list(tools_cfg.get("requires_approval_tools"))
                    payload["tools"]["requires_approval_tools"] = list(
                        {
                            *payload["tools"].get("requires_approval_tools", []),
                            *requires_approval,
                        }
                    )
                if bool(payload["tools"].get("allow_external_side_effects", False)) and not bool(
                    tools_cfg.get("allow_external_side_effects", True)
                ):
                    payload["tools"]["allow_external_side_effects"] = False

            resources_cfg = narrowing.get("resources") if isinstance(narrowing.get("resources"), dict) else {}
            if resources_cfg:
                payload["resources"]["allowed_global_resources"] = _intersect(
                    payload["resources"].get("allowed_global_resources", []),
                    _as_str_list(resources_cfg.get("allowed_global_resources")),
                )
                payload["resources"]["allowed_room_resources"] = _intersect(
                    payload["resources"].get("allowed_room_resources", []),
                    _as_str_list(resources_cfg.get("allowed_room_resources")),
                )

            entities_cfg = narrowing.get("entities") if isinstance(narrowing.get("entities"), dict) else {}
            if entities_cfg:
                if bool(payload["entities"].get("enabled", True)) and not bool(entities_cfg.get("enabled", True)):
                    payload["entities"]["enabled"] = False
                payload["entities"]["allowed_entity_cards"] = _intersect(
                    payload["entities"].get("allowed_entity_cards", []),
                    _as_str_list(entities_cfg.get("allowed_entity_cards")),
                )
                payload["entities"]["pinned_entities"] = _intersect(
                    payload["entities"].get("pinned_entities", []),
                    _as_str_list(entities_cfg.get("pinned_entities")),
                )

            writes_cfg = narrowing.get("writes") if isinstance(narrowing.get("writes"), dict) else {}
            if writes_cfg:
                for key in ("write_unified_log", "write_kg", "allow_fact_extraction"):
                    if key in writes_cfg and bool(payload["writes"].get(key, False)) and not bool(writes_cfg.get(key)):
                        payload["writes"][key] = False
                    if key in writes_cfg and not bool(payload["writes"].get(key, False)) and bool(writes_cfg.get(key)):
                        raise ValueError(
                            f"[{manager_name}] scope_contract attempted to expand writes.{key} from false to true."
                        )

            approval_cfg = narrowing.get("approval") if isinstance(narrowing.get("approval"), dict) else {}
            if approval_cfg and "authority_level" in approval_cfg:
                parent_level = _coerce_authority_level(
                    payload.get("approval", {}).get("authority_level", 0),
                    field_name="scope.approval.authority_level",
                )
                requested_level = _coerce_authority_level(
                    approval_cfg.get("authority_level"),
                    field_name=f"{manager_name}.scope_contract.approval.authority_level",
                )
                if requested_level > parent_level:
                    raise ValueError(
                        f"[{manager_name}] scope_contract attempted to expand approval.authority_level "
                        f"from {parent_level} to {requested_level}."
                    )
                payload["approval"]["authority_level"] = requested_level
                # Note: manager narrowing only clamps authority_level. Other approval behavior
                # (dangerous-tool gates, install gates) is not configurable per-manager at this time.

            return ScopeContext.model_validate(payload)
        except Exception as e:
            logger.error("[%s] Failed applying manager scope_contract narrowing: %s", manager_name, e)
            logger.debug("[%s] manager scope_contract narrowing exception details", manager_name, exc_info=True)
            raise

    def _project_scope_to_runtime_data(self, *, base_data: dict[str, Any], scope: ScopeContext) -> dict[str, Any]:
        data = dict(base_data) if isinstance(base_data, dict) else {}
        data["scope_contract_enforced"] = True
        data["scope_contract"] = scope.model_dump()

        # Bridge contract into existing runtime knobs so managers/agents/tools reuse current scaffolding.
        scoped_allowed = list(scope.tools.allowed_tools or [])
        scoped_blocked = list(scope.tools.blocked_tools or [])

        # Preserve explicit task-spec restrictions when provided.
        if "task_allowed_tools" in data and data.get("task_allowed_tools") is not None:
            explicit_allowed_raw = data.get("task_allowed_tools")
            if not isinstance(explicit_allowed_raw, list):
                raise ValueError(
                    f"task_allowed_tools must be a list when provided, got {type(explicit_allowed_raw).__name__}."
                )
            explicit_allowed = _validate_allowed_tools_list(
                values=_as_str_list(explicit_allowed_raw),
                field_name="task_allowed_tools",
            )
            data["task_allowed_tools"] = _intersect_allowed_tools(scoped_allowed, explicit_allowed)
        else:
            data["task_allowed_tools"] = scoped_allowed

        # Blocked tools are additive: scope-level blocked + task-level blocked.
        if "task_except_tools" in data and data.get("task_except_tools") is not None:
            explicit_blocked_raw = data.get("task_except_tools")
            if not isinstance(explicit_blocked_raw, list):
                raise ValueError(
                    f"task_except_tools must be a list when provided, got {type(explicit_blocked_raw).__name__}."
                )
            explicit_blocked = _as_str_list(explicit_blocked_raw)
            data["task_except_tools"] = list({*scoped_blocked, *explicit_blocked})
        else:
            data["task_except_tools"] = scoped_blocked

        # Optional precomputed visible tool list (ingress layer), constrained by
        # the effective tool policy after parent+manager+task narrowing.
        if "visible_tools" in data and data.get("visible_tools") is not None:
            visible_raw = data.get("visible_tools")
            if not isinstance(visible_raw, list):
                raise ValueError(
                    f"visible_tools must be a list when provided, got {type(visible_raw).__name__}."
                )
            visible_clean = _as_str_list(visible_raw)
            allowed_effective = _as_str_list(data.get("task_allowed_tools"))
            blocked_effective = set(_as_str_list(data.get("task_except_tools")))
            allow_all = len(allowed_effective) == 1 and allowed_effective[0] == "all"
            allowed_set = set(allowed_effective)
            visible_out: list[str] = []
            for name in visible_clean:
                if not allow_all and name not in allowed_set:
                    continue
                if name in blocked_effective:
                    continue
                if name not in visible_out:
                    visible_out.append(name)
            data["visible_tools"] = visible_out
        data["allowed_global_resources"] = list(scope.resources.allowed_global_resources or [])
        data["allowed_room_resources"] = list(scope.resources.allowed_room_resources or [])
        data["allowed_entity_cards"] = list(scope.entities.allowed_entity_cards or [])
        data["pinned_entities"] = list(scope.entities.pinned_entities or [])
        data["rag_scopes"] = list(scope.resources.resource_groups or [])
        data["write_unified_log"] = bool(scope.writes.write_unified_log)
        data["write_kg"] = bool(scope.writes.write_kg)
        data["allow_fact_extraction"] = bool(scope.writes.allow_fact_extraction)
        data["auto_send"] = bool(scope.delivery.auto_send)
        data["allow_initiation"] = bool(scope.delivery.allow_initiation)
        data["allowed_reply_types"] = list(scope.delivery.allowed_reply_types or [])
        return data
