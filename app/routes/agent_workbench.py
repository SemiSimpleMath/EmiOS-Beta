"""
Agent Workbench — interactive prompt testing and debugging.

Endpoints:
  GET  /debug/agent-workbench           → HTML page
  GET  /debug/agent-workbench/agents    → list all agents with metadata
  POST /debug/agent-workbench/resolve   → resolve prompts for an agent with given context
  POST /debug/agent-workbench/run       → run the agent and return structured output
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List

from flask import Blueprint, jsonify, render_template, request

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)

agent_workbench_bp = Blueprint("agent_workbench", __name__)


def _list_agents() -> List[Dict[str, Any]]:
    """Return a list of all registered agents with their config metadata."""
    try:
        registry = DI.agent_factory.agent_registry.configs or {}
    except Exception:
        registry = {}

    agents = []
    for name in sorted(registry.keys()):
        cfg = registry.get(name, {})
        if not isinstance(cfg, dict):
            continue

        # Extract key config fields.
        llm_params = cfg.get("llm_params", {})
        user_ctx = cfg.get("user_context_items", [])
        system_ctx = cfg.get("system_context_items", [])

        agents.append({
            "name": name,
            "class_name": cfg.get("class_name", "Agent"),
            "engine": llm_params.get("engine", "") if isinstance(llm_params, dict) else "",
            "provider": llm_params.get("llm_provider", "") if isinstance(llm_params, dict) else "",
            "user_context_items": user_ctx if isinstance(user_ctx, list) else [],
            "system_context_items": system_ctx if isinstance(system_ctx, list) else [],
        })
    return agents


def _get_agent_form_fields(agent_name: str) -> List[Dict[str, str]]:
    """Return the agent's output schema fields from its agent_form."""
    try:
        factory = DI.agent_factory
        agent = factory.create_agent(agent_name)
        if agent is None:
            return []
        form_class = None
        # Check for agent_form in config.
        cfg = agent.config if hasattr(agent, "config") and isinstance(agent.config, dict) else {}
        # Try to get the Pydantic model from agent_input_form.
        try:
            form_class = DI.agent_registry.get_agent_input_form(agent_name)
        except Exception:
            pass
        if form_class is None:
            return []
        fields = []
        for field_name, field_info in form_class.model_fields.items():
            desc = ""
            if hasattr(field_info, "description") and field_info.description:
                desc = str(field_info.description)
            fields.append({
                "name": field_name,
                "type": str(field_info.annotation) if hasattr(field_info, "annotation") else "str",
                "description": desc,
                "required": field_info.is_required() if hasattr(field_info, "is_required") else True,
            })
        return fields
    except Exception as e:
        logger.debug("Failed to get agent form for %s: %s", agent_name, e)
        return []


