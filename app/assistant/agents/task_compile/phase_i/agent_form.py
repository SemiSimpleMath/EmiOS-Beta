from __future__ import annotations

from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field


class AtomicEvent(BaseModel):
    id: str = Field(description="Stable event id, for example event_1.")
    event_type: Literal["watch_event", "state_event", "time_event"] = Field(
        description="Atomic event type."
    )
    description: str = Field(
        default="",
        description="Short description of the declared runtime trigger.",
    )
    canonical_event_name: str = Field(
        default="",
        description="Canonical runtime event name.",
    )


class AtomicCondition(BaseModel):
    id: str = Field(description="Stable condition id, for example condition_1.")
    condition_type: Literal["event_observed", "state_predicate", "time_predicate"] = Field(
        description="Atomic condition type."
    )
    description: str = Field(
        default="",
        description="Short description of the runtime-readable predicate.",
    )
    expression: str = Field(
        default="",
        description="Runtime-parseable predicate expression.",
    )


class ProducedData(BaseModel):
    """A fact or artifact produced by an action step."""
    id: str = Field(
        description="Data id, for example artifact_1 or fact_1.",
    )
    kind: Literal["fact", "artifact"] = Field(
        description="Whether this is a scalar fact or a rich artifact.",
    )
    description: str = Field(
        description="What this data represents. For example: 'Email summary results from today' or 'CNN/BBC news screenshots and headlines'.",
    )


class ToolCallSpec(BaseModel):
    """A single deterministic tool call within a tool_sequence action."""
    tool: str = Field(description="Tool name (must exist in tool registry).")
    args_json: str = Field(default="{}", description='Arguments as a JSON object string, e.g. {"url": "https://www.cnn.com"}. Use ${data_id} for consumed input references.')


class AtomicAction(BaseModel):
    id: str = Field(description="Stable action id, for example action_1.")
    executor: str = Field(
        default="",
        description=(
            "Manager name that executes this action block. Must be from the manager catalog. "
            "Leave empty when deterministic=true (tool_sequence steps don't use managers)."
        ),
    )
    deterministic: bool = Field(
        default=False,
        description=(
            "True when this action is a fixed sequence of tool calls with known arguments — "
            "no LLM/manager needed. The task spec marks these as '(deterministic)'. "
            "When true, the tools list must be provided and executor is ignored."
        ),
    )
    instruction: str = Field(
        default="",
        description="Standalone executable work statement for this action block.",
    )
    tools: List[ToolCallSpec] = Field(
        default_factory=list,
        description=(
            "Ordered list of direct tool calls for deterministic actions. "
            "Required when deterministic=true. Empty for manager-based actions."
        ),
    )
    consumes_data_ids: List[str] = Field(
        default_factory=list,
        description=(
            "Data ids consumed by this action. Every id listed here MUST be "
            "produced by a prior action's produces list OR be a preloaded "
            "task input. Never reference a data id that is not explicitly "
            "produced or preloaded."
        ),
    )
    produces: List[ProducedData] = Field(
        default_factory=list,
        description=(
            "Data produced by this action. Each entry declares the id, kind, "
            "and a short description of what it represents. At most one "
            "artifact per action."
        ),
    )


class PhaseIAtoms(BaseModel):
    events: List[AtomicEvent] = Field(
        default_factory=list,
        description="Atomic event declarations extracted from the task.",
    )
    conditions: List[AtomicCondition] = Field(
        default_factory=list,
        description="Atomic runtime-readable conditions extracted from the task.",
    )
    actions: List[AtomicAction] = Field(
        default_factory=list,
        description="Atomic uninterrupted executable action blocks extracted from the task.",
    )


class AgentForm(BaseModel):
    phase_i_atoms: PhaseIAtoms
