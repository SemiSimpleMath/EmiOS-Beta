import queue
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.assistant.runtime import start_monitored_thread

logger = get_logger(__name__)


class ProgressCurator:
    """
    Curates low-level progress "facts" into UI-friendly progress cards.

    This is intentionally lightweight and runs on its own thread:
    - EventHub handlers only enqueue (never block).
    - The curator thread formats + emits to UI via a separate event topic.
    """

    FACT_TOPIC = "agent_progress_fact"
    EMIT_TOPIC = "agent_progress_emit"

    def __init__(self, *, max_queue: int = 1000):
        self._q: "queue.Queue[dict]" = queue.Queue(maxsize=max_queue)
        self._thread = start_monitored_thread(
            owner="progress_curator",
            name="progress-curator-loop",
            target=self._run,
            daemon=True,
            kind="service_loop",
            metadata={"component": "progress_curator"},
        )

        DI.event_hub.register_event(self.FACT_TOPIC, self._on_fact)
        logger.info("✅ ProgressCurator initialized (subscribed to agent_progress_fact).")

    def _on_fact(self, message: Message) -> None:
        """EventHub handler: enqueue only."""
        try:
            payload = message.data if isinstance(message.data, dict) else {}
            if not payload:
                return
            try:
                self._q.put_nowait(payload)
            except queue.Full:
                # Drop oldest behavior: drain one item and enqueue new.
                try:
                    _ = self._q.get_nowait()
                except Exception:
                    pass
                try:
                    self._q.put_nowait(payload)
                except Exception:
                    return
        except Exception:
            logger.debug("ProgressCurator enqueue failed", exc_info=True)

    def _run(self) -> None:
        while True:
            try:
                fact = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                card = self._curate_fact_to_card(fact)
                if card:
                    DI.event_hub.publish(
                        Message(
                            sender="progress_curator",
                            receiver=None,
                            event_topic=self.EMIT_TOPIC,
                            data=card,
                            timestamp=datetime.now(timezone.utc),
                        )
                    )
            except Exception:
                logger.warning("ProgressCurator failed to curate/emit", exc_info=True)

    def _curate_fact_to_card(self, fact: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Convert facts into a stable UI card.

        ``what_i_am_thinking`` (planner's own one-line self-narration) is
        passed through unchanged — it's the highest-signal narration source
        we have. The chat narrator picks it up for in-chat updates;
        consumers that don't want it just don't read the field.
        """
        kind = str(fact.get("kind") or "unknown")
        agent = str(fact.get("agent") or fact.get("sender") or "")
        manager = str(fact.get("manager") or "")
        task = str(fact.get("task") or "")

        # Hide unhelpful control-node chatter (this is the #1 "not exciting" failure mode).
        _boring_actions = {
            "manager_exit_node",
            "return_control",
            "graceful_exit_control_node",
        }
        if kind in {"tool_call", "planner_decision"}:
            a = str(fact.get("next_action") or fact.get("action") or "").strip()
            if a in _boring_actions or a.endswith("_exit_node"):
                return None

        # Evidence/learning
        learned = fact.get("learned")
        if isinstance(learned, str):
            learned_items = [learned.strip()] if learned.strip() else []
        elif isinstance(learned, list):
            learned_items = [str(x).strip() for x in learned if str(x).strip()]
        else:
            learned_items = []

        # Next action
        next_action = str(fact.get("next_action") or fact.get("action") or "")
        next_action_input = fact.get("action_input")
        if isinstance(next_action_input, str) and len(next_action_input) > 240:
            next_action_input = next_action_input[:240] + "..."

        # Basic card format (demo-style)
        goal = str((fact.get("goal") or task or "")).strip()
        if goal:
            goal = " ".join(goal.split())
            if len(goal) > 160:
                goal = goal[:160] + "..."

        # For tool results, try to provide one clean learned line.
        if kind == "tool_result" and not learned_items:
            tool = str(fact.get("tool") or "")
            preview = str(fact.get("preview") or "").strip()
            if preview:
                learned_items = [preview]
            elif tool:
                learned_items = [f"Tool finished: {tool}"]

        # Headline: keep it short and UI-friendly.
        headline = ""
        if kind == "planner_decision":
            headline = "Planner chose next step"
        elif kind == "tool_call":
            headline = "Calling tool"
        elif kind == "tool_result":
            headline = "Tool result"
        else:
            headline = kind

        # Normalize learned: drop empty + cap to 5 lines.
        learned_items = [x for x in learned_items if isinstance(x, str) and x.strip()][:5]

        # If we have nothing meaningful, skip.
        if not goal and not learned_items and not next_action:
            return None

        now = datetime.now(timezone.utc).isoformat()
        what_i_am_thinking = str(fact.get("what_i_am_thinking") or "").strip()

        return {
            "ts": now,
            "kind": kind,
            "manager": manager,
            "agent": agent,
            "headline": headline,
            "goal": goal,
            "what_i_am_thinking": what_i_am_thinking,
            "learned": learned_items,
            "next": {"action": next_action, "input_preview": next_action_input},
            "meta": {
                "tool": fact.get("tool"),
                "tool_result_id": fact.get("tool_result_id"),
                "result_type": fact.get("result_type"),
                "action_count": fact.get("action_count"),
            },
        }

