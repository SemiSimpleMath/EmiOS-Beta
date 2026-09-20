"""work_node_materializer_node — build the actionable list from READY work-object nodes.

The work-object analogue of view_materializer (which built the list from dayflow items). Each ready node
becomes ONE actionable item (item_id = "work_id::node_id", summary = the node's task) — exactly the shape
action_selector consumes. The switchboard reads the ONE picked node and routes it
(communicate-with-the-user -> create_dayflow_ticket, work -> work_emi_team_manager); one dispatch per tick, and
the dispatch signals a prompt follow-up tick when more ready nodes remain.

Lists only architect-authored direct task children of the goal that are actionable:
worker-owned provenance is excluded, including old records marked actionable. Tasks include plain work, tell-the-user goals, and asks due for
their first surface or a re-ask. Everything else stays out — the goal node, event/signal waits (the
state_mover wakes those), and in-flight nodes (future wake_at, e.g. an ask surfaced within the hour).

This node BUILDS THE LIST and nothing else. It used to carry a reply pre-step that scanned the ticket
store for responses to in-flight asks; landing a tool result was never this node's job and doing it here
put it seven stops into the tick, behind the steward. A ticket response is now recorded by the ticket
dispatch itself: the ask runs as a session whose tool call returns the user's response.
"""
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_TERMINAL_WO_STATES = {"done", "abandoned"}


class WorkNodeMaterializerNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        items = []
        try:
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            from work_objects.model import utcnow
            store = get_dayflow_work_store()
            now = utcnow()
            for s in store.list_work_objects():
                if str(s.get("status") or "").lower() in _TERMINAL_WO_STATES:
                    continue
                try:
                    wo = store.load(s["id"])
                except Exception as e:
                    logger.warning("[%s] work object %s not loadable: %s", self.name, s.get("id"), e)
                    continue
                items.extend(self._node_items(wo, now))
        except Exception as e:
            logger.error("[%s] work-node materialize failed: %s", self.name, e)
            logger.debug("[%s] materialize exception", self.name, exc_info=True)

        self.blackboard.update_state_value("actionable_items", items)
        # Mirror view_materializer's empty short-circuit: nothing ready -> skip action_selector.
        if not items:
            logger.info("[%s] no ready work nodes — skipping action_selector.", self.name)
            self.blackboard.update_state_value("next_agent", "post_room_finalize_node")
        logger.info("[%s] materialized %d ready work node(s)", self.name, len(items))
        self.blackboard.update_state_value("last_agent", self.name)

    @staticmethod
    def _node_items(wo, now):
        """The ACTIONABLE nodes of ONE work object, as action_selector items. A node is dispatchable ONLY
        once state_mover has promoted it (proposed/waiting -> actionable); a node still in proposed/waiting
        sits parked in the architect's inbox and is NOT listed here. (External-event nodes are never promoted
        — the state_mover wakes them via node_wakes — so they never reach actionable.)"""
        out = []
        for n in wo.nodes.values():
            if not wo.is_work_unit(n):
                continue
            if n.status != "actionable" or not wo.is_ready(n, now) or n.wake_kind in {"event", "signal"}:
                continue
            task = (f"{n.title}. {n.content}" if n.content else (n.title or "")).strip()
            out.append({
                "item_id": f"{wo.id}::{n.id}",
                "short_id": f"{wo.id}::{n.id}",
                "summary": task,
                "source_type": "work_node",
                "state": "actionable",
                "importance": "medium",
                "actionability": "actionable",
            })
        return out

