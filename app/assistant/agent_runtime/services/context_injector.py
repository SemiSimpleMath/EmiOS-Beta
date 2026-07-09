from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from jinja2 import Template

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.agent_runtime.services.resource_resolver import ResourceResolver
from app.assistant.agent_runtime.services.user_bio_context_service import UserBioContextService
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import get_local_time_str

logger = get_logger(__name__)

# Cumulative injected-skill weight per prompt that earns a warning. Bodies
# ride the prompt whole and always-inject persona packs stack onto every
# downstream agent of a task — this makes the bloat visible before it becomes
# a cost bug (2026-07-07 skills audit).
_SKILLS_CHARS_WARN = 30_000


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

    def _refresh_resource_cache_entry(self, agent, resource_id: str) -> None:
        try:

            resource_manager = getattr(DI, "resource_manager", None)
            if resource_manager is None:
                logger.error("[%s] DI.resource_manager is unavailable for refresh of '%s'.", agent.name, resource_id)
                raise RuntimeError("resource_manager unavailable")
            resource_manager.refresh_resource(resource_id)
        except Exception as e:
            logger.error("[%s] Failed refreshing resource cache for '%s': %s", agent.name, resource_id, e)
            logger.debug("[%s] resource cache refresh exception details", agent.name, exc_info=True)
            raise


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

        Companion helper: ``resolve_auto_injected_skill_names`` exposes
        just the names that came from path (2)+(3)+(4) (anything not in
        ``agent.config['skills']``). Templates use it to render a
        single ``{% for name in auto_injected_skill_names %}{{ skills[name] }}{% endfor %}``
        block without per-skill explicit references — see SCOPE_AUDIT
        Step 1.5 follow-up (skill rendering gap closed by exposing the
        auto-injected names list).
        """
        resolved, _ = self._resolve_skills_with_provenance(agent)
        return resolved

    def resolve_auto_injected_skill_names(self, agent) -> List[str]:
        """Return the ordered list of skill names that came from any
        dynamic path (auto-inject / caller-supplied / scope-stamped) —
        i.e. NOT statically declared in ``agent.config['skills']``.

        Used by prompt templates to render dynamic skills generically:

            {%- for name in auto_injected_skill_names %}
            {{ skills[name] }}
            {%- endfor %}

        This closes the "auto-injection but template doesn't render"
        gap that caused Wire to discover_skills('bluesky') even though
        the bluesky skill was already loaded into the skills dict.
        """
        _, dynamic_names = self._resolve_skills_with_provenance(agent)
        return dynamic_names

    def _resolve_skills_with_provenance(self, agent) -> tuple[Dict[str, str], List[str]]:
        """Internal: build the skills dict and track which names came
        from dynamic paths (auto-inject / caller / scope) vs static
        agent.config['skills'].

        Returns ``(skills_dict, dynamic_names_in_order)``.
        """
        registry = getattr(DI, "skill_registry", None)
        if registry is None:
            logger.warning("[%s] DI.skill_registry not available — no skills loaded", agent.name)
            return {}, []

        resolved: Dict[str, str] = {}
        static_names: set[str] = set()

        # The skill GATE is universal: every path below (static, keyword, caller-
        # supplied, scope-stamped) must check that a skill's requires_scope matches
        # the live scope before admitting it. A skill with no requires_scope passes
        # trivially. Single source of truth: SkillInjector.skill_gate_passes.
        scope_ctx = agent.blackboard.get_state_value("scope_context", None)
        _gate_injector = getattr(DI, "skill_injector", None)

        def _passes_gate(skill) -> bool:
            if _gate_injector is None:
                return True  # no injector -> can't evaluate; don't block (logged once below)
            return _gate_injector.skill_gate_passes(skill, scope=scope_ctx)

        # 1. Static skills from agent config — gated like every other path.
        for name in (agent.config.get("skills", []) or []):
            if not isinstance(name, str) or not name.strip():
                continue
            skill = registry.get(name.strip())
            if skill is None:
                logger.warning(
                    "[%s] declared skill %r not registered — dropped", agent.name, name,
                )
                continue
            if not _passes_gate(skill):
                logger.debug(
                    "[%s] static skill %r blocked by requires_scope gate (scope mismatch)",
                    agent.name, skill.name,
                )
                continue
            resolved[skill.name] = skill.body
            static_names.add(skill.name)

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
                principal = None
                if scope_ctx is not None:
                    principal = (
                        scope_ctx.get("acting_as") if isinstance(scope_ctx, dict)
                        else getattr(scope_ctx, "acting_as", None)
                    )
                for name in injector.matching_skill_names(
                    task=task,
                    incoming_message=incoming,
                    scope_acting_as=principal,
                    scope=scope_ctx,
                ):
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
                if not _passes_gate(skill):
                    logger.debug(
                        "[%s] caller-supplied skill %r blocked by requires_scope gate",
                        agent.name, name,
                    )
                    continue
                resolved[name] = skill.body
                logger.debug("[%s] caller-supplied skill=%s", agent.name, name)

        # 4. Scope-stamped skills (scope_context.skills.always_inject).
        #    Set by the scope builder based on principal / mode / room / any
        #    other scope-shaping signal. Propagates through nested sub-agents
        #    because ScopeContext propagates — closes the gap where a
        #    keyword-triggered skill on the user's first message would be
        #    lost in downstream planner / tool-args / writer agents.
        #    (These names were already gate-matched when always_inject was built;
        #    re-checking here keeps the gate universal even if a list was
        #    hand-stamped without going through discovery.)
        scope_skills_policy = None
        if isinstance(scope_ctx, dict):
            scope_skills_policy = scope_ctx.get("skills")
        elif scope_ctx is not None:
            scope_skills_policy = getattr(scope_ctx, "skills", None)
        if scope_skills_policy is not None:
            def _policy_get(field: str):
                if isinstance(scope_skills_policy, dict):
                    return scope_skills_policy.get(field)
                return getattr(scope_skills_policy, field, None)
            for name in (_policy_get("always_inject") or []):
                if not isinstance(name, str) or not name.strip():
                    continue
                name = name.strip()
                if name in resolved:
                    continue
                skill = registry.get(name)
                if skill is None:
                    logger.warning(
                        "[%s] scope-stamped skill %r not registered — dropped",
                        agent.name, name,
                    )
                    continue
                if not _passes_gate(skill):
                    logger.debug(
                        "[%s] scope-stamped skill %r blocked by requires_scope gate",
                        agent.name, name,
                    )
                    continue
                resolved[name] = skill.body
                logger.debug("[%s] scope-injected skill=%s", agent.name, name)
            # denied_skills is the final filter — applies to every path above.
            for name in (_policy_get("denied_skills") or []):
                if isinstance(name, str) and name.strip() in resolved:
                    del resolved[name.strip()]
                    logger.debug("[%s] scope-denied skill=%s", agent.name, name)

        # Size visibility: surface the cumulative skill weight once it crosses
        # the threshold — which skills, how heavy — so a fat SKILL.md or a
        # stacking persona pack shows up in logs instead of only on the bill.
        total_chars = sum(len(body or "") for body in resolved.values())
        if total_chars > _SKILLS_CHARS_WARN:
            logger.warning(
                "[%s] injected skills total %d chars across %d skill(s) (warn threshold %d): %s",
                agent.name, total_chars, len(resolved), _SKILLS_CHARS_WARN,
                sorted(resolved.keys()),
            )

        # Compute dynamic-names list — preserves insertion order from the
        # resolved dict (Python 3.7+), filters out static-config skills.
        dynamic_names = [n for n in resolved.keys() if n not in static_names]
        return resolved, dynamic_names

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

        # Resolve skills once and expose both shapes:
        #   - ``skills`` (dict by name) — for explicit references like
        #     ``{{ skills["critic-handling"] }}``.
        #   - ``auto_injected_skill_names`` (list, insertion-ordered) —
        #     names from any dynamic path (auto-inject / caller-supplied /
        #     scope-stamped). Templates render with one block:
        #         {%- for name in auto_injected_skill_names %}
        #         {{ skills[name] }}
        #         {%- endfor %}
        #     Without this list, auto-injected skills are loaded into
        #     ``skills`` but never rendered unless the template explicitly
        #     names them — the bug that made Wire call discover_skills
        #     for the already-loaded bluesky skill.
        skills_dict, auto_injected_skill_names = self._resolve_skills_with_provenance(agent)
        context: Dict[str, Any] = {
            "date_time": get_local_time_str(),
            "day_of_week": datetime.now().strftime("%A"),
            "action_count": agent.blackboard.get_state_value(f"{agent.name}_action_count", 0),
            "room_contact_name": str(agent.blackboard.get_state_value("room_contact_name", "") or "").strip(),
            "current_speaker_name": str(agent.blackboard.get_state_value("current_speaker_name", "") or "").strip(),
            "skills": skills_dict,
            "auto_injected_skill_names": auto_injected_skill_names,
        }

        if message is not None:
            incoming_value = (
                    getattr(message, "agent_input", None)
                    or getattr(message, "content", None)
                    or getattr(message, "task", None)
            )
            if isinstance(incoming_value, str) and incoming_value.strip():
                context["incoming_message"] = incoming_value.strip()

            # `agent_input` as a declared context item resolves to the
            # inbound message's agent_input VERBATIM. Without this, the
            # generic blackboard lookup at the end of the key loop
            # resolves it to None for dict inputs — AgentInputApplier
            # SPREADS dict agent_input onto the blackboard as individual
            # keys, so there is no "agent_input" blackboard entry — and
            # every template using {{ agent_input.field }} silently
            # rendered blank (the succession_judge / date_gap_gate /
            # wiki-fact / answer_matcher judging-empty-input bug,
            # found 2026-06-12).
            if getattr(message, "agent_input", None) is not None:
                context["agent_input"] = message.agent_input

            # Same class, older convention: direct Agent invocations carry
            # their payload in Message(task=..., information=...). Nothing
            # copies those fields to the blackboard, so the generic lookup
            # below resolved them to None and the template rendered blank —
            # context_engine::chat_scan judged "User message: " since it
            # shipped (caught by the skeleton guard, 2026-06-12). The
            # message fields FILL only when the blackboard has nothing:
            # manager flows (whose control nodes own the blackboard task)
            # keep their precedence.
            for _fld in ("task", "information"):
                _v = getattr(message, _fld, None)
                if isinstance(_v, str) and _v.strip():
                    _bb = agent.blackboard.get_state_value(_fld, None)
                    if _bb is None or (isinstance(_bb, str) and not _bb.strip()):
                        context[_fld] = _v

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
                    # Audited 2026-06-12 (prompt guards): deliberate
                    # optional-decoration degrade — bio context enriches chat
                    # tone; declare it in required_context_items where a
                    # judgment genuinely depends on it.
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
                #
                # The ask ANCHOR is this turn's inbound message row id
                # (threaded onto the blackboard by the room session
                # manager). Answer capture resolves the asked ROOM from
                # it — without an anchor the question is never judged
                # against chat and can only expire.
                try:
                    from app.assistant.pending_questions import (
                        pick_question_for_nudge,
                    )
                    anchor = str(
                        agent.blackboard.get_state_value("inbound_message_id", "") or ""
                    ).strip() or None
                    picked = pick_question_for_nudge(
                        topic_tag=None, asked_in_message_id=anchor,
                    )
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

