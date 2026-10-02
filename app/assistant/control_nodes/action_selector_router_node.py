"""Control node: hand the node the action_selector picked to the switchboard, as the architect wrote it.

The action_selector PICKS: one item id off the actionable list, or no-op. It writes nothing for the
switchboard. This node loads the picked node and stages its own title and goal (shared with the wake
pass's router, node_dispatch.stage_for_switchboard), so the switchboard routes on the node's record.

Until 2026-10-02 the selector wrote a `switchboard_task` of its own (a form inherited from the chat
gate via ChatTaskRouterNode) and the switchboard routed on that restatement. The Seija send node
("If [the owner] approved the exact draft, email Seija ...") was restated as "Ask [the owner] whether
he authorizes ...", routed to a ticket, and the approved email went out only on the retry. The
inherited router also posted the chat gate's "On it." acknowledgment to the owner's chat on every
dispatch.
"""
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_SELECTOR = "dayflow_orchestrator::action_selector"


class ActionSelectorRouterNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        last_agent = self.blackboard.get_state_value("last_agent", None)
        if last_agent != _SELECTOR:
            raise ValueError(f"[{self.name}] expected last_agent={_SELECTOR}, got {last_agent!r}")

        picked = [str(i) for i in (self.blackboard.get_state_value("acted_on_item_ids", []) or [])]
        if self.blackboard.get_state_value("no_op_tf", False):
            if picked:
                raise ValueError(f"[{self.name}] the selector chose no-op and picked {picked}")
            self.blackboard.update_state_value("result", {
                "final_answer_task": str(self.blackboard.get_state_value("task", "") or ""),
                "final_answer_answer": "",
                "final_answer_no_op": True,
                "final_answer_what_was_done": "No-op at action selector.",
                "final_answer_interesting_info": "",
                "final_answer_sources": [],
            })
            self.blackboard.update_state_value("next_agent", "final_answer_node")
            self.blackboard.update_state_value("last_agent", self.name)
            return

        if len(picked) != 1:
            raise ValueError(f"[{self.name}] the selector must pick exactly one item, got {picked}")
        from app.assistant.dayflow_orchestrator.node_dispatch import canonical_pick, stage_for_switchboard
        ref = canonical_pick(picked[0], self.blackboard.get_state_value("actionable_items", []) or [])
        if "::" not in ref:
            raise ValueError(f"[{self.name}] the selector picked {ref!r}, which is not a work node")
        work_id, _, node_id = ref.partition("::")
        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        node = get_dayflow_work_store().load(work_id).nodes.get(node_id)
        if node is None:
            raise ValueError(f"[{self.name}] the selector picked {ref}, which the work store does not have")

        stage_for_switchboard(self.blackboard, ref, node)
        self.blackboard.update_state_value("result", None)
        self.blackboard.update_state_value("next_agent", "dayflow_orchestrator::switchboard")
        logger.info("[%s] %s picked — to the switchboard as written: %s", self.name, ref, node.title or node.content)
        self.blackboard.update_state_value("last_agent", self.name)
