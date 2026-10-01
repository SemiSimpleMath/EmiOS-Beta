"""Owner-only /dayflow/flow page: dayflow as a diagram, and what each of its agents was last sent and
answered (2026-09-30).

The diagram is read from the three managers' own configs, so it shows what runs: each manager's
`state_map`, walked from its delegator to its final answer, is the sequence of its stages. A stage
is an agent (a model call) or a control node (code); a control node's description is its module or
class docstring, and the agents it calls are the ones its file creates. What the configs do not
say — what starts each manager and how they hand over to each other — is in LINKS below.

Calls are those recorded by subconscious/brain_trace.py: every `dayflow_orchestrator::` agent, and
every agent called inside a dayflow work attempt (the worker's agents, the ticket builder), newest
first. Open one for its system prompt, user prompt and result (/api/brain/calls/<id>).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from flask import Blueprint, jsonify, render_template, request

from app.routes._security import reject_if_not_local

dayflow_flow_bp = Blueprint("dayflow_flow", __name__)
dayflow_flow_bp.before_request(reject_if_not_local)

_ASSISTANT = Path(__file__).resolve().parents[1] / "assistant"
MANAGERS = [
    ("dayflow_orchestrator_manager", "Planning pass",
     "One at a time with the wake pass. Started by the scheduler: new intake, work progress, a brain "
     "handoff, or the 30-minute ceiling. Ends when it has claimed one node to run."),
    ("dayflow_wake_manager", "Wake pass",
     "One due node's time-wake. No planning stage: it re-judges the moment, then dispatches or holds."),
    ("dayflow_dispatch_manager", "Dispatch room",
     "One claimed node, on its own thread: build the tool's arguments, make the call (which may block "
     "for a whole ask), judge the result."),
]
# Roles of the agents, as written in CLAUDE.md's dayflow section; the configs carry none.
AGENT_ROLES = {
    "dayflow_orchestrator::intake_triage": "Triage: which pending intake is worth the steward's attention.",
    "dayflow_orchestrator::strategic_planner_wo": "The steward: decides WHAT work exists. Turns intake "
        "(including the brain's concern handoffs) into work objects, reviews the rest as no-action or "
        "deferred, completes or abandons work.",
    "dayflow_orchestrator::work_architect": "The architect: lays each new goal's graph of tasks, and "
        "re-plans flagged ones.",
    "dayflow_orchestrator::state_mover": "Promotes ready tasks; may hold one for the current context.",
    "dayflow_orchestrator::action_selector": "Picks the one ready task to run now.",
    "dayflow_orchestrator::switchboard": "Reads the task's goal: reaching the user becomes a ticket "
        "(create_dayflow_ticket), everything else goes to the worker (work_emi_team_manager).",
    "dayflow_orchestrator::work_finalizer": "Judges whether the task's goal was achieved: achieved / "
        "achieved_plan_changes / retry / unrecoverable, with next step and an account of what happened.",
    "dayflow_orchestrator::result_formatter": "Writes the pass's closing summary.",
}
# How the passes meet each other and the rest of the assistant: not in any state_map.
LINKS = {
    "work_node_dispatch_node": "claims one node and opens the Dispatch room for it; the pass ends here",
    "strategic_planner_wo_prep_node": "reads pending intake: the brain's concern handoffs and admitted pods",
    "dayflow_tool_caller": "runs the worker (work_emi_team_manager) or a ticket to the user "
                           "(create_dayflow_ticket); calls made inside are shown below",
    "work_finalizer_node": "writes the judgment; the concerns the work is attached to get it, and the "
                           "brain reads it",
}
_PLUMBING = {"final_answer_node", "manager_exit_node", "graceful_exit", "graceful_exit_control_node"}


def _docstring(node_name: str) -> str:
    path = _ASSISTANT / "control_nodes" / f"{node_name}.py"
    if not path.exists():
        return ""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    text = ast.get_docstring(tree) or next(
        (ast.get_docstring(n) for n in tree.body if isinstance(n, ast.ClassDef) and ast.get_docstring(n)), "")
    return " ".join((text or "").split("\n\n")[0].split())


def _agents_called(node_name: str) -> List[str]:
    path = _ASSISTANT / "control_nodes" / f"{node_name}.py"
    if not path.exists():
        return []
    return sorted(set(re.findall(r"create_agent\(\s*['\"]([^'\"]+)['\"]", path.read_text(encoding="utf-8"))))


def _engine(agent_name: str) -> Optional[str]:
    namespace, _, name = agent_name.partition("::")
    path = _ASSISTANT / "agents" / namespace / name / "config.yaml"
    if not path.exists():
        return None
    return ((yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("llm_params") or {}).get("engine")


def _stages(manager: str) -> List[Dict[str, Any]]:
    config = yaml.safe_load((_ASSISTANT / "multi_agents" / manager / "config.yaml").read_text(encoding="utf-8"))
    agents = {a["name"] for a in config.get("agents") or []}
    state_map = config["flow_config"]["state_map"]
    stages, seen, name = [], set(), state_map["room::delegator"]
    while name and name not in _PLUMBING and name not in seen:
        seen.add(name)
        is_agent = name in agents
        calls = [name] if is_agent else _agents_called(name)
        stages.append({"name": name, "kind": "agent" if is_agent else "control",
                       "description": AGENT_ROLES.get(name, "") if is_agent else _docstring(name),
                       "agents": [{"name": a, "engine": _engine(a), "role": AGENT_ROLES.get(a, "")} for a in calls],
                       "link": LINKS.get(name, ""), "work_calls": name == "dayflow_tool_caller"})
        name = state_map.get(name)
    return stages


@dayflow_flow_bp.route("/dayflow/flow")
def dayflow_flow_page():
    return render_template("dayflow_flow.html")


@dayflow_flow_bp.route("/api/dayflow/flow")
def dayflow_flow_api():
    return jsonify({"managers": [{"name": m, "title": t, "about": about, "stages": _stages(m)}
                                 for m, t, about in MANAGERS]})


@dayflow_flow_bp.route("/api/dayflow/calls")
def dayflow_calls_api():
    """Newest recorded calls of one agent, or (work=1) of the agents called inside work attempts."""
    from app.assistant.subconscious import brain_trace
    limit = min(int(request.args.get("limit") or 10), 50)
    if request.args.get("work"):
        return jsonify({"calls": brain_trace.work_attempt_calls(limit=limit)})
    agent = (request.args.get("agent") or "").strip()
    if not agent:
        return jsonify({"error": "agent or work is required"}), 400
    return jsonify({"calls": brain_trace.list_calls(agent=agent, limit=limit)})
