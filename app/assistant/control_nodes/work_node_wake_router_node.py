"""Control node: after the state_mover, decide whether a precisely-woken node actually fires.

Lives in dayflow_wake_manager (work_node_wake_prep_node -> state_mover -> here). The state_mover
has just made the one judgment that pass exists for: is now a good moment, or should this be held
for quiet hours / a meeting / the user being away? This node reads the answer off the graph and
either dispatches the node or lets the pass end.

  still `actionable`  -> stage it and go to the switchboard, the same dispatch every node gets
  held (`waiting`)    -> the pass ends; the hold's reactivate_at re-arms the wake on its own
  gone / ended        -> the pass ends

Until 2026-09-18 it sat inside the orchestrator's state_map and fell through to the materializer
on a normal tick; the wake pass is its own manager now and a missing trigger here is an error.
"""
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class WorkNodeWakeRouterNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        ref = str(self.blackboard.get_state_value("triggered_work_node", "") or "").strip()
        if "::" not in ref:
            raise ValueError(f"[{self.name}] no triggered_work_node on the blackboard — this node "
                             f"only runs inside a wake pass")

        # Consume it: this pass is the only one that acts on the wake.
        self.blackboard.update_state_value("triggered_work_node", "")
        work_id, _, node_id = ref.partition("::")
        try:
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            node = get_dayflow_work_store().load(work_id).nodes.get(node_id)
        except Exception as e:
            logger.error("[%s] could not read %s after the state_mover: %s", self.name, ref, e)
            node = None

        if node is None or node.status != "actionable":
            status = node.status if node is not None else "missing"
            logger.info("[%s] %s not dispatching this pass (status=%s) — the state_mover held it, "
                        "or it ended; its wake re-arms if it was held.", self.name, ref, status)
            # Straight to the room's tail. The finalizer no longer runs in this room — it runs
            # in the dispatch room that made the call — so a pass which dispatched nothing has
            # nothing here to judge and simply ends.
            self.blackboard.update_state_value("next_agent", "post_room_finalize_node")
            self.blackboard.update_state_value("last_agent", self.name)
            return

        # THE SWITCHBOARD READS `task` + `information` (its user_context_items) and routes on them.
        # On a normal tick the materializer -> action_selector -> action_selector_router_node chain
        # fills those; this pass skips all three, so it must fill them itself. It did not, and the
        # switchboard was handed an EMPTY task: with nothing but the clock in its prompt it routed
        # on the clock — "UI notification showing the current time" -> create_dayflow_ticket.
        # Every time-waked node went that way, which is nearly every scheduled reminder and device
        # action: on 2026-09-16 the 21:00 "set the AC to 70F" and the 22:00 "turn off the
        # whole-house lights" were both ticketed back to the user instead of being done.
        goal = (node.content or "").strip()
        title = (node.title or "").strip()
        if not goal and not title:
            # Routing on an empty goal is how this broke. Refuse rather than guess.
            raise ValueError(
                f"[{self.name}] {ref} has neither title nor content — the switchboard cannot route "
                f"a node with no goal")
        self.blackboard.update_state_value("task", title or goal)
        self.blackboard.update_state_value("information", goal)
        self.blackboard.update_state_value("acted_on_item_ids", [ref])
        self.blackboard.update_state_value("actionable_items", [{"item_id": ref}])
        self.blackboard.update_state_value("next_agent", "dayflow_orchestrator::switchboard")
        logger.info("[%s] %s cleared the moment check — dispatching: %s",
                    self.name, ref, title or goal)
        self.blackboard.update_state_value("last_agent", self.name)