def _resolve_prompts(
    agent_name: str,
    context_overrides: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Create an agent, resolve its prompts, return system + user text."""
    try:
        agent = DI.agent_factory.create_agent(agent_name)
        if agent is None:
            return {"error": f"Agent '{agent_name}' not found."}

        # Set up minimal blackboard state so context injection works.
        _WORKBENCH_SCOPE = "scope::workbench::test"
        agent.blackboard.update_state_value("scope_id", _WORKBENCH_SCOPE)
        agent.blackboard.update_state_value("task", "workbench test")
        agent.blackboard.update_state_value("information", "")
        # Push a scope onto the call stack so get_current_scope_id() works.
        agent.blackboard.call_stack.append(("workbench", agent_name, _WORKBENCH_SCOPE))

        # Apply context overrides to the agent's blackboard.
        if context_overrides and isinstance(context_overrides, dict):
            for key, value in context_overrides.items():
                agent.blackboard.update_state_value(key, value)

        # Build the prompt messages.
        msg = Message(
            content="workbench_test",
            agent_input="workbench_test",
            scope_id=_WORKBENCH_SCOPE,
        )
        messages = agent.construct_prompt(msg)

        system_prompt = ""
        user_prompt = ""
        for m in messages:
            if isinstance(m, dict):
                if m.get("role") == "system":
                    system_prompt = m.get("content", "")
                elif m.get("role") == "user":
                    user_prompt = m.get("content", "")

        # Get raw templates.
        raw_system = ""
        raw_user = ""
        try:
            cfg = agent.config if isinstance(agent.config, dict) else {}
            prompts = cfg.get("prompts", {})
            if isinstance(prompts, dict):
                sys_tpl = prompts.get("system")
                usr_tpl = prompts.get("user")
                if sys_tpl and hasattr(sys_tpl, "source"):
                    raw_system = sys_tpl.source
                if usr_tpl and hasattr(usr_tpl, "source"):
                    raw_user = usr_tpl.source
        except Exception:
            pass

        # Extract config (sanitized — no template objects).
        agent_config = {}
        try:
            cfg = agent.config if isinstance(agent.config, dict) else {}
            agent_config = {
                "name": cfg.get("name", agent_name),
                "class_name": cfg.get("class_name", ""),
                "llm_params": cfg.get("llm_params", {}),
                "allowed_tools": cfg.get("allowed_tools", []),
                "except_tools": cfg.get("except_tools", []),
                "user_context_items": cfg.get("user_context_items", []),
                "system_context_items": cfg.get("system_context_items", []),
                "action_required": cfg.get("action_required", True),
                "entity_scan_keys": cfg.get("entity_scan_keys", []),
            }
        except Exception:
            pass

        # Extract agent_form source code.
        agent_form_source = ""
        try:
            import inspect
            form_class = DI.agent_factory.agent_registry.get_agent_input_form(agent_name)
            if form_class is not None:
                agent_form_source = inspect.getsource(form_class)
        except Exception:
            pass

        return {
            "agent_name": agent_name,
            "system_prompt_resolved": system_prompt,
            "user_prompt_resolved": user_prompt,
            "system_prompt_raw": raw_system,
            "user_prompt_raw": raw_user,
            "agent_config": agent_config,
            "agent_form_source": agent_form_source,
        }
    except Exception as e:
        logger.error("Failed to resolve prompts for %s: %s", agent_name, e)
        logger.debug("resolve_prompts exception details", exc_info=True)
        return {"error": str(e)}


def _run_agent(
    agent_name: str,
    context_overrides: Dict[str, Any] | None = None,
    system_prompt_override: str | None = None,
    user_prompt_override: str | None = None,
) -> Dict[str, Any]:
    """Run the agent with optional prompt overrides and return the result."""
    try:
        agent = DI.agent_factory.create_agent(agent_name)
        if agent is None:
            return {"error": f"Agent '{agent_name}' not found."}

        # Set up minimal blackboard state.
        _WORKBENCH_SCOPE = "scope::workbench::test"
        agent.blackboard.update_state_value("scope_id", _WORKBENCH_SCOPE)
        agent.blackboard.update_state_value("task", "workbench test")
        agent.blackboard.update_state_value("information", "")
        agent.blackboard.call_stack.append(("workbench", agent_name, _WORKBENCH_SCOPE))

        # Apply context overrides.
        if context_overrides and isinstance(context_overrides, dict):
            for key, value in context_overrides.items():
                agent.blackboard.update_state_value(key, value)

        msg = Message(
            content="workbench_test",
            agent_input="workbench_test",
            scope_id=_WORKBENCH_SCOPE,
        )

        start = time.time()

        if system_prompt_override or user_prompt_override:
            # Build messages manually with overrides.
            messages = agent.construct_prompt(msg)
            for m in messages:
                if isinstance(m, dict):
                    if m.get("role") == "system" and system_prompt_override:
                        m["content"] = system_prompt_override
                    elif m.get("role") == "user" and user_prompt_override:
                        m["content"] = user_prompt_override

            # Run the LLM directly.
            schema = agent.config.get("structured_output") if isinstance(agent.config, dict) else None
            form_class = None
            try:
                form_class = DI.agent_registry.get_agent_input_form(agent_name)
            except Exception:
                pass

            result = agent._run_llm_with_schema(messages, schema or form_class)
        else:
            # Normal run.
            result_msg = agent.action_handler(msg)
            result = getattr(result_msg, "data", None) or {}

        elapsed = time.time() - start

        # Normalize result to dict.
        if hasattr(result, "model_dump"):
            result = result.model_dump()
        elif not isinstance(result, dict):
            result = {"raw": str(result)}

        return {
            "agent_name": agent_name,
            "result": result,
            "elapsed_seconds": round(elapsed, 2),
        }
    except Exception as e:
        logger.error("Failed to run agent %s: %s", agent_name, e)
        logger.debug("run_agent exception details", exc_info=True)
        return {"error": str(e)}


# --- Routes ---

@agent_workbench_bp.route("/debug/agent-workbench", methods=["GET"])
def agent_workbench_page():
    return render_template("agent_workbench.html")


@agent_workbench_bp.route("/debug/agent-workbench/agents", methods=["GET"])
def agent_workbench_agents():
    agents = _list_agents()
    return jsonify({"agents": agents})


@agent_workbench_bp.route("/debug/agent-workbench/form", methods=["POST"])
def agent_workbench_form():
    data = request.get_json(silent=True) or {}
    agent_name = str(data.get("agent_name") or "").strip()
    if not agent_name:
        return jsonify({"error": "agent_name required"}), 400
    fields = _get_agent_form_fields(agent_name)
    return jsonify({"agent_name": agent_name, "fields": fields})


@agent_workbench_bp.route("/debug/agent-workbench/resolve", methods=["POST"])
def agent_workbench_resolve():
    data = request.get_json(silent=True) or {}
    agent_name = str(data.get("agent_name") or "").strip()
    if not agent_name:
        return jsonify({"error": "agent_name required"}), 400
    context_overrides = data.get("context_overrides") or {}
    result = _resolve_prompts(agent_name, context_overrides)
    return jsonify(result)


@agent_workbench_bp.route("/debug/agent-workbench/run", methods=["POST"])
def agent_workbench_run():
    data = request.get_json(silent=True) or {}
    agent_name = str(data.get("agent_name") or "").strip()
    if not agent_name:
        return jsonify({"error": "agent_name required"}), 400
    context_overrides = data.get("context_overrides") or {}
    system_override = data.get("system_prompt_override")
    user_override = data.get("user_prompt_override")
    result = _run_agent(
        agent_name,
        context_overrides=context_overrides,
        system_prompt_override=system_override,
        user_prompt_override=user_override,
    )
    return jsonify(result)
