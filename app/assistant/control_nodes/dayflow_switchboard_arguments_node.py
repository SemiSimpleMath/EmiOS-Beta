"""Dayflow switchboard arguments node.

Turns the switchboard's choice into a normalized tool-call payload, the same
stage `ChatSwitchboardArgumentsNode` and `MasterRoomSwitchboardArgumentsNode`
occupy for their surfaces.

Dayflow differs from those two in where the arguments come FROM. A chat
switchboard restates the user's request as `task` / `task_information`, so the
shared `normalize_switchboard_args` can read them off the blackboard. The
dayflow switchboard emits only `reason` and `delegate_to`, because the task is
not prose it invents — it is the NODE, and the node is on the graph. So this
node reads the node and builds the payload from it.

Every call carries the same facts about the node it is for, and no argument is
conditioned on which tool was picked:

- ``work_id`` / ``node_id`` — which node this call discharges
- ``trigger_context.work_node`` — the same ref as provenance, joined on ids
- ``task`` / ``information`` — the node's own goal and detail
- ``append_links`` — research pages this node hands over, when it has any

Each tool takes what it needs and ignores the rest: `run_work_node` reads the
ids, `create_dayflow_ticket` reads task/information (it builds its own brief
from them) plus the context and links. Adding a third tool needs no change
here, which is the point — the caller does not know what kind of tool it picked.
"""
from __future__ import annotations

from typing import Any, Dict

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

# How long this orchestrator is willing to hold one call open, and therefore how long a
# question stays answerable. Supplied on EVERY dispatch: it is the caller's policy, not a
# property of whichever tool was picked, and a tool that has no use for it ignores it.
#
# It has to be passed rather than left to the tool's own defaults, which are deliberately
# different from each other — create_dayflow_ticket defaults to a 4-hour ticket but only
# BLOCKS for 600s, so defaulting would mean the call reports "user not reached" after ten
# minutes while their question sits on screen for another three and a half hours.
#
# dispatch_sweeper imports this: its tolerance for a node that looks stuck has to exceed
# the longest a call may legitimately block, or it would fail nodes mid-question.
ASK_WINDOW_HOURS = 1


class DayflowSwitchboardArgumentsNode(ControlNode):
    """Build the tool-call payload for the one node this tick dispatches."""

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        delegate_to = str(self.blackboard.get_state_value("delegate_to", "") or "").strip()
        if not delegate_to:
            raise ValueError(f"[{self.name}] delegate_to must be set by the switchboard.")

        ref = str(self.blackboard.get_state_value("work_node_ref", "") or "").strip()
        if "::" not in ref:
            raise ValueError(
                f"[{self.name}] work_node_ref must be 'work_id::node_id', got {ref!r} — "
                f"the dispatch gate sets it when it claims the node."
            )
        work_id, node_id = ref.split("::", 1)

        arguments = self._arguments_for(work_id, node_id, ref)

        self.blackboard.update_state_value("action", delegate_to)
        self.blackboard.update_state_value("action_input", arguments)
        self.blackboard.update_state_value(
            "tool_arguments", {"target_name": delegate_to, "arguments": arguments},
        )
        logger.info("[%s] %s <- %s", self.name, delegate_to, ref)

        self.blackboard.update_state_value("calling_agent", None)
        self.blackboard.update_state_value("last_agent", self.name)

    def _arguments_for(self, work_id: str, node_id: str, ref: str) -> Dict[str, Any]:
        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store

        store = get_dayflow_work_store()
        wo = store.load(work_id)
        node = wo.nodes.get(node_id)
        if node is None:
            raise ValueError(f"[{self.name}] node {ref} is not on the graph.")

        # The node's own words. `content` is the planner's full instruction and is the
        # material a tool actually needs; `wake_ref` is a wake-match primitive (often just
        # the title) and only stands in when there is no content. Letting wake_ref shadow
        # content is how "planner + pencil + instrument on Aug 21" once reached the user as
        # "check on the supplies needed for music class".
        detail = str(node.content or "").strip() or str(getattr(node, "wake_ref", "") or "").strip()

        arguments: Dict[str, Any] = {
            "task": str(node.title or "").strip() or detail,
            "information": detail,
            "work_id": work_id,
            "node_id": node_id,
            "trigger_context": {"work_node": ref},
            "valid_hours": ASK_WINDOW_HOURS,
            "wait_timeout_seconds": ASK_WINDOW_HOURS * 3600,
        }

        links = _research_pod_ids(wo, node_id)
        if links:
            # A pod id must reach the user exactly or not at all — never via LLM
            # transcription — so the links are built here and appended deterministically.
            arguments["append_links"] = [f"Full report: /research/{p}" for p in links]
        return arguments


def _research_pod_ids(wo, node_id: str) -> list[str]:
    """Research pods this node hands over: the pod_refs on the node itself, its
    depends_on upstreams, and those upstreams' evidence/artifact children (parent-linked
    or produces-linked). Pure id walk — the kind is read off the pod URI, no wording."""
    ups = [e.src for e in wo.edges if e.dst == node_id and e.relation == "depends_on"]
    pool = {node_id, *ups}
    for uid in ups:
        produced = {e.dst for e in wo.edges if e.src == uid and e.relation == "produces"}
        pool |= {m.id for m in wo.nodes.values() if m.parent_id == uid} | produced
    ids: list[str] = []
    for nid in sorted(pool):
        n = wo.nodes.get(nid)
        pod = str(getattr(n, "pod_ref", "") or "") if n is not None else ""
        if pod.startswith("datapod:research_finding:") and pod not in ids:
            ids.append(pod)
    return ids
