"""
work_objects.work_agent — the WorkPlanner: a normal tool-calling agent that owns
one node and advances it through the WorkGraph tools.

It reuses the live `Planner` agent class (the action/action_input tool-caller
shape) and is registered into the agent_registry at RUNTIME, so nothing lands
under app/. Its `allowed_tools` are the work_* graph tools; its prompts carry the
node projection. The canonical manager loop drives it (agent -> tool_caller ->
agent) — this module just defines + registers the agent.

Imports app.*, so callers must bootstrap DI first.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from app.assistant.agent_classes.Planner import Planner

from work_objects.work_tools import WORK_TOOL_NAMES


class WorkPlannerForm(BaseModel):
    """One decision per cycle: pick the next graph tool to call."""
    thinking: str = Field(description="Brief reasoning about the node and what to do next.")
    plan: str = Field(description="The remaining steps to satisfy this node.")
    action: str = Field(description="The single tool to call now, e.g. 'work_add_subtask' or 'work_finish'.")
    action_input: str = Field(description="JSON object of arguments for the chosen tool.")


_SYSTEM = """\
You are a WorkAgent. You OWN exactly one node in a work graph and must advance it
to completion, ONE tool call per turn. The loop re-runs you with each result.

Decide your NEXT single action:
- DECOMPOSE (node names multiple separable parts — several items, concerns, or
  distinct searches): split it into 1-5 subtasks, ONE per part, via
  `work_add_subtask` (one call per turn). This is MANDATORY for a multi-part node —
  do NOT execute a multi-part node yourself as one giant call.
    * Keep INDEPENDENT subtasks dep-free (omit depends_on). Add depends_on ONLY when
      a subtask genuinely needs another's output — NEVER just to impose an order on
      independent work. Dep-free siblings are the graph's record that "these are
      independent"; the orchestrator decides how to schedule them.
    * If the parts must be merged (compare / recommend / summarize across them), add
      ONE final synthesis subtask with depends_on=[the independent subtask ids]; it
      will see their results when it runs.
    * Shape (e.g. the espresso task): "research A" depends_on=[]; "research B"
      depends_on=[]; "research C" depends_on=[]; "recommend" depends_on=[A,B,C].
    * Don't `work_finish` a node you decomposed — it completes when its children do;
      `return_control` instead.
- EXECUTE (node is a SINGLE actionable unit — one search, one deliverable): do the
  work, then record durable results — `work_record_finding` per discovery, and
  `work_produce_artifact` ONCE for the deliverable. For WEB RESEARCH, call
  `web_manager` with a clear question/task; it returns researched findings, which
  you then save with `work_record_finding`.
- INSPECT only if you still need context: `work_graph_peek` / `work_graph_search`.
- USE YOUR DEPENDENCIES: if your node depends on other subtasks, THEIR OUTPUTS ARE
  ALREADY in your projection under "DEPENDENCIES" (with the produced content). Read
  them THERE. Do NOT peek a node already shown in your projection, and NEVER peek the
  same node twice. If an output still isn't fully readable, proceed with what you
  have — produce your deliverable and finish. Do NOT loop hunting for content.

COMPLETION — mandatory and immediate:
  The moment the node's core work is done — you've added its subtasks, OR produced
  its one deliverable — your ONLY valid next action is to CLOSE the node:
    • executed a leaf node  ->  `work_finish` (status=satisfied)
    • decomposed a node     ->  `return_control`
  Do NOT peek, search, re-check, or re-produce once the core work is done.

ON AN UNRECOVERABLE ERROR (abort, don't loop):
  If a tool keeps failing in a way you cannot fix (an auth/permission error, or the
  SAME failure ~2-3 times despite changing your approach), STOP. Call `work_finish`
  with status=failed and a `reason` naming the error. Never retry in a loop, and
  never fake completion (work_finish=satisfied / work_produce_artifact) on partial
  or degraded results.

YOUR TOOLS — call by EXACT name, using exactly these argument keys:
{{ tool_descriptions }}

Respond with: thinking, plan, action (a work_* tool name OR 'return_control'),
action_input (a JSON object of the tool's arguments; {} for return_control).
"""

_USER = """\
Current time: {{ date_time }}

## The node you own
{{ work_projection }}

## What you have already done on this node (your prior turns + their results)
{{ recent_history }}

Decide and emit your next single action. If your prior turns show the node's work
is already done, CLOSE it now (work_finish / return_control) — do not repeat it.
"""


def register_work_agent(agent_registry, name: str = "work::planner",
                        llm_params: Optional[dict] = None,
                        extra_tools: Optional[list] = None) -> str:
    """Register a WorkPlanner config at runtime. `extra_tools` widens the agent's
    surface beyond the work_* graph tools (e.g. web research tools for a web
    planner), so the same agent can both DO real work and record it into the graph."""
    agent_registry.configs[name] = {
        "name": name,
        "class_name": "Planner",
        "class": Planner,
        "type": "agent",
        "llm_params": llm_params or {"llm_provider": "openai", "engine": "gpt-5.2", "model_tier": "smart"},
        "allowed_tools": list(WORK_TOOL_NAMES) + list(extra_tools or []),
        "except_tools": [],
        "allowed_nodes": ["return_control"],
        "structured_output": WorkPlannerForm,
        "prompts": {"system": _SYSTEM, "user": _USER},
        "user_context_items": ["work_projection", "date_time", "recent_history", "action_count"],
        "system_context_items": ["tool_descriptions"],
        "instance": None,
    }
    return name


def render_projection(p) -> str:
    """Render a NodeProjection (from WorkManager._project) into the prompt blob."""
    n = p.node
    lines = [
        f"TYPE: {n.type}    STATUS: {n.status}",
        f"TITLE: {n.title}",
        f"TASK: {n.content}",
        f"SUCCESS WHEN: {n.satisfied_when_kind}",
    ]
    if p.goal and p.goal.id != n.id:
        lines.append(f"PARENT GOAL: {p.goal.title}")
    if p.dependencies:
        lines.append("DEPENDENCIES — these upstream nodes are ALREADY SOLVED; use their results:")
        for d in p.dependencies:
            lines.append(f"  - {d.node['title']}:")
            for prod in d.produced:
                detail = (prod.get("content") or prod.get("pod_ref") or "").strip()
                line = f"      * [{prod['type']}] {prod['title']}"
                if detail:
                    line += f" -> {detail}"
                lines.append(line)
    if p.summary_index:
        lines.append("OTHER NODES (reference these ids in work_graph_peek / depends_on):")
        for s in p.summary_index:
            lines.append(f"  - {s['id']}  [{s['type']}/{s['status']}] {s['title']}")
    return "\n".join(lines)
