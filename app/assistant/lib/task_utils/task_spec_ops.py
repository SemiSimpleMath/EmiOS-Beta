"""
Task spec operations — apply structured edits to a TaskSpec.

Each operation is a targeted mutation. No search/replace, no markdown
parsing. The spec writer agent emits SpecEdits, and this module
applies them to the live TaskSpec object.
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.assistant.lib.task_utils.task_spec_model import (
    KnownInput,
    TaskSpec,
    TaskStep,
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def apply_edits(spec: TaskSpec, edits: List[Dict[str, Any]]) -> TaskSpec:
    """Apply a list of SpecEdit dicts to a TaskSpec. Returns the mutated spec."""
    for i, edit in enumerate(edits):
        if not isinstance(edit, dict):
            logger.warning("apply_edits: edit[%d] is not a dict, skipping.", i)
            continue
        op = str(edit.get("op") or "").strip()
        try:
            _apply_one(spec, op, edit)
        except Exception as e:
            logger.error("apply_edits: edit[%d] op=%s failed: %s", i, op, e)
            logger.debug("apply_edits exception details", exc_info=True)
    return spec


def _apply_one(spec: TaskSpec, op: str, edit: Dict[str, Any]) -> None:
    value = str(edit.get("value") or "").strip()

    if op == "set_title":
        if not value:
            raise ValueError("set_title requires a non-empty value.")
        spec.title = value

    elif op == "set_goal":
        if not value:
            raise ValueError("set_goal requires a non-empty value.")
        spec.goal = value

    elif op == "set_completion":
        spec.completion = value

    elif op == "set_description":
        spec.description = value

    elif op == "add_step":
        name = str(edit.get("step_name") or "").strip()
        desc = str(edit.get("step_description") or "").strip()
        if not name:
            raise ValueError("add_step requires a non-empty step_name.")
        step = TaskStep(name=name, description=desc)
        idx = int(edit.get("step_index", -1))
        if idx < 0 or idx >= len(spec.steps):
            spec.steps.append(step)
        else:
            spec.steps.insert(idx, step)
        # Update stage if we now have steps.
        if spec.stage == "draft" and spec.steps:
            spec.stage = "steps_defined"

    elif op == "edit_step":
        idx = int(edit.get("step_index", -1))
        if idx < 0 or idx >= len(spec.steps):
            raise ValueError(f"edit_step: step_index {idx} out of range (0-{len(spec.steps)-1}).")
        step = spec.steps[idx]
        name = str(edit.get("step_name") or "").strip()
        desc = str(edit.get("step_description") or "").strip()
        if name:
            step.name = name
        if desc:
            step.description = desc

    elif op == "remove_step":
        idx = int(edit.get("step_index", -1))
        if idx < 0 or idx >= len(spec.steps):
            raise ValueError(f"remove_step: step_index {idx} out of range.")
        removed = spec.steps.pop(idx)
        logger.info("apply_edits: removed step '%s' at index %d.", removed.name, idx)

    elif op == "reorder_steps":
        new_order = edit.get("new_order")
        if not isinstance(new_order, list):
            raise ValueError("reorder_steps requires new_order as list of indices.")
        if sorted(new_order) != list(range(len(spec.steps))):
            raise ValueError(f"reorder_steps: new_order must be a permutation of 0..{len(spec.steps)-1}.")
        spec.steps = [spec.steps[i] for i in new_order]

    elif op == "add_input":
        input_id = str(edit.get("input_id") or "").strip()
        input_kind = str(edit.get("input_kind") or "fact").strip()
        input_desc = str(edit.get("input_description") or "").strip()
        input_val = str(edit.get("input_value") or "").strip() or None
        if not input_id:
            raise ValueError("add_input requires a non-empty input_id.")
        if input_kind not in ("fact", "artifact"):
            raise ValueError(f"add_input: input_kind must be 'fact' or 'artifact', got '{input_kind}'.")
        # Check for duplicates.
        if any(inp.id == input_id for inp in spec.known_inputs):
            logger.warning("apply_edits: input '%s' already exists, skipping add.", input_id)
            return
        spec.known_inputs.append(KnownInput(
            id=input_id,
            kind=input_kind,
            description=input_desc,
            value=input_val,
        ))

    elif op == "remove_input":
        input_id = value or str(edit.get("input_id") or "").strip()
        if not input_id:
            raise ValueError("remove_input requires input_id.")
        spec.known_inputs = [inp for inp in spec.known_inputs if inp.id != input_id]

    else:
        raise ValueError(f"Unknown spec edit op: {op!r}")
