from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from sqlalchemy import select

from jinja2 import Template

from app.assistant.database.db_handler import UnifiedLog2026
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.agent_runtime.services.resource_resolver import ResourceResolver
from app.assistant.agent_runtime.services.user_bio_context_service import UserBioContextService
from app.models.base import get_session
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import get_local_time, get_local_time_str
from app.assistant.utils.assistant_name import get_assistant_name

logger = get_logger(__name__)


def _filter_msgs_by_session_start(msgs: list, agent: Any) -> list:
    """
    If the blackboard carries a `doc_session_start_utc` value, filter the message list
    to only messages at or after that timestamp. Used to scope doc_writer history to
    the current doc session so prior-room conversation is not leaked in.
    """
    raw = agent.blackboard.get_state_value("doc_session_start_utc", None)
    if not isinstance(raw, str) or not raw.strip():
        return msgs
    try:
        cutoff = datetime.fromisoformat(raw.strip())
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        cutoff = cutoff.astimezone(timezone.utc)
    except Exception as e:
        logger.debug("_filter_msgs_by_session_start: could not parse cutoff %r: %s", raw, e)
        return msgs
    filtered = []
    for m in msgs:
        ts = getattr(m, "timestamp", None)
        if ts is None:
            filtered.append(m)
            continue
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts = ts.astimezone(timezone.utc)
        if ts >= cutoff:
            filtered.append(m)
    logger.debug(
        "_filter_msgs_by_session_start: cutoff=%s kept %d/%d msgs",
        cutoff.isoformat(), len(filtered), len(msgs),
    )
    return filtered



