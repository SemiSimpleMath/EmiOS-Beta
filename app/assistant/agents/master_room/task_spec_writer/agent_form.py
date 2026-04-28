from typing import Dict, List, Literal, Any, Optional

from pydantic import BaseModel, Field


class SpecEdit(BaseModel):
    """A single targeted edit to the task spec."""
    op: Literal[
        "set_title",
        "set_goal",
        "set_completion",
        "add_step",
        "edit_step",
        "remove_step",
        "reorder_steps",
        "add_input",
        "remove_input",
        "set_description",
    ] = Field(description="The operation to perform on the spec.")

    step_index: int = Field(
        default=-1,
        description="0-based step index for step operations (add_step uses -1 to append). Ignored for non-step ops.",
    )

    value: str = Field(
        default="",
        description=(
            "String value for simple ops: "
            "set_title → new title, "
            "set_goal → new goal, "
            "set_completion → completion text, "
            "set_description → task description, "
            "remove_step → ignored, "
            "remove_input → input id to remove."
        ),
    )

    step_name: str = Field(default="", description="Step name for add_step or edit_step.")
    step_description: str = Field(default="", description="Step description for add_step or edit_step.")

    input_id: str = Field(default="", description="Input id for add_input (e.g., 'fact_1').")
    input_kind: Literal["fact", "artifact", ""] = Field(default="", description="Input kind for add_input.")
    input_description: str = Field(default="", description="Input description for add_input.")
    input_value: str = Field(default="", description="Input value for add_input (optional).")

    new_order: List[int] = Field(
        default_factory=list,
        description="New step order as list of current indices for reorder_steps (e.g., [2, 0, 1]).",
    )


class AgentForm(BaseModel):
    task_action: Literal["update", "no_op"] = Field(
        default="no_op",
        description=(
            "Choose 'update' ONLY when the task spec must change this turn. "
            "Choose 'no_op' when the exchange adds no new task-spec content. "
            "When task_action is 'no_op', edits MUST be empty."
        ),
    )
    edits: List[SpecEdit] = Field(
        default_factory=list,
        description=(
            "Ordered list of operations to apply to the spec. "
            "MUST be empty when task_action is 'no_op'."
        ),
    )
