from app.assistant.event_hub.event_hub import EventHub
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class ProgressEmitter:
    """
    Emits high-signal planner progress facts for UI.
    """

    def __init__(self, *, event_hub: EventHub) -> None:
        self._event_hub = event_hub

    def emit_planner_decision(self, agent, result_dict: dict) -> None:
        if not (
            str(agent.name).endswith("::planner")
            or str(agent.name).endswith(":planner")
            or str(agent.name).endswith("_planner")
        ):
            return

        task = agent.blackboard.get_state_value("task")

        # The instance .name is suffixed with a UUID when invoked through
        # manager_interface (e.g. "web_manager_55d613a4"). For consumers
        # like display-name lookup we want the canonical TYPE ("web_manager")
        # — that's what's keyed in the manager_registry and the display-name
        # registry. Prefer manager_config["name"] which is the type from
        # the YAML; fall back to instance .name with the suffix stripped.
        manager_name = ""
        try:
            parent = agent.parent
            if parent is not None:
                cfg = getattr(parent, "manager_config", None)
                if isinstance(cfg, dict):
                    cfg_name = cfg.get("name")
                    if isinstance(cfg_name, str) and cfg_name.strip():
                        manager_name = cfg_name.strip()
                if not manager_name:
                    raw = getattr(parent, "name", "") or ""
                    # Strip the manager_interface invocation suffix
                    # ``_<8 hex chars>`` if present.
                    import re as _re
                    manager_name = _re.sub(r"_[0-9a-f]{8}$", "", str(raw))
        except Exception:
            logger.debug("Could not derive canonical manager name", exc_info=True)
            manager_name = ""

        # what_i_am_thinking is the planner's own one-line description of
        # what it's doing this step — the natural narration source. Far
        # better than narrating tool names ("running search_web") because
        # the planner already had to articulate the goal to fill the field.
        thinking = result_dict.get("what_i_am_thinking")
        if not isinstance(thinking, str):
            thinking = ""
        thinking = thinking.strip()

        # Carry the originating surface's reply_to forward so the chat
        # narrator (and any other downstream consumer) can route output
        # back to the right surface — UI socket, slack channel, sms, etc.
        # Source: scope_context.reply_to populated at ingress and
        # propagated through chains via manager_interface.
        reply_to = None
        try:
            scope = agent.blackboard.get_state_value("scope_context")
            if isinstance(scope, dict):
                rt = scope.get("reply_to")
                if isinstance(rt, dict) and rt:
                    reply_to = dict(rt)
        except Exception:
            reply_to = None

        # Look up the per-instance display_name (e.g. "Webby_2") and base
        # name from MAMInstanceManager via the blackboard's invocation_id.
        # ChatNarrator uses base for the named/unnamed gate and display
        # for the chat sender. If the lookup fails, downstream falls back
        # to display_name_for(manager_name) which is fine.
        instance_display_name = None
        base_display_name = None
        try:
            invocation_id = agent.blackboard.get_state_value("_invocation_id")
            if isinstance(invocation_id, str) and invocation_id.strip():
                from app.assistant.ServiceLocator.service_locator import DI
                mam = getattr(DI, "mam_instance_manager", None)
                if mam is not None:
                    record = mam.find_by_invocation_id(invocation_id)
                    if record is not None:
                        instance_display_name = record.display_name
                        base_display_name = record.base_display_name
        except Exception:
            logger.debug("Could not look up MAM record for progress fact", exc_info=True)

        fact = {
            "kind": "planner_decision",
            "agent": agent.name,
            "manager": manager_name,
            "base_display_name": base_display_name,
            "display_name": instance_display_name,
            "task": task,
            "action": result_dict.get("action"),
            "action_input": result_dict.get("action_input"),
            "what_i_am_thinking": thinking,
            "reply_to": reply_to,
            "learned": (result_dict.get("summary") or "").strip() if isinstance(result_dict.get("summary"), str) else "",
            "action_count": agent.blackboard.get_state_value("action_count"),
        }
        self._event_hub.publish(
            Message(
                sender=agent.name,
                receiver=None,
                event_topic="agent_progress_fact",
                data=fact,
            )
        )