class ContextInjector:
    def __init__(self, *, blackboard: Any = None) -> None:
        self._blackboard = blackboard

    @staticmethod
    def _is_context_suppressed(msg: Any) -> bool:
        meta = getattr(msg, "metadata", None)
        if not isinstance(meta, dict):
            return False
        if bool(meta.get("context_pinned", False)):
            return False
        return bool(meta.get("context_suppressed", False))

    @staticmethod
    def _resolve_unified_lookback_hours(agent) -> float:
        """
        Fallback recency window for unified history reads when no summary cutoff exists.

        Resolution order:
          1. agent.blackboard state key "history_unified_lookback_hours" (runtime override)
          2. agent.config["history_lookback_hours"] (static config.yaml setting)
          3. hard-coded default of 36 hours
        """
        default_hours = 36
        # Check static agent config first so config.yaml acts as the declarative default.
        config_default = default_hours
        try:
            cfg = getattr(agent, "config", None)
            if isinstance(cfg, dict) and "history_lookback_hours" in cfg:
                config_default = int(cfg["history_lookback_hours"])
        except Exception as e:
            logger.error("[%s] Failed reading history_lookback_hours from agent config: %s", getattr(agent, "name", "agent"), e)
            logger.debug("[%s] agent config history_lookback_hours read exception details", getattr(agent, "name", "agent"), exc_info=True)
        try:
            raw = agent.blackboard.get_state_value("history_unified_lookback_hours", config_default)
        except Exception as e:
            logger.error("[%s] Failed reading history_unified_lookback_hours: %s", getattr(agent, "name", "agent"), e)
            logger.debug("[%s] history_unified_lookback_hours read exception details", getattr(agent, "name", "agent"), exc_info=True)
            raw = config_default
        try:
            value = float(raw)
        except Exception as e:
            logger.error(
                "[%s] Invalid history_unified_lookback_hours value '%s': %s",
                getattr(agent, "name", "agent"),
                raw,
                e,
            )
            logger.debug("[%s] history_unified_lookback_hours parse exception details", getattr(agent, "name", "agent"), exc_info=True)
            value = float(default_hours)
        if value <= 0:
            raise ValueError("history_unified_lookback_hours must be > 0")
        return value

    def _refresh_resource_cache_entry(self, agent, resource_id: str) -> None:
        try:
            from app.assistant.ServiceLocator.service_locator import DI

            resource_manager = getattr(DI, "resource_manager", None)
            if resource_manager is None:
                logger.error("[%s] DI.resource_manager is unavailable for refresh of '%s'.", agent.name, resource_id)
                raise RuntimeError("resource_manager unavailable")
            resource_manager.refresh_resource(resource_id)
        except Exception as e:
            logger.error("[%s] Failed refreshing resource cache for '%s': %s", agent.name, resource_id, e)
            logger.debug("[%s] resource cache refresh exception details", agent.name, exc_info=True)
            raise


    @staticmethod
    def _to_utc(ts: Any) -> datetime | None:
        if ts is None:
            return None
        if isinstance(ts, datetime):
            try:
                if ts.tzinfo is None:
                    return ts.replace(tzinfo=timezone.utc)
                return ts.astimezone(timezone.utc)
            except Exception as e:
                logger.error("Failed converting datetime to UTC: %s", e)
                logger.debug("UTC conversion exception details", exc_info=True)
                return None
        if isinstance(ts, str):
            raw = ts.strip()
            if not raw:
                return None
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except Exception as e:
                logger.error("Failed parsing ISO datetime '%s': %s", raw, e)
                logger.debug("ISO datetime parse exception details", exc_info=True)
                return None
        return None

    def _load_unified_chat_rows(self, *, cutoff_utc: datetime | None, limit: int | None = None) -> list[dict]:
        """Load master-room chat history from unified_log.

        The room_id == 'master_room' predicate is mandatory, not defensive.
        source='chat' happens to currently coincide with master_room in the
        historical data, but that is a coincidence of the write-path, not an
        invariant. Without an explicit room filter, any future code that
        writes source='chat' from Slack/doc_editor/task_spec would cross-
        contaminate the history injected into every agent prompt that
        enables the `history` context key.
        """
        session = get_session()
        try:
            stmt = (
                select(
                    UnifiedLog2026.id,
                    UnifiedLog2026.timestamp,
                    UnifiedLog2026.role,
                    UnifiedLog2026.speaker_name,
                    UnifiedLog2026.speaker_role,
                    UnifiedLog2026.message,
                )
                    .where(UnifiedLog2026.source == "chat")
                    .where(UnifiedLog2026.room_id == "master_room")
                    .where(UnifiedLog2026.role.in_(["user", "assistant"]))
                    .order_by(UnifiedLog2026.timestamp.desc())
            )
            if isinstance(limit, int) and limit > 0:
                stmt = stmt.limit(limit)
            if cutoff_utc is not None:
                stmt = stmt.where(UnifiedLog2026.timestamp > cutoff_utc)

            rows = session.execute(stmt).all()
            out: list[dict] = []
            for row in rows:
                out.append(
                    {
                        "id": str(row.id) if row.id is not None else None,
                        "timestamp": row.timestamp,
                        "role": str(row.role or "").strip().lower(),
                        "speaker_name": str(row.speaker_name or "").strip(),
                        "speaker_role": str(row.speaker_role or "").strip().lower(),
                        "message": str(row.message or ""),
                    }
                )
            out.reverse()
            return out
        except Exception as e:
            logger.error("Failed loading unified chat rows for history prep: %s", e)
            logger.debug("unified chat load exception details", exc_info=True)
            return []
        finally:
            session.close()

    @staticmethod
    def _is_room_scoped_message(msg: Any) -> bool:
        try:
            rid = getattr(msg, "room_id", None)
            if isinstance(rid, str) and rid.strip():
                return True
            surface = getattr(msg, "room_surface", None)
            if isinstance(surface, str) and surface.strip():
                return True
            data = getattr(msg, "data", None)
            if isinstance(data, dict):
                rid2 = data.get("room_id")
                if isinstance(rid2, str) and rid2.strip():
                    return True
                surface2 = data.get("room_surface")
                if isinstance(surface2, str) and surface2.strip():
                    return True
            meta = getattr(msg, "metadata", None)
            if isinstance(meta, dict):
                reply_to = meta.get("reply_to")
                if isinstance(reply_to, dict):
                    rt = str(reply_to.get("type") or "").strip().lower()
                    if rt in {"slack", "twilio_sms", "telegram"}:
                        return True
        except Exception as e:
            logger.error("Failed room-scoped detection in ContextInjector: %s", e)
            logger.debug("ContextInjector room-scoped detection exception details", exc_info=True)
            return False
        return False

    @staticmethod
    def _resolve_history_sender_from_row(row: dict) -> str:
        speaker_name = str(row.get("speaker_name") or "").strip()
        if speaker_name:
            return speaker_name

        role = str(row.get("role") or "").strip().lower()
        speaker_role = str(row.get("speaker_role") or "").strip().lower()
        effective_role = role or speaker_role
        if effective_role in {"user", "user_msg", "human", "participant"}:
            return "User"
        _asst = get_assistant_name().lower()
        if effective_role in {"assistant", _asst, f"{_asst}_agent", "assistant_msg", "agent"}:
            return get_assistant_name()
        if effective_role:
            return effective_role.replace("_", " ").title()
        return "Unknown"

    def _build_history_for_prompt(self, agent, *, limit: int | None = None) -> str:
        try:
            if self._blackboard is not None and callable(getattr(self._blackboard, "get_messages", None)):
                messages = self._blackboard.get_messages()
            else:
                getter = getattr(agent.blackboard, "get_messages", None)
                messages = getter() if callable(getter) else []
        except Exception as e:
            logger.error("[%s] Failed to load messages for history injection: %s", agent.name, e)
            logger.debug("[%s] history load exception details", agent.name, exc_info=True)
            return ""

        excluded_tags = {"history_summary", "entity_card_injection", "agent_notification"}
        selected = []
        latest_summary_text = ""
        summary_cutoff_utc: datetime | None = None

        for msg in reversed(messages or []):
            try:
                if not bool(getattr(msg, "is_chat", False)):
                    continue
                if self._is_room_scoped_message(msg):
                    continue
                sub_types = set(getattr(msg, "sub_data_type", []) or [])
                if "history_summary" not in sub_types:
                    continue
                content = (getattr(msg, "content", None) or "").strip()
                if not content:
                    continue
                latest_summary_text = content
                meta = getattr(msg, "metadata", None)
                if isinstance(meta, dict):
                    summary_cutoff_utc = self._to_utc(meta.get("summarized_through_utc"))
                break
            except Exception as e:
                logger.error("[%s] Failed resolving latest summary marker for history: %s", agent.name, e)
                logger.debug("[%s] summary marker resolution exception details", agent.name, exc_info=True)
                continue

        for msg in messages or []:
            try:
                if not bool(getattr(msg, "is_chat", False)):
                    continue
                if self._is_context_suppressed(msg):
                    continue
                if self._is_room_scoped_message(msg):
                    continue
                sub_types = set(getattr(msg, "sub_data_type", []) or [])
                if sub_types.intersection(excluded_tags):
                    continue
                meta = getattr(msg, "metadata", None)
                if isinstance(meta, dict) and bool(meta.get("summarized", False)):
                    continue
                content = (getattr(msg, "content", None) or "").strip()
                if not content:
                    continue
                ts_utc = self._to_utc(getattr(msg, "timestamp", None))
                if summary_cutoff_utc is not None and ts_utc is not None and ts_utc <= summary_cutoff_utc:
                    # When summary exists, older/equal raw items are compressed into summary.
                    continue
                selected.append(msg)
            except Exception as e:
                logger.error("[%s] Failed filtering message for history injection: %s", agent.name, e)
                logger.debug("[%s] history filter exception details", agent.name, exc_info=True)
                continue

        # Always enforce the agent's configured lookback window.  If a summary boundary already
        # exists we take the *more recent* of the two so short-window agents (e.g. switchboard)
        # don't receive history older than their configured cap even when a summary is present.
        lookback_hours = self._resolve_unified_lookback_hours(agent)
        lookback_cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        if summary_cutoff_utc is None:
            summary_cutoff_utc = lookback_cutoff
        else:
            summary_cutoff_utc = max(summary_cutoff_utc, lookback_cutoff)

        try:
            selected.sort(key=lambda x: getattr(x, "timestamp", None) or 0)
        except Exception as e:
            logger.error("[%s] Failed sorting history; using insertion order: %s", agent.name, e)
            logger.debug("[%s] history sort exception details", agent.name, exc_info=True)

        combined: list[tuple[datetime | None, str, str]] = []
        selected_ids = set()
        for msg in selected:
            try:
                msg_id = getattr(msg, "id", None)
                if isinstance(msg_id, str) and msg_id.strip():
                    selected_ids.add(msg_id.strip())
                ts_utc = self._to_utc(getattr(msg, "timestamp", None))
                sender = getattr(msg, "sender", None) or getattr(msg, "role", None) or "Unknown"
                content = (getattr(msg, "content", None) or "").strip()
                if content:
                    combined.append((ts_utc, str(sender), content))
            except Exception as e:
                logger.error("[%s] Failed converting selected message for combined history: %s", agent.name, e)
                logger.debug("[%s] combined history conversion exception details", agent.name, exc_info=True)
                continue

        # Pull durable chat rows from unified log; include only rows newer than summary cutoff.
        unified_rows = self._load_unified_chat_rows(cutoff_utc=summary_cutoff_utc, limit=None)
        for row in unified_rows:
            try:
                row_id = row.get("id")
                if isinstance(row_id, str) and row_id.strip() and row_id.strip() in selected_ids:
                    continue
                content = str(row.get("message") or "").strip()
                if not content:
                    continue
                sender = self._resolve_history_sender_from_row(row)
                ts_utc = self._to_utc(row.get("timestamp"))
                combined.append((ts_utc, sender, content))
            except Exception as e:
                logger.error("[%s] Failed converting unified row for combined history: %s", agent.name, e)
                logger.debug("[%s] unified row conversion exception details", agent.name, exc_info=True)
                continue

        if not combined and not latest_summary_text:
            return ""

        try:
            combined.sort(key=lambda x: (x[0] is None, x[0] or datetime.min.replace(tzinfo=timezone.utc)))
        except Exception as e:
            logger.error("[%s] Failed sorting combined history rows; using insertion order: %s", agent.name, e)
            logger.debug("[%s] combined history sort exception details", agent.name, exc_info=True)

        if isinstance(limit, int) and limit > 0 and len(combined) > limit:
            combined = combined[-limit:]

        lines = []
        if latest_summary_text:
            lines.append(f"[Chat Summary]: {latest_summary_text}")

        for ts_utc, sender, content in combined:
            if ts_utc:
                try:
                    from app.assistant.utils.time_utils import utc_to_local

                    local_dt = utc_to_local(ts_utc)
                    now_local = utc_to_local(datetime.now(timezone.utc))
                    if local_dt.date() == now_local.date():
                        local = local_dt.strftime("%H:%M")
                    else:
                        local = local_dt.strftime("%Y-%m-%d %H:%M")
                    lines.append(f"[{local}] {sender}: {content}")
                    continue
                except Exception as e:
                    logger.error("[%s] Failed formatting timestamp in history: %s", agent.name, e)
                    logger.debug("[%s] history timestamp format exception details", agent.name, exc_info=True)
            lines.append(f"{sender}: {content}")
        return "\n".join(lines).strip()

    def resolve_resource(self, agent, resource_id: str) -> Any:
        if not resource_id:
            return ""

        scope_context = agent.blackboard.get_state_value("scope_context", None)
        if scope_context is None:
            raise ValueError(f"[{agent.name}] scope_context is required for resource resolution.")

        value = None
        try:
            value = ResourceResolver.get_global_resource(resource_id, required=False, scope_context=scope_context)
        except Exception as e:
            # Scope policy blocks are expected for non-UI surfaces (Telegram, SMS)
            # that have restricted resource access.  Degrade gracefully.
            logger.info("[%s] Resource '%s' unavailable (scope policy or read error): %s", agent.name, resource_id, e)
            return ""

        if value is None:
            logger.info("[%s] Canonical resource '%s' not found in global blackboard.", agent.name, resource_id)
            return ""

        if isinstance(value, str) and ("{{" in value or "{%" in value):
            logger.error(
                "[%s] Resource '%s' contains template tokens at injection time. Templates must be compiled first.",
                agent.name,
                resource_id,
            )
            raise ValueError(f"Resource '{resource_id}' is not compiled.")

        return value

    def resolve_skills(self, agent) -> Dict[str, str]:
        """Resolve all skills (static + auto-injected) into a name → body dict.

        Two paths produce skills for an agent:

        1. **Static** — ``agent.config['skills']`` lists skill names the
           agent always wants. Looked up in SkillRegistry and added.
        2. **Auto-injected** — when ``agent.config['accept_auto_skills']``
           is true, the SkillInjector evaluates each skill's
           ``auto_inject_when`` trigger against the current task /
           incoming_message and adds matching skills.

        Both paths feed the same dict (templates render the same way).
        Missing skills are logged and dropped — never crash the prompt.
        """
        registry = getattr(DI, "skill_registry", None)
        if registry is None:
            logger.warning("[%s] DI.skill_registry not available — no skills loaded", agent.name)
            return {}

        resolved: Dict[str, str] = {}

        # 1. Static skills from agent config.
        for name in (agent.config.get("skills", []) or []):
            if not isinstance(name, str) or not name.strip():
                continue
            skill = registry.get(name.strip())
            if skill is None:
                logger.warning(
                    "[%s] declared skill %r not registered — dropped", agent.name, name,
                )
                continue
            resolved[skill.name] = skill.body

        # 2. Auto-injected skills (opt-in via accept_auto_skills).
        if agent.config.get("accept_auto_skills"):
            injector = getattr(DI, "skill_injector", None)
            if injector is None:
                logger.warning(
                    "[%s] accept_auto_skills=true but DI.skill_injector unavailable", agent.name,
                )
            else:
                task = str(agent.blackboard.get_state_value("task", "") or "")
                incoming = str(agent.blackboard.get_state_value("incoming_message", "") or "")
                for name in injector.matching_skill_names(task=task, incoming_message=incoming):
                    if name in resolved:
                        continue
                    skill = registry.get(name)
                    if skill is None:
                        continue
                    resolved[name] = skill.body
                    logger.debug("[%s] auto-injected skill=%s", agent.name, name)

        # 3. Caller-supplied skills (per-invocation, via agent_input["skills_input"]).
        #    Spec-compliant: a flat list of skill names. No transitive close,
        #    no requires-graph — the Anthropic Agent Skills spec doesn't
        #    declare skill dependencies in frontmatter. Composition happens
        #    via SKILL.md bodies referencing bundled files or other skills by
        #    name; the caller is responsible for passing the full set of
        #    skills it wants delivered.
        requested = agent.blackboard.get_state_value("skills_input", []) or []
        if not isinstance(requested, list):
            logger.warning(
                "[%s] skills_input must be a list of skill names; got %s",
                agent.name, type(requested).__name__,
            )
        else:
            for name in requested:
                if not isinstance(name, str) or not name.strip():
                    continue
                name = name.strip()
                if name in resolved:
                    continue
                skill = registry.get(name)
                if skill is None:
                    logger.warning(
                        "[%s] requested skill %r not registered — dropped",
                        agent.name, name,
                    )
                    continue
                resolved[name] = skill.body
                logger.debug("[%s] caller-supplied skill=%s", agent.name, name)
        return resolved

    def generate_injections_block(self, agent, prompt_injections, message=None, entity_injection_keys: set[str] | None = None):
        if not isinstance(prompt_injections, list):
            raise ValueError(
                f"[{agent.name}] 'context_items' must be a list, but got: "
                f"{type(prompt_injections).__name__} ({prompt_injections})"
            )

        keys = entity_injection_keys or set()
        entity_keys = [key for key in prompt_injections if isinstance(key, str) and key in keys]
        non_entity_keys = [key for key in prompt_injections if key not in entity_keys]
        entity_field_keys = [key[len("entity_"):] for key in entity_keys if key.startswith("entity_")]

        context: Dict[str, Any] = {
            "date_time": get_local_time_str(),
            "day_of_week": datetime.now().strftime("%A"),
            "action_count": agent.blackboard.get_state_value(f"{agent.name}_action_count", 0),
            "room_contact_name": str(agent.blackboard.get_state_value("room_contact_name", "") or "").strip(),
            "current_speaker_name": str(agent.blackboard.get_state_value("current_speaker_name", "") or "").strip(),
            # Skills declared at agent.config.skills are resolved here once,
            # exposed as a dict that templates access via
            # ``{{ skills["skill-name"] }}``. Missing skills are dropped
            # with a warning, never crash the prompt.
            "skills": self.resolve_skills(agent),
        }

        if message is not None:
            incoming_value = (
                    getattr(message, "agent_input", None)
                    or getattr(message, "content", None)
                    or getattr(message, "task", None)
            )
            if isinstance(incoming_value, str) and incoming_value.strip():
                context["incoming_message"] = incoming_value.strip()

        if "incoming_message" not in context:
            bb_task = str(agent.blackboard.get_state_value("task", "") or "").strip()
            if bb_task:
                context["incoming_message"] = bb_task

        for key in non_entity_keys:
            if key in context:
                continue

            if key.startswith("resource_"):
                context[key] = self.resolve_resource(agent, key)
                continue

            if key == "tool_descriptions":
                tool_desc = agent.get_tool_descriptions() or {}
                if not isinstance(tool_desc, dict):
                    logger.error("[%s] tool_descriptions must be a dict, got: %s", agent.name, type(tool_desc))
                    tool_desc = {}
                context[key] = tool_desc
                continue

            if key == "allowed_nodes":
                allowed = agent.get_allowed_nodes()
                agent_descriptions = []
                for name in allowed:
                    agent_config = agent.agent_registry.get_agent_config(name) or {}
                    prompts = agent_config.get("prompts", {})
                    raw_description = prompts.get("description", "")
                    try:
                        template = Template(raw_description)
                        rendered_description = template.render(
                            self_name=name,
                            self_short_name=name.split("::")[-1],
                        )
                    except Exception as e:
                        logger.error("[%s] Error rendering description for agent '%s': %s", agent.name, name, e)
                        logger.debug("[%s] allowed_nodes description exception details", agent.name, exc_info=True)
                        rendered_description = raw_description
                    agent_descriptions.append({"name": name, "description": rendered_description})

                context[key] = agent_descriptions
                continue

            if key == "recent_history":
                # recent_history is local-manager context only.
                # Do not allow room/global scope overrides to change planner-local history resolution.
                target_scope_id = agent.blackboard.get_current_scope_id()
                if not isinstance(target_scope_id, str) or not target_scope_id.strip():
                    raise ValueError(f"[{agent.name}] current scope id is required for recent_history injection.")
                msgs = agent.blackboard.get_messages_for_scope(target_scope_id)
                rendered = agent.build_recent_history(msgs)
                logger.info(
                    "[HISTORY_DIAG:E] recent_history for agent=%s scope_id=%s msg_count=%d rendered_len=%d",
                    agent.name, target_scope_id, len(msgs) if msgs else 0, len(rendered or ""),
                )
                context[key] = rendered
                continue

            if key == "latest_exchange":
                target_scope_id = agent.blackboard.get_current_scope_id()
                if not isinstance(target_scope_id, str) or not target_scope_id.strip():
                    raise ValueError(f"[{agent.name}] current scope id is required for latest_exchange injection.")
                msgs = agent.blackboard.get_messages_for_scope(target_scope_id)
                msgs = _filter_msgs_by_session_start(msgs, agent)
                context[key] = agent.components.history_formatter.format_latest_exchange(msgs)
                continue

            if key == "prior_history":
                target_scope_id = agent.blackboard.get_current_scope_id()
                if not isinstance(target_scope_id, str) or not target_scope_id.strip():
                    raise ValueError(f"[{agent.name}] current scope id is required for prior_history injection.")
                msgs = agent.blackboard.get_messages_for_scope(target_scope_id)
                msgs = _filter_msgs_by_session_start(msgs, agent)
                # prior_history = all messages except the last user+assistant exchange
                latest_text = agent.components.history_formatter.format_latest_exchange(msgs)
                full_text = agent.build_recent_history(msgs)
                # Strip the latest exchange from the end of full history
                if latest_text and full_text.endswith(latest_text):
                    prior = full_text[: -len(latest_text)].rstrip()
                else:
                    prior = full_text
                context[key] = prior
                continue

            if key == "history":
                try:
                    injected_history = agent.blackboard.get_state_value("history", "__missing__")
                    if injected_history != "__missing__":
                        if injected_history is None:
                            context[key] = ""
                        elif isinstance(injected_history, str):
                            context[key] = injected_history
                        else:
                            raise ValueError(
                                f"[{agent.name}] Injected history must be a string, got {type(injected_history).__name__}."
                            )
                        continue

                    custom_builder = getattr(agent, "build_history", None)
                    if callable(custom_builder):
                        custom = custom_builder()
                        context[key] = str(custom or "")
                    else:
                        context[key] = self._build_history_for_prompt(agent)
                except Exception as e:
                    logger.error("[%s] Failed building history for prompt injection: %s", agent.name, e)
                    logger.debug("[%s] history builder exception details", agent.name, exc_info=True)
                    context[key] = ""
                continue

            if key == "user_bio_context":
                try:
                    incoming = str(context.get("incoming_message") or "").strip()
                    if not incoming:
                        incoming = str(context.get("task") or agent.blackboard.get_state_value("task", "") or "").strip()
                    if not incoming:
                        incoming = str(agent.blackboard.get_state_value("agent_input", "") or "").strip()
                    logger.debug(
                        "[%s] user_bio_context: incoming=%r (source: %s)",
                        agent.name,
                        incoming if incoming else "(empty)",
                        "incoming_message" if context.get("incoming_message") else "task" if (context.get("task") or agent.blackboard.get_state_value("task")) else "agent_input" if incoming else "none",
                    )
                    if not incoming:
                        logger.debug("[%s] user_bio_context: no incoming text found — skipping.", agent.name)
                    result = UserBioContextService.build_context(agent=agent, incoming_text=incoming)
                    logger.debug("[%s] user_bio_context: result length=%d", agent.name, len(result))
                    context[key] = result
                except Exception as e:
                    logger.error("[%s] Failed building user_bio_context: %s", agent.name, e)
                    logger.debug("[%s] user_bio_context builder exception details", agent.name, exc_info=True)
                    context[key] = ""
                continue

            if key == "health_status_summary":
                try:
                    health_payload = self.resolve_resource(agent, "resource_health_inference_output")
                    if health_payload in ("", None):
                        context[key] = ""
                    elif not isinstance(health_payload, dict):
                        raise ValueError(
                            f"[{agent.name}] resource_health_inference_output must be a dict for health_status_summary."
                        )
                    else:
                        context[key] = str(health_payload.get("general_health_assessment") or "").strip()
                except Exception as e:
                    logger.error("[%s] Failed building health_status_summary: %s", agent.name, e)
                    logger.debug("[%s] health_status_summary exception details", agent.name, exc_info=True)
                    context[key] = ""
                continue

            if key == "chat_nudges":
                # Pick a pending question to nudge the chat agent with.
                # The nudge becomes a hint in the agent's context (not
                # a mechanical append) — the agent decides whether to
                # weave it into the reply naturally. Mark-asked happens
                # inside pick_question_for_nudge so the same question
                # doesn't surface every turn (noticer can re-emit if
                # the underlying concern persists).
                try:
                    from app.assistant.pending_questions import (
                        pick_question_for_nudge,
                    )
                    picked = pick_question_for_nudge(topic_tag=None)
                    if picked is None:
                        context[key] = ""
                    else:
                        _qid, q_text = picked
                        context[key] = q_text
                except Exception as e:
                    logger.debug(
                        "[%s] chat_nudges lookup failed: %s",
                        agent.name, e, exc_info=True,
                    )
                    context[key] = ""
                continue

            if key == "chat_memory":
                try:
                    from app.assistant.agent_runtime.services.chat_memory_rag import recall, format_recall_for_prompt
                    # Use the user's current message as the query.
                    query = str(context.get("incoming_message") or "").strip()
                    if not query:
                        query = str(context.get("task") or agent.blackboard.get_state_value("task", "") or "").strip()
                    if not query:
                        query = str(agent.blackboard.get_state_value("agent_input", "") or "").strip()
                    if query:
                        # Scope RAG to the calling room. Without this, recall()
                        # falls back to its default "master_room" and leaks
                        # master_room history into every other room's prompts.
                        rag_room_id = str(
                            agent.blackboard.get_state_value("room_id", "") or ""
                        ).strip() or "master_room"
                        matches = recall(query, top_k=6, room_id=rag_room_id)
                        context[key] = format_recall_for_prompt(matches, max_chars=1200)
                    else:
                        context[key] = ""
                except Exception as e:
                    logger.debug("[%s] chat_memory unavailable: %s", agent.name, e)
                    context[key] = ""
                continue

            if key == "location_summary":
                try:
                    loc_resource = self.resolve_resource(agent, "resource_current_location")
                    if isinstance(loc_resource, dict):
                        current = loc_resource.get("current_location") or {}
                        address = current.get("address") or {}
                        label = str(current.get("label") or "").strip()
                        # Structured fields (canonical home address uses lowercase keys)
                        city = str(address.get("city") or address.get("City") or "").strip()
                        state = str(address.get("state") or address.get("State") or "").strip()
                        country = str(address.get("country") or address.get("Country") or "").strip()
                        # Build "Home, Irvine, CA, US" — skip empty parts, skip duplicates
                        # If city already contains state (e.g. "Irvine, CA"), don't repeat state
                        if state and f", {state}" in city:
                            state = ""
                        parts = [p for p in [label, city, state, country] if p]
                        context[key] = ", ".join(parts) if parts else ""
                    else:
                        context[key] = ""
                except Exception as e:
                    logger.debug("[%s] location_summary unavailable: %s", agent.name, e)
                    context[key] = ""
                continue

            if key in {"referenced_pods", "referenced_pods_block"}:
                try:
                    pod_injector = getattr(agent.components, "pod_injector", None)
                    if pod_injector is None:
                        context[key] = [] if key == "referenced_pods" else ""
                        continue
                    headers = pod_injector.hydrate_for_context(
                        user_context=context,
                        message=message,
                    )
                    if key == "referenced_pods":
                        context[key] = [h.model_dump(mode="json") for h in headers]
                    else:
                        context[key] = pod_injector.format_block(headers)
                except Exception as e:
                    logger.error("[%s] Failed hydrating pod references for '%s': %s", agent.name, key, e)
                    logger.debug("[%s] pod hydration exception details", agent.name, exc_info=True)
                    raise
                continue

            if key == "recent_dayflow_tickets":
                try:
                    from app.assistant.ticket_manager import get_ticket_manager, TicketState, TicketType
                    from app.assistant.pipelines.dayflow.utils.context_sources import _format_ticket_for_context

                    _DAYFLOW_TICKET_TYPES = [
                        TicketType.DAYFLOW_ADVICE.value,
                        TicketType.DAYFLOW_NOTIFY.value,
                        TicketType.DAYFLOW_DECISION.value,
                    ]
                    raw = get_ticket_manager().get_tickets(
                        states=[TicketState.PENDING, TicketState.PROPOSED, TicketState.ACCEPTED,
                                TicketState.DISMISSED, TicketState.SNOOZED],
                        limit=30,
                    )
                    context[key] = [
                        _format_ticket_for_context(t)
                        for t in raw
                        if getattr(t, "ticket_type", "") in _DAYFLOW_TICKET_TYPES
                    ]
                except Exception as e:
                    logger.error("[%s] Failed loading recent_dayflow_tickets: %s", agent.name, e)
                    logger.debug("[%s] recent_dayflow_tickets exception details", agent.name, exc_info=True)
                    context[key] = []
                continue

            if key == "recent_dayflow_items":
                try:
                    from app.assistant.dayflow_orchestrator.state_store import get_dayflow_items

                    _NOISE_STATES = frozenset({"new", "artifact", "needs_planning"})
                    _NOISE_SOURCES = frozenset({"chat", "calendar", "email"})
                    items = get_dayflow_items()

                    plan_synopses: list[dict] = []
                    task_rows: list[dict] = []

                    for m in items:
                        if not isinstance(m, dict):
                            continue
                        meta = m.get("metadata") if isinstance(m.get("metadata"), dict) else {}
                        source_type = str(meta.get("source_type") or "").strip().lower()
                        event_type = str(meta.get("event_type") or "").strip().lower()
                        state_raw = str(meta.get("state") or "").strip().lower()

                        if source_type == "chat" or event_type == "cross_room_chat":
                            continue
                        if state_raw in _NOISE_STATES:
                            continue
                        if source_type in _NOISE_SOURCES:
                            continue

                        summary = str(meta.get("summary") or m.get("content") or "").strip()
                        if not summary:
                            continue

                        if source_type == "plan_synopsis":
                            if str(meta.get("plan_status") or "").strip().lower() == "completed":
                                continue
                            plan_synopses.append({
                                "plan_id": str(meta.get("plan_id") or "").strip(),
                                "objective": str(meta.get("objective") or summary).strip(),
                            })
                            continue

                        importance = str(meta.get("importance") or "").strip()
                        readable = summary
                        if source_type:
                            readable = f"{readable} - {source_type}"
                        if importance and importance != "medium":
                            readable = f"{readable} ({importance})"

                        task_rows.append({
                            "summary": summary,
                            "readable": readable,
                            "state_raw": state_raw,
                            "state": state_raw.replace("_", " ") if state_raw else "",
                            "source_type": source_type,
                            "plan_id": str(meta.get("plan_id") or "").strip(),
                        })

                    context[key] = {
                        "plans": plan_synopses[:15],
                        "tasks": task_rows[:20],
                    }
                except Exception as e:
                    logger.error("[%s] Failed loading recent_dayflow_items: %s", agent.name, e)
                    logger.debug("[%s] recent_dayflow_items exception details", agent.name, exc_info=True)
                    context[key] = {"plans": [], "tasks": []}
                continue

            if key == "context_activation_memo":
                try:
                    from app.assistant.context_engine.context_memo import get_active_memo
                    memo_msg = get_active_memo()
                    if memo_msg is not None:
                        memo_text = (getattr(memo_msg, "content", None) or "").strip()
                        if memo_text:
                            context[key] = f"[Background context Emi prepared]\n{memo_text}"
                        else:
                            context[key] = ""
                    else:
                        context[key] = ""
                except Exception as e:
                    logger.error("[%s] Failed loading context_activation_memo: %s", agent.name, e)
                    logger.debug("[%s] context_activation_memo load exception details", agent.name, exc_info=True)
                    context[key] = ""
                continue

            if key.startswith("geoguessr_"):
                try:
                    from app.assistant.ServiceLocator.service_locator import DI
                    from app.assistant.room_session_manager.services.geoguessr_session_service import GeoguessrSessionService
                    scope = getattr(message, "scope_context", None) if message is not None else None
                    if scope is None:
                        scope = agent.blackboard.get_state_value("scope_context", None)
                    room_id = str(getattr(scope, "room_id", None) or agent.blackboard.get_state_value("room_id", "") or "").strip()
                    surface = str(getattr(scope, "surface", None) or agent.blackboard.get_state_value("room_surface", "ui") or "ui").strip()
                    context_id = str(getattr(scope, "room_context_id", None) or agent.blackboard.get_state_value("room_context_id", "main") or "main").strip()
                    geo_svc = GeoguessrSessionService(blackboard=DI.global_blackboard)
                    session = geo_svc.get_active_room_binding(room_id=room_id, surface=surface, context_id=context_id) if room_id else None
                    if session:
                        _geo_map = {
                            "geoguessr_clue_history": session.get("clue_log") or [],
                            "geoguessr_best_guess": session.get("best_guess") or "",
                            "geoguessr_confidence": session.get("confidence") or 0,
                            "geoguessr_screenshot_count": len(session.get("screenshot_paths") or []),
                            "geoguessr_answer_revealed": bool(session.get("answer_revealed")),
                        }
                        context[key] = _geo_map.get(key)
                    else:
                        context[key] = agent.blackboard.get_state_value(key, None)
                except Exception as e:
                    logger.error("[%s] Failed resolving geoguessr context key '%s': %s", agent.name, key, e)
                    logger.debug("[%s] geoguessr context key exception details", agent.name, exc_info=True)
                    raise
                continue

            context[key] = agent.blackboard.get_state_value(key, None)

        # --- task_keyword_resources (per-agent config) ---
        # config.yaml declares: task_keyword_resources: {doordash: resource_doordash_guidelines, ...}
        # Matches against task AND incoming_message.
        try:
            keyword_map = agent.config.get("task_keyword_resources") if hasattr(agent, "config") else None
            if isinstance(keyword_map, dict) and keyword_map:
                search_text = " ".join(
                    str(v or "")
                    for v in (
                        agent.blackboard.get_state_value("task", ""),
                        context.get("incoming_message", ""),
                    )
                ).lower()
                for keyword, resource_id in keyword_map.items():
                    if not isinstance(keyword, str) or not isinstance(resource_id, str):
                        continue
                    if keyword.lower() in search_text and resource_id not in context:
                        try:
                            context[resource_id] = self.resolve_resource(agent, resource_id)
                            logger.debug(
                                "[%s] task_keyword_resources: injected '%s' (keyword=%r matched)",
                                agent.name,
                                resource_id,
                                keyword,
                            )
                        except PermissionError:
                            logger.info(
                                "[%s] task_keyword_resources: '%s' blocked by scope policy (keyword=%r) — skipping.",
                                agent.name,
                                resource_id,
                                keyword,
                            )
                        except Exception as e:
                            logger.warning(
                                "[%s] task_keyword_resources: failed to resolve '%s' for keyword=%r: %s — skipping.",
                                agent.name,
                                resource_id,
                                keyword,
                                e,
                            )
                            logger.debug("[%s] task_keyword_resources resolve exception details", agent.name, exc_info=True)
        except Exception as e:
            logger.error("[%s] task_keyword_resources processing failed: %s", agent.name, e)
            logger.debug("[%s] task_keyword_resources exception details", agent.name, exc_info=True)
            raise

        # --- Aggregate site_guidelines from matched keyword resources ---
        # Collects all resolved task_keyword_resources into a single template variable
        # so templates don't need to hardcode each resource name.
        try:
            keyword_map = agent.config.get("task_keyword_resources") if hasattr(agent, "config") else None
            if isinstance(keyword_map, dict) and keyword_map:
                guide_parts: list[str] = []
                for _keyword, resource_id in keyword_map.items():
                    if not isinstance(resource_id, str):
                        continue
                    value = context.get(resource_id)
                    if isinstance(value, str) and value.strip():
                        guide_parts.append(value.strip())
                    elif isinstance(value, dict):
                        content = value.get("content")
                        if isinstance(content, str) and content.strip():
                            guide_parts.append(content.strip())
                if guide_parts:
                    context["site_guidelines"] = "\n\n".join(guide_parts)
        except Exception:
            logger.debug("[%s] site_guidelines aggregation failed", agent.name, exc_info=True)

        # --- Global keyword resource injection (sidecar *.triggers.json) ---
        # Agents opt in with: enable_keyword_resource_injection: true
        # The KeywordResourceIndex scans all resource_*.triggers.json files at startup.
        try:
            if agent.config.get("enable_keyword_resource_injection"):
                from app.assistant.agent_runtime.services.keyword_resource_index import KeywordResourceIndex
                scan_keys = agent.config.get("keyword_scan_context_keys") or ["task", "incoming_message"]
                if not isinstance(scan_keys, list):
                    raise ValueError(
                        f"[{agent.name}] keyword_scan_context_keys must be a list when provided."
                    )
                scan_parts: list[str] = []
                for sk in scan_keys:
                    if not isinstance(sk, str) or not sk.strip():
                        continue
                    if sk == "task":
                        scan_parts.append(str(agent.blackboard.get_state_value("task", "") or ""))
                    elif sk in context:
                        value = context.get(sk)
                        if isinstance(value, str):
                            scan_parts.append(value)
                    else:
                        bb_val = agent.blackboard.get_state_value(sk, "")
                        if isinstance(bb_val, str):
                            scan_parts.append(bb_val)
                search_text = " ".join(scan_parts)
                matches = KeywordResourceIndex.get_instance().match(search_text, agent_name=agent.name)
                injected_keyword_resources = []
                for resource_id, label in matches:
                    if resource_id in context:
                        continue
                    try:
                        value = self.resolve_resource(agent, resource_id)
                        if value is not None and value != "":
                            context[resource_id] = value
                            injected_keyword_resources.append({
                                "resource_id": resource_id,
                                "label": label,
                                "content": value,
                            })
                            logger.debug(
                                "[%s] keyword_resource_injection: injected '%s' (label=%r)",
                                agent.name, resource_id, label,
                            )
                    except Exception as e:
                        logger.error(
                            "[%s] keyword_resource_injection: failed to resolve '%s': %s",
                            agent.name, resource_id, e,
                        )
                        logger.debug("[%s] keyword_resource_injection resolve exception details", agent.name, exc_info=True)
                        raise
                if injected_keyword_resources:
                    context["_keyword_injected_resources"] = injected_keyword_resources
        except Exception as e:
            logger.error("[%s] keyword_resource_injection processing failed: %s", agent.name, e)
            logger.debug("[%s] keyword_resource_injection exception details", agent.name, exc_info=True)
            raise

        if not entity_keys:
            return context

        for key in entity_keys:
            context.setdefault(key, "")
        if len(entity_field_keys) > 0:
            context.setdefault("entity_info", "")

        return context

