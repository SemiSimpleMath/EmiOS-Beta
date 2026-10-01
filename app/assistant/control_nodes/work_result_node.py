"""
WorkResultNode — the work manager's result, taken from the node instead of retold.

Runs when the work planner returns control. The task's result is the findings the planner recorded
on its node during THIS attempt (WorkPlanner tags each with the main task's dispatch epoch). No
agent rewrites them: until 2026-09-30 emi_team::final_answer wrote the result from the task text
and this run's tool history, which it read without the node or the planner's findings, so a retry
that called no tool got an answer invented from the task sentence alone.

A return without a finding for this attempt is sent back to the planner once, with the reason, while
its tool history is still in front of it. A second empty return ends the attempt as an error result:
"no result recorded", never a result written on the planner's behalf.
"""
from app.assistant.agent_runtime.services.final_answer_normalizer import FinalAnswerNormalizer
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)

_REPROMPTED = "work_result_reprompted"


def attempt_findings(wo, node_id: str, attempt: int) -> list:
    """This attempt's findings on the node and its checklist items, oldest first."""
    return sorted((n for n in wo.provenance_for(node_id)
                   if n.type == "evidence" and n.payload.get("finding_attempt") == attempt
                   and (n.content or "").strip()),
                  key=lambda n: n.created_at)


class WorkResultNode(ControlNode):
    def action_handler(self, message):
        from work_objects.runtime import get_work_context
        ctx = get_work_context()
        attempt = ctx.owner.dispatch_epoch
        findings = attempt_findings(ctx.store.load(ctx.work_id), ctx.node_id, attempt)
        self.blackboard.update_state_value("last_agent", self.name)

        if not findings and not self.blackboard.get_state_value(_REPROMPTED, False):
            planner = self.blackboard.get_state_value("role_bindings", {}).get("planner")
            if not planner:
                raise ValueError(f"[{self.name}] manager has no planner role binding to return to")
            self.blackboard.update_state_value(_REPROMPTED, True)
            self.blackboard.add_msg(Message(
                data_type="agent_request", sender=self.name, receiver=planner,
                content=("You returned control without recording a result for this attempt. The "
                         "findings you write during this attempt are the task's result; nothing else "
                         "writes it. Write the result in `findings` now, stating what it rests on "
                         "(this attempt's tool results, or the earlier record you reuse), then "
                         "return_control.")))
            logger.warning("[%s] %s::%s attempt %s returned without a result; back to %s",
                           self.name, ctx.work_id, ctx.node_id, attempt, planner)
            self.blackboard.update_state_value("next_agent", planner)
            return

        if findings:
            payload = {"final_answer_answer": "\n".join(f"- {(n.content or '').strip()}" for n in findings),
                       "finding_ids": [n.id for n in findings], "finding_attempt": attempt}
        else:
            logger.error("[%s] %s::%s attempt %s: no result recorded after a second return",
                         self.name, ctx.work_id, ctx.node_id, attempt)
            payload = {"final_answer_answer": "The worker returned control twice without recording a "
                                              "result for this attempt. No result was recorded.",
                       "finding_attempt": attempt}
        # The same envelope and pod carry-through manager_exit_node gives a final-answer agent's output,
        # so pods a delegated manager produced still reach the node.
        payload = FinalAnswerNormalizer.attach_carry_through(payload, self.blackboard)
        final = FinalAnswerNormalizer.normalize(payload)
        if not findings:
            final["exit_state"] = "error_exit"   # result_recorder records the attempt as failed
        self.blackboard.update_state_value("final_answer", final)
        self.blackboard.update_state_value("final_answer_raw", payload)
        self.blackboard.update_state_value("next_agent", None)
