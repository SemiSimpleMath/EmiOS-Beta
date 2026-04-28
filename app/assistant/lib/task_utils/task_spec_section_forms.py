"""
Per-section Pydantic forms and instructions for the task spec writer.

Each section of the task spec has:
- A Pydantic model (the schema the LLM fills)
- Instruction text (how to fill it)
- A function to extract current data for that section from the TaskSpec

The resolver maps a nano classifier target → (model, instructions, current_data).
This is the same pattern as ToolArguments: different schema per target,
same agent every time.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Tuple, Type

from pydantic import BaseModel, Field

from app.assistant.lib.task_utils.task_spec_model import TaskSpec


# ---------------------------------------------------------------------------
# Per-section Pydantic forms
# ---------------------------------------------------------------------------

class TitleForm(BaseModel):
    """Fill in the task title."""
    title: str = Field(description="A short, descriptive task title (2-6 words).")


class GoalForm(BaseModel):
    """Fill in the task goal."""
    goal: str = Field(description="One sentence describing the outcome of this task. What does success look like?")


class CompletionForm(BaseModel):
    """Fill in completion criteria."""
    completion: str = Field(description="When is this task done? Describe the success condition in one or two sentences.")


class DescriptionForm(BaseModel):
    """Fill in additional task description."""
    description: str = Field(description="Additional context, constraints, or notes about the task.")


class AddStepForm(BaseModel):
    """Add a new step to the task."""
    step_name: str = Field(description="Short step name (2-6 words, e.g., 'Fetch recent emails').")
    step_description: str = Field(
        description=(
            "Plain English description of what this step does. Focus on WHAT, not HOW. "
            "Do not include tool names or technical details — those are added later automatically."
        ),
    )


class EditStepForm(BaseModel):
    """Edit an existing step."""
    step_name: str = Field(
        default="",
        description="Updated step name. Leave empty to keep current name.",
    )
    step_description: str = Field(
        default="",
        description="Updated step description. Leave empty to keep current description.",
    )


class AddInputForm(BaseModel):
    """Add a known input to the task."""
    input_id: str = Field(
        description="Short reference id (e.g., 'recipient_email', 'config_file'). Use snake_case.",
    )
    input_kind: Literal["fact", "artifact"] = Field(
        description="'fact' for scalars (email, number, flag). 'artifact' for files or content.",
    )
    input_description: str = Field(
        description="What this input is and how it's used in the task.",
    )
    input_value: str = Field(
        default="",
        description="Concrete value if known (e.g., 'recipient@example.com'). Leave empty if provided at runtime.",
    )


# ---------------------------------------------------------------------------
# Per-section instructions
# ---------------------------------------------------------------------------

_INSTRUCTIONS: Dict[str, str] = {
    "title": (
        "The user is naming this task. You may refine capitalization but do not change wording."
    ),
    "goal": (
        "The user is describing what this task should accomplish overall. "
        "You may fix grammar but do not change meaning. Stick as closely as possible to the given text."
    ),
    "completion": (
        "The user is describing when this task is done. "
        "You may fix grammar but do not change meaning. Stick as closely as possible to the given text."
    ),
    "description": (
        "The user is adding context or notes about the task. "
        "You may fix grammar but do not change meaning. Stick as closely as possible to the given text."
    ),
    "add_step": (
        "The user is describing a new step for this task. "
        "Extract a short step name and a plain English description. "
        "You may fix grammar but do not change meaning. Stick as closely as possible to the given text. "
        "Focus on WHAT the step does, not HOW (no tool names, no technical details). "
        "The tool planner will figure out the technical execution later."
    ),
    "edit_step": (
        "The user is modifying an existing step. "
        "Only fill in fields that the user wants to change — leave others empty. "
        "You may fix grammar but do not change meaning. Stick as closely as possible to the given text."
    ),
    "add_input": (
        "The user is providing a known value that exists before the task runs. "
        "This could be an email address, file path, account ID, or similar. "
        "Give it a descriptive snake_case id and classify it as 'fact' (scalar) or 'artifact' (file/content)."
    ),
}


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

# Map: target → (Pydantic form class, requires current step data)
_FORM_MAP: Dict[str, Tuple[Type[BaseModel], bool]] = {
    "title": (TitleForm, False),
    "goal": (GoalForm, False),
    "completion": (CompletionForm, False),
    "description": (DescriptionForm, False),
    "add_step": (AddStepForm, False),
    "edit_step": (EditStepForm, True),   # Needs current step data
    "add_input": (AddInputForm, False),
}


def resolve_section(
    *,
    target: str,
    spec: TaskSpec,
    step_index: int = -1,
    user_content: str = "",
) -> Dict[str, Any] | None:
    """Resolve the form, instructions, and current data for a spec section.

    Returns dict with:
        form_class: Pydantic model class
        instructions: str (how to fill this section)
        current_data: str (current value for context, if applicable)
        user_content: str (what the user said)

    Returns None if target doesn't need LLM processing (e.g., remove_step).
    """
    entry = _FORM_MAP.get(target)
    if entry is None:
        return None

    form_class, needs_step_data = entry
    instructions = _INSTRUCTIONS.get(target, "Fill in the requested section.")

    current_data = ""
    if target == "title":
        current_data = f"Current title: {spec.title or '(not set)'}"
    elif target == "goal":
        current_data = f"Current goal: {spec.goal or '(not set)'}"
    elif target == "completion":
        current_data = f"Current completion: {spec.completion or '(not set)'}"
    elif target == "description":
        current_data = f"Current description: {spec.description or '(not set)'}"
    elif target == "edit_step" and needs_step_data:
        if 0 <= step_index < len(spec.steps):
            step = spec.steps[step_index]
            current_data = (
                f"Current step {step_index}:\n"
                f"  Name: {step.name}\n"
                f"  Description: {step.description}"
            )
        else:
            current_data = f"Step index {step_index} is out of range (0-{len(spec.steps)-1})."
    elif target == "add_step":
        if spec.steps:
            existing = ", ".join(f'"{s.name}"' for s in spec.steps)
            current_data = f"Existing steps: {existing}"

    return {
        "form_class": form_class,
        "instructions": instructions,
        "current_data": current_data,
        "user_content": user_content,
    }


def apply_form_result(
    *,
    target: str,
    spec: TaskSpec,
    step_index: int,
    form_data: Dict[str, Any],
) -> None:
    """Apply the filled form data back to the TaskSpec."""
    from app.assistant.lib.task_utils.task_spec_model import KnownInput, TaskStep

    if target == "title":
        spec.title = str(form_data.get("title") or "").strip()
    elif target == "goal":
        spec.goal = str(form_data.get("goal") or "").strip()
    elif target == "completion":
        spec.completion = str(form_data.get("completion") or "").strip()
    elif target == "description":
        spec.description = str(form_data.get("description") or "").strip()
    elif target == "add_step":
        name = str(form_data.get("step_name") or "").strip()
        desc = str(form_data.get("step_description") or "").strip()
        if name:
            new_step = TaskStep(name=name, description=desc)
            # step_index is the step number to insert AFTER (0-based).
            # -1 or out of range = append to end.
            if 0 <= step_index < len(spec.steps):
                spec.steps.insert(step_index + 1, new_step)
            else:
                spec.steps.append(new_step)
            if spec.stage == "draft":
                spec.stage = "steps_defined"
    elif target == "edit_step":
        if 0 <= step_index < len(spec.steps):
            step = spec.steps[step_index]
            name = str(form_data.get("step_name") or "").strip()
            desc = str(form_data.get("step_description") or "").strip()
            if name:
                step.name = name
            if desc:
                step.description = desc
    elif target == "add_input":
        input_id = str(form_data.get("input_id") or "").strip()
        kind = str(form_data.get("input_kind") or "fact").strip()
        desc = str(form_data.get("input_description") or "").strip()
        value = str(form_data.get("input_value") or "").strip() or None
        if input_id and kind in ("fact", "artifact"):
            spec.known_inputs.append(KnownInput(
                id=input_id, kind=kind, description=desc, value=value,
            ))
