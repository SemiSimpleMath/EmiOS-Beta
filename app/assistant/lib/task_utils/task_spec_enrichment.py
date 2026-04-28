"""
Apply tool planner results back to the TaskSpec model.

Takes the StepPlan output from the tool planner agent and enriches
the TaskSpec's plain English steps with execution details: kind,
tools, manager, data flow, waits, decisions.

Also handles step splitting when the planner recommends separating
collection from interpretation.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from app.assistant.lib.task_utils.task_spec_model import (
    DataRef,
    DecisionBranch,
    StepKind,
    TaskSpec,
    TaskStep,
    ToolCall,
    WaitCondition,
    WaitType,
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def apply_step_plans(spec: TaskSpec, step_plans: List[Dict[str, Any]]) -> TaskSpec:
    """Apply tool planner results to the TaskSpec's steps.

    Enriches each step with kind, tools/manager, data flow.
    Handles merged steps via source_steps field — when the planner
    merges consecutive same-executor steps, the plan covers multiple
    original steps and produces a single combined step.
    """
    if not step_plans:
        return spec

    new_steps: List[TaskStep] = []
    artifact_counter = 1

    for i, plan in enumerate(step_plans):
        if not isinstance(plan, dict):
            continue

        # Resolve the original step(s) this plan covers.
        source_steps = plan.get("source_steps") or []
        if source_steps and isinstance(source_steps, list):
            # Merge: combine descriptions from all source steps (1-based indices).
            source_indices = [int(s) - 1 for s in source_steps if isinstance(s, (int, float)) and int(s) - 1 < len(spec.steps)]
            if source_indices:
                step = spec.steps[source_indices[0]]
                if len(source_indices) > 1:
                    # Combine descriptions from merged steps.
                    merged_descs = [spec.steps[idx].description for idx in source_indices if spec.steps[idx].description]
                    if merged_descs:
                        step.description = " ".join(merged_descs)
                    merged_names = [spec.steps[idx].name for idx in source_indices if spec.steps[idx].name]
                    if merged_names:
                        step.name = " + ".join(merged_names)
            else:
                step = TaskStep(
                    name=str(plan.get("step_name") or f"Step {i + 1}"),
                    description="",
                )
        elif i < len(spec.steps):
            # Fallback: 1:1 mapping by index.
            step = spec.steps[i]
        else:
            step = TaskStep(
                name=str(plan.get("step_name") or f"Step {i + 1}"),
                description="",
            )

        kind_str = str(plan.get("kind") or "action").strip()
        kind = _parse_kind(kind_str)

        step.kind = kind

        # Data flow — consumes
        consumes = plan.get("consumes")
        if isinstance(consumes, list):
            step.consumes = [str(c) for c in consumes if c]

        # Data flow — produces
        produces_raw = plan.get("produces")
        if isinstance(produces_raw, list):
            step.produces = []
            for prod in produces_raw:
                if isinstance(prod, dict):
                    step.produces.append(DataRef(
                        id=str(prod.get("id") or f"artifact_{artifact_counter}"),
                        kind=str(prod.get("kind") or "artifact"),
                        description=str(prod.get("description") or ""),
                    ))
                    artifact_counter += 1

        # Kind-specific fields
        if kind == StepKind.TOOL_SEQUENCE:
            tools_raw = plan.get("tools") or []
            step.tools = []
            for tc in tools_raw:
                if not isinstance(tc, dict):
                    continue
                tool_name = str(tc.get("tool") or "").strip()
                args_json = str(tc.get("args_json") or "{}").strip()
                try:
                    args = json.loads(args_json) if args_json else {}
                except json.JSONDecodeError:
                    args = {}
                step.tools.append(ToolCall(
                    tool=tool_name,
                    args=args,
                    purpose=str(tc.get("purpose") or "").strip(),
                ))

        elif kind == StepKind.ACTION:
            step.executor = str(plan.get("manager_name") or "emi_team_manager").strip()
            step.instruction = str(plan.get("instruction") or step.description).strip()
            raw_pinned = plan.get("pinned_tools")
            if isinstance(raw_pinned, list):
                step.pinned_tools = [str(t).strip() for t in raw_pinned if isinstance(t, str) and str(t).strip()]

        elif kind == StepKind.WAIT:
            wait_raw = plan.get("wait")
            if isinstance(wait_raw, dict):
                step.wait = WaitCondition(
                    wait_type=_parse_wait_type(str(wait_raw.get("wait_type") or "event")),
                    description=str(wait_raw.get("description") or "").strip(),
                    time_local=str(wait_raw.get("time_local") or "").strip(),
                    duration_minutes=int(wait_raw.get("duration_minutes", 0)),
                    event_name=str(wait_raw.get("event_name") or "").strip(),
                    event_description=str(wait_raw.get("description") or "").strip(),
                )

        elif kind == StepKind.DECISION:
            dec_raw = plan.get("decision")
            if isinstance(dec_raw, dict):
                step.decision = DecisionBranch(
                    condition=str(dec_raw.get("condition") or "").strip(),
                    description=str(dec_raw.get("description") or "").strip(),
                    on_true_step=str(dec_raw.get("on_true_step") or "").strip(),
                    on_false_step=str(dec_raw.get("on_false_step") or "").strip(),
                )

        new_steps.append(step)

    spec.steps = new_steps
    spec.stage = "planned"
    return spec


def _parse_kind(kind_str: str) -> StepKind:
    mapping = {
        "tool_sequence": StepKind.TOOL_SEQUENCE,
        "deterministic": StepKind.TOOL_SEQUENCE,
        "action": StepKind.ACTION,
        "manager": StepKind.ACTION,
        "wait": StepKind.WAIT,
        "decision": StepKind.DECISION,
    }
    return mapping.get(kind_str.lower(), StepKind.ACTION)


def _parse_wait_type(wait_str: str) -> WaitType:
    mapping = {
        "time": WaitType.TIME,
        "duration": WaitType.DURATION,
        "event": WaitType.EVENT,
        "user_reply": WaitType.USER_REPLY,
    }
    return mapping.get(wait_str.lower(), WaitType.EVENT)
