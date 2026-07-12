"""
Task Spec Data Model.

Defines the canonical structure for a task specification. This is the
single source of truth for what a task IS — the UI renders it, the
spec writer mutates it, the tool planner enriches it, and the compiler
reads it.

A task spec goes through stages:
1. Draft: title + goal + plain English steps
2. Planned: steps enriched with executors, tools, and data flow
3. Compiled: converted to a work object (see task_runtime.wo_builder)
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class StepKind(str, Enum):
    """How a step is executed at runtime."""
    ACTION = "action"                   # LLM-driven: delegated to a manager
    TOOL_SEQUENCE = "tool_sequence"     # Deterministic: direct tool calls, no LLM
    WAIT = "wait"                       # Pause: wait for time, event, or signal
    DECISION = "decision"               # Branch: evaluate a condition


class WaitType(str, Enum):
    """What a wait step is waiting for."""
    TIME = "time"                       # Wait until a specific time (e.g., "at 9:00 AM")
    DURATION = "duration"               # Wait for a duration (e.g., "wait 30 minutes")
    EVENT = "event"                     # Wait for an external event (e.g., "email from Katy")
    USER_REPLY = "user_reply"           # Wait for the user to respond to a notification


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

class ToolCall(BaseModel):
    """A single deterministic tool call with known arguments."""
    tool: str = Field(description="Tool name (must exist in tool registry).")
    args: Dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments to pass. Use ${artifact_N} for data from prior steps.",
    )
    purpose: str = Field(default="", description="One-line description of what this call does.")


class DataRef(BaseModel):
    """A reference to data produced or consumed by a step."""
    id: str = Field(description="Canonical data id, e.g. artifact_1, fact_1.")
    kind: Literal["artifact", "fact"] = Field(
        default="artifact",
        description="artifact = rich content (email summaries, screenshots). fact = scalar (boolean, number, string).",
    )
    description: str = Field(default="", description="What this data represents.")


class WaitCondition(BaseModel):
    """What a wait step is waiting for."""
    wait_type: WaitType = Field(description="Type of wait.")
    description: str = Field(description="Human-readable description of the wait condition.")
    # Time-based
    time_local: str = Field(default="", description="Local time to wait until (e.g., '09:00', '17:30').")
    duration_minutes: int = Field(default=0, description="Duration to wait in minutes.")
    # Event-based
    event_name: str = Field(default="", description="Canonical event name to wait for.")
    event_description: str = Field(default="", description="Human description of the event.")


class DecisionBranch(BaseModel):
    """A branch in a decision step."""
    condition: str = Field(description="Condition expression (e.g., 'artifact_1 contains urgent').")
    description: str = Field(default="", description="Human description of this branch.")
    on_true_step: str = Field(default="", description="Step name to go to if true.")
    on_false_step: str = Field(default="", description="Step name to go to if false.")


# ---------------------------------------------------------------------------
# Step
# ---------------------------------------------------------------------------

class TaskStep(BaseModel):
    """One step in a task specification.

    A step starts as plain English (stage 1-2) and gets enriched with
    technical details (stage 3) by the tool planner.
    """
    # Identity
    name: str = Field(description="Short, descriptive step name.")
    description: str = Field(default="", description="Plain English description of what this step does.")

    # Execution (filled in by tool planner at stage 3)
    kind: Optional[StepKind] = Field(
        default=None,
        description="How this step executes. None = not yet planned.",
    )

    # For ACTION steps (manager-driven)
    executor: str = Field(
        default="",
        description="Manager name for action steps (e.g., 'emi_team_manager', 'personal_admin_manager').",
    )
    instruction: str = Field(
        default="",
        description="Natural language instruction for the manager. Derived from description if not set.",
    )
    pinned_tools: List[str] = Field(
        default_factory=list,
        description="Tools to pin as visible for this step, bypassing the narrower.",
    )

    # For TOOL_SEQUENCE steps (deterministic)
    tools: List[ToolCall] = Field(
        default_factory=list,
        description="Ordered tool calls for deterministic steps.",
    )

    # For WAIT steps
    wait: Optional[WaitCondition] = Field(
        default=None,
        description="Wait condition for wait steps.",
    )

    # For DECISION steps
    decision: Optional[DecisionBranch] = Field(
        default=None,
        description="Branch condition for decision steps.",
    )

    # Data flow (can be set during stage 2 or 3)
    produces: List[DataRef] = Field(
        default_factory=list,
        description="Data this step produces for downstream steps.",
    )
    consumes: List[str] = Field(
        default_factory=list,
        description="Data ids this step needs from prior steps (e.g., ['artifact_1', 'artifact_2']).",
    )

    @property
    def is_planned(self) -> bool:
        """Whether the tool planner has enriched this step."""
        return self.kind is not None

    def reset_planning(self) -> None:
        """Clear all enrichment fields, returning the step to unplanned state."""
        self.kind = None
        self.executor = ""
        self.instruction = ""
        self.pinned_tools = []
        self.tools = []
        self.wait = None
        self.decision = None
        self.produces = []
        self.consumes = []


# ---------------------------------------------------------------------------
# Known Inputs
# ---------------------------------------------------------------------------

class KnownInput(BaseModel):
    """A value that exists before the task runs."""
    id: str = Field(description="Reference id (e.g., fact_1, artifact_1).")
    kind: Literal["fact", "artifact"] = Field(description="fact = scalar, artifact = file/content.")
    description: str = Field(description="What this input is (e.g., 'recipient email address').")
    value: Optional[str] = Field(
        default=None,
        description="Concrete value if known (e.g., 'recipient@example.com'). None if provided at runtime.",
    )


# ---------------------------------------------------------------------------
# Task Spec (top-level)
# ---------------------------------------------------------------------------

class TaskSpec(BaseModel):
    """Complete task specification.

    This is the canonical structure that all task creation agents
    read and write. The UI panel renders it as markdown.
    """
    # Metadata
    task_id: str = Field(default="", description="Unique task identifier. Auto-generated if empty.")
    title: str = Field(default="", description="Human-readable task title.")
    goal: str = Field(default="", description="One-sentence goal — what is the outcome?")
    description: str = Field(default="", description="Additional context or notes.")
    created_at_utc: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="When the spec was created.",
    )

    # Known inputs (things that exist before the task runs)
    known_inputs: List[KnownInput] = Field(
        default_factory=list,
        description="Facts and artifacts available before the task starts.",
    )

    # Steps (the ordered operations)
    steps: List[TaskStep] = Field(
        default_factory=list,
        description="Ordered list of task steps.",
    )

    # Completion
    completion: str = Field(
        default="",
        description="When is this task done? Success criteria.",
    )

    # Planning state
    stage: Literal["draft", "steps_defined", "planned", "compiled"] = Field(
        default="draft",
        description="Current stage: draft → steps_defined → planned → compiled.",
    )

    @property
    def all_steps_planned(self) -> bool:
        """Whether all steps have been enriched by the tool planner."""
        return bool(self.steps) and all(s.is_planned for s in self.steps)

    def reset_all_planning(self) -> None:
        """Reset all steps to unplanned state."""
        for step in self.steps:
            step.reset_planning()
        self.stage = "steps_defined"

    def to_markdown(self) -> str:
        """Render the spec as markdown for the UI panel."""
        parts: list[str] = []

        parts.append("## Title")
        parts.append(self.title or "[New Task]")
        parts.append("")
        parts.append("## Description")
        parts.append(self.goal or "[TBD]")
        parts.append("")

        if self.known_inputs:
            parts.append("## Known Inputs")
            for inp in self.known_inputs:
                val = f": {inp.value}" if inp.value else ""
                parts.append(f"- {inp.id} ({inp.kind}){val} — {inp.description}")
            parts.append("")

        parts.append("## Steps")
        if not self.steps:
            parts.append("[No steps defined yet]")
        else:
            for i, step in enumerate(self.steps, start=1):
                parts.append(self._render_step(i, step))
        parts.append("")

        parts.append("## Completion")
        parts.append(self.completion or "[TBD]")

        return "\n".join(parts)

    @staticmethod
    def _render_step(num: int, step: TaskStep) -> str:
        """Render a single step as markdown.

        Always shows the plain English description first, then technical
        details underneath (if the step has been planned).
        """
        lines: list[str] = []

        # Header: step name + kind tag
        kind_tag = ""
        if step.kind == StepKind.TOOL_SEQUENCE:
            kind_tag = " `deterministic`"
        elif step.kind == StepKind.ACTION:
            kind_tag = f" → `{step.executor}`" if step.executor else ""
        elif step.kind == StepKind.WAIT:
            kind_tag = " `wait`"
        elif step.kind == StepKind.DECISION:
            kind_tag = " `decision`"

        lines.append(f"{num}. **{step.name}**{kind_tag}")

        # Always show the plain English description.
        if step.description:
            lines.append(f"   {step.description}")

        # Technical details (only for planned steps).
        if step.kind == StepKind.TOOL_SEQUENCE and step.tools:
            lines.append("   - tools:")
            for tc in step.tools:
                args_str = ", ".join(
                    f'{k}="{v}"' if isinstance(v, str) else f"{k}={v}"
                    for k, v in tc.args.items()
                )
                lines.append(f"     - `{tc.tool}({args_str})`")
        elif step.kind == StepKind.ACTION and step.instruction and step.instruction != step.description:
            lines.append(f"   - instruction: {step.instruction}")
            if step.pinned_tools:
                lines.append(f"   - pinned_tools: {', '.join(step.pinned_tools)}")
        elif step.kind == StepKind.WAIT and step.wait:
            lines.append(f"   - condition: {step.wait.description}")
            if step.wait.time_local:
                lines.append(f"   - until: {step.wait.time_local}")
            if step.wait.event_description:
                lines.append(f"   - event: {step.wait.event_description}")
        elif step.kind == StepKind.DECISION and step.decision:
            lines.append(f"   - condition: {step.decision.condition}")
            lines.append(f"   - if true → {step.decision.on_true_step}")
            lines.append(f"   - if false → {step.decision.on_false_step}")

        # Data flow
        for prod in step.produces:
            lines.append(f"   - produces: {prod.id} ({prod.description})")
        if step.consumes:
            lines.append(f"   - consumes: {', '.join(step.consumes)}")

        return "\n".join(lines)
