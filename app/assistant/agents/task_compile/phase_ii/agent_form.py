from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, Field


class StepRationale(BaseModel):
    step_id: str = Field(description="Step id this rationale applies to.")
    reasoning: str = Field(
        description=(
            "Why this step was composed as-is: kind choice, executor selection, "
            "dataflow decisions, branching logic, or critic feedback response."
        )
    )


class ToolCall(BaseModel):
    """A single deterministic tool call within a tool_sequence step."""
    tool: str = Field(description="Tool name (must exist in tool registry).")
    args_json: str = Field(default="{}", description='Arguments as a JSON object string, e.g. {"url": "https://www.cnn.com"}. Use ${data_id} for consumed input references.')


class TaskStep(BaseModel):
    id: str = Field(description="Stable step id.")
    kind: Literal["action", "tool_sequence", "wait_for_event", "wait_gate", "decision", "end"] = Field(
        description="Step kind enum. Use 'tool_sequence' for deterministic steps with known tool calls and arguments (no LLM/manager needed)."
    )
    title: str = Field(default="", description="Short human-readable title.")
    executor: str = Field(
        default="",
        description="Manager name for action steps. Must be from the manager catalog. Empty for non-action steps.",
    )
    instruction: str = Field(default="", description="Natural language instruction for action steps.")
    tools: List[ToolCall] = Field(
        default_factory=list,
        description="Ordered list of direct tool calls for tool_sequence steps. Each tool is called in order without LLM involvement. Empty for non-tool_sequence steps.",
    )
    event_name: str = Field(default="", description="Canonical event name for wait steps.")
    subscriptions: List[str] = Field(
        default_factory=list,
        description="Canonical event names for wait_gate multi-source subscriptions.",
    )
    release_condition: str = Field(
        default="",
        description="Runtime-parseable boolean expression used by wait_gate to decide when to release.",
    )
    clear_condition: str = Field(
        default="",
        description="Leave empty in Phase II; post-processing/runtime owns clear-condition defaults.",
    )
    condition: str = Field(default="", description="Condition expression for decision steps.")
    on_true: str = Field(default="", description="Next step id when condition is true.")
    on_false: str = Field(default="", description="Next step id when condition is false.")
    next_step: str = Field(default="", description="Default next step id for linear transitions.")
    consumes_data_ids: List[str] = Field(
        default_factory=list,
        description="Artifact/fact ids this step consumes from prior steps.",
    )
    produces_data_ids: List[str] = Field(
        default_factory=list,
        description="Artifact/fact ids this step produces for downstream steps.",
    )


class CompiledTaskDraft(BaseModel):
    entry_step_id: str = Field(description="Entry step id.")
    steps: List[TaskStep] = Field(default_factory=list, description="Workflow steps.")


class LogicEdge(BaseModel):
    from_step_id: str = Field(description="Source step id in compiled workflow.")
    to_step_id: str = Field(description="Destination step id in compiled workflow.")
    reason: str = Field(default="", description="Why this edge exists (for example on_true/on_false/next_step).")


class LogicBinding(BaseModel):
    step_id: str = Field(description="Compiled workflow step id.")
    atomic_type: Literal["event", "condition", "action"] = Field(description="Atomic unit type from Phase I.")
    atomic_id: str = Field(description="Phase I atomic id (for example event_1/action_2).")


class LogicTree(BaseModel):
    entry_step_id: str = Field(description="Entry step id for composed workflow.")
    edges: List[LogicEdge] = Field(default_factory=list, description="Directed graph edges between steps.")
    bindings: List[LogicBinding] = Field(
        default_factory=list,
        description="Mapping from composed steps back to Phase I atom ids.",
    )


class AgentForm(BaseModel):
    thoughts: List[StepRationale] = Field(
        default_factory=list,
        description=(
            "Per-step reasoning explaining composition decisions. "
            "Include one entry per non-trivial step. "
            "If responding to critic feedback, note whether each flagged item was fixed or contested and why."
        ),
    )
    compiled_task: CompiledTaskDraft
    logic_tree: LogicTree
