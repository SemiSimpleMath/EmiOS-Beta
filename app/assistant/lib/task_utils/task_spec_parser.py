"""
Parse task spec markdown into a TaskSpec model.
"""
from __future__ import annotations

import re
from typing import List

from app.assistant.lib.task_utils.task_spec_model import TaskSpec, TaskStep, KnownInput
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def parse_spec_markdown(markdown: str) -> TaskSpec:
    """Parse a task spec markdown string into a TaskSpec object."""
    spec = TaskSpec()
    lines = markdown.splitlines()

    # Extract title from first # heading
    for line in lines:
        if line.startswith("# ") and not line.startswith("## "):
            spec.title = line[2:].strip()
            break

    # Extract sections by ## headings
    sections: dict[str, str] = {}
    current_section = ""
    current_lines: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if current_section:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = line[3:].strip().lower()
            current_lines = []
        elif current_section:
            current_lines.append(line)
    if current_section:
        sections[current_section] = "\n".join(current_lines).strip()

    # Title (from ## Title section if no # heading)
    if not spec.title:
        spec.title = sections.get("title", "")

    spec.description = sections.get("description", "")
    spec.goal = sections.get("goal", "")
    spec.completion = sections.get("completion", "")

    # Steps
    steps_text = sections.get("steps", "")
    if steps_text and steps_text != "[No steps defined yet]":
        spec.steps = _parse_steps(steps_text)

    # Known Inputs
    inputs_text = sections.get("known inputs", "")
    if inputs_text:
        spec.known_inputs = _parse_known_inputs(inputs_text)

    # Task ID from title
    if spec.title and spec.title not in ("[New Task]", "[TBD]"):
        spec.task_id = re.sub(r"[^a-z0-9]+", "_", spec.title.lower()).strip("_")

    return spec


def _parse_steps(steps_text: str) -> List[TaskStep]:
    """Parse numbered steps from markdown text."""
    # Split into step blocks by numbered lines (1. 2. 3. etc.)
    step_blocks = re.split(r"(?=^\d+\.\s)", steps_text, flags=re.MULTILINE)
    parsed = []
    for block in step_blocks:
        block = block.strip()
        if not block:
            continue
        first_line = block.splitlines()[0]
        match = re.match(r"^\d+\.\s+\**(.+?)\**(?:\s*→.*)?$", first_line)
        if match:
            name = match.group(1).strip()
            desc_lines = block.splitlines()[1:]
            description = "\n".join(
                line.strip().lstrip("- ") for line in desc_lines if line.strip()
            ).strip()
            parsed.append(TaskStep(name=name, description=description))
        elif re.match(r"^\d+\.\s", first_line):
            # Simple format: "1. Do something"
            name = re.sub(r"^\d+\.\s+", "", first_line).strip()
            name = name.strip("*")
            desc_lines = block.splitlines()[1:]
            description = "\n".join(
                line.strip().lstrip("- ") for line in desc_lines if line.strip()
            ).strip()
            parsed.append(TaskStep(name=name, description=description))
    return parsed


def _parse_known_inputs(inputs_text: str) -> List[KnownInput]:
    """Parse known inputs section."""
    parsed = []
    for line in inputs_text.splitlines():
        line = line.strip()
        if line.startswith("- "):
            line = line[2:]
            match = re.match(r"(\S+)\s+\((\w+)\)(?::\s*(.+?))?\s*—\s*(.+)", line)
            if match:
                parsed.append(KnownInput(
                    id=match.group(1),
                    kind=match.group(2),
                    description=match.group(4).strip(),
                    value=match.group(3).strip() if match.group(3) else None,
                ))
    return parsed
