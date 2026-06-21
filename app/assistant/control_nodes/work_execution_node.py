"""Execution node for the work-object dayflow pipeline.

Runs AFTER work_architect_node has laid down each goal's DAG. Drives the graph forward NODE by node:
for every active work object it runs the READY nodes (deps satisfied, time-gate passed) one at a time
via work_on(store, work_id, node_id=...), re-evaluating after each so a chain can progress within a
tick — bounded by _MAX_NODES_PER_TICK.

It SKIPS event-waits (wake_kind in event/user_reply/signal): the substrate's is_ready does NOT gate
those (only time + dependency), so without this skip they would fire prematurely. The node-aware
state_mover owns waking them (Increment 3). Time-gates and dependencies ARE handled by the graph, so
those park/fire correctly here already. Never raises — a failure degrades to fewer nodes run this tick.

Inert until the dayflow manager's state_map routes to it.
"""
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_MAX_NODES_PER_TICK = 5
_TERMINAL_WO_STATES = {"done", "abandoned"}
_EVENT_WAKES = {"event", "user_reply", "signal"}


class WorkExecutionNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        executed = []
        try:
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            from work_objects.work_runtime import work_on
            from work_objects.model import utcnow
            store = get_dayflow_work_store()
            now = utcnow()
            active_ids = [s["id"] for s in store.list_work_objects()
                          if str(s.get("status") or "").lower() not in _TERMINAL_WO_STATES]

            done = set()   # (work_id, node_id) already run THIS tick — never re-run within a tick
            ran = 0
            while ran < _MAX_NODES_PER_TICK:
                progressed = False
                for work_id in active_ids:
                    if ran >= _MAX_NODES_PER_TICK:
                        break
                    try:
                        wo = store.load(work_id)
                    except Exception as e:
                        logger.warning("[%s] work object %s not loadable: %s", self.name, work_id, e)
                        continue
                    if str(wo.status or "").lower() in _TERMINAL_WO_STATES:
                        continue
                    node = self._next_runnable(wo, now, work_id, done)
                    if node is None:
                        continue
                    done.add((work_id, node.id))
                    try:
                        status = work_on(store, work_id, node_id=node.id)
                        executed.append({"work_id": work_id, "node": node.id, "status": status})
                        ran += 1
                        progressed = True
                        logger.info("[%s] ran node %s/%s -> %s", self.name, work_id, node.id, status)
                    except Exception as e:
                        logger.error("[%s] work_on failed for %s/%s: %s", self.name, work_id, node.id, e)
                        logger.debug("[%s] work_on exception", self.name, exc_info=True)
                        ran += 1   # count it so a failing node can't spin the loop
                if not progressed:
                    break
        except Exception as e:
            logger.error("[%s] execution node failed: %s", self.name, e)
            logger.debug("[%s] execution node exception", self.name, exc_info=True)

        self.blackboard.update_state_value("work_execution_result", executed)
        logger.info("[%s] ran %d node(s)", self.name, len(executed))
        self.blackboard.update_state_value("last_agent", self.name)

    @staticmethod
    def _next_runnable(wo, now, work_id, done):
        """The first ready node worth running: not the goal node, not already run this tick, and not an
        event-wait (those are the state_mover's to wake). Time/dependency gates are already enforced by
        wo.ready_nodes()."""
        goal_id = wo.goal_node_id
        for n in wo.ready_nodes(now):
            if n.id == goal_id:
                continue
            if (work_id, n.id) in done:
                continue
            if getattr(n, "wake_kind", None) in _EVENT_WAKES:
                continue
            return n
        return None
