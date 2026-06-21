"""Pre-LLM prep for the event_waker (Increment 3).

Finds the work-object nodes that are parked on an EXTERNAL EVENT and genuinely waiting on it NOW —
wake_kind is event/user_reply/signal AND is_ready is True (deps + time already satisfied, so the only
thing left is the event). Projects each as a task the waker reasons over, and gathers recent incoming
intake to match against. If nothing is parked on an event, it skips the waker + its persist (routes
straight to work_execution_node) so no LLM call is spent.

Inert until the dayflow manager's state_map routes to it.
"""
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_EVENT_WAKES = {"event", "user_reply", "signal"}
_TERMINAL_WO_STATES = {"done", "abandoned"}


class EventWakerPrepNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        waiting = []
        try:
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            from work_objects.model import utcnow
            store = get_dayflow_work_store()
            now = utcnow()
            for s in store.list_work_objects():
                if str(s.get("status") or "").lower() in _TERMINAL_WO_STATES:
                    continue
                wo = store.load(s["id"])
                for n in wo.nodes.values():
                    if getattr(n, "wake_kind", None) in _EVENT_WAKES and wo.is_ready(n, now):
                        waiting.append({
                            "task_id": f"{s['id']}::{n.id}",
                            "waiting_for": getattr(n, "wake_ref", "") or "",
                            "context": (n.title or n.content or "").strip().replace("\n", " ")[:140],
                        })
        except Exception as e:
            logger.error("[%s] prep failed: %s", self.name, e)
            logger.debug("[%s] prep exception", self.name, exc_info=True)
            waiting = []

        if not waiting:
            self.blackboard.update_state_value("waiting_tasks", [])
            self.blackboard.update_state_value("next_agent", "work_execution_node")  # skip the waker
            self.blackboard.update_state_value("last_agent", self.name)
            return

        self.blackboard.update_state_value("waiting_tasks", waiting)
        self.blackboard.update_state_value("recent_intake", self._recent_intake())
        logger.info("[%s] %d task(s) parked on an event", self.name, len(waiting))
        self.blackboard.update_state_value("last_agent", self.name)

    def _recent_intake(self):
        out = []
        try:
            from app.assistant.dayflow_orchestrator.state_store import get_dayflow_items
            items = get_dayflow_items() or []
        except Exception as e:
            logger.warning("[%s] could not load intake: %s", self.name, e)
            return out

        def _ts(it):
            m = it.get("metadata", {}) or {}
            return str(m.get("created_at") or it.get("created_at") or "")

        try:
            items = sorted(items, key=_ts, reverse=True)
        except Exception:
            pass

        for it in items[:30]:
            m = it.get("metadata", {}) or {}
            when = m.get("created_at_local") or ""
            pre = f"[{when}] " if when else ""
            if str(m.get("source_type") or "") == "email":
                summ = m.get("email_summary", "")
                out.append(f"{pre}Email from {m.get('email_sender', 'Unknown')}: "
                           f"\"{m.get('email_subject', m.get('summary', ''))}\"" + (f" — {summ}" if summ else ""))
            else:
                summ = m.get("summary", "")
                if summ:
                    out.append(f"{pre}{summ} ({m.get('source_type', '')})")
        return out
