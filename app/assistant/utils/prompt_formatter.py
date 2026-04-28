"""
Universal formatter for converting structured data into LLM-friendly prompt text.

Converts dicts, lists, and nested structures into clean indented key-value
format that uses fewer tokens and is easier for models to parse than JSON.

Usage:
    from app.assistant.utils.prompt_formatter import format_for_prompt

    text = format_for_prompt({"name": "CNN", "headlines": ["h1", "h2"]})
    # name: CNN
    # headlines:
    #   - h1
    #   - h2
"""
from __future__ import annotations

from typing import Any


def format_for_prompt(data: Any, *, indent: int = 0, max_depth: int = 6) -> str:
    """Convert structured data to clean indented text for LLM prompts.

    Rules:
    - Dicts become indented key: value pairs
    - Lists of scalars become "- item" bullets
    - Lists of dicts become numbered or separated blocks
    - Strings pass through as-is
    - None/empty values are omitted
    - No JSON syntax (no braces, brackets, quotes, commas)
    """
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if isinstance(data, (int, float, bool)):
        return str(data)
    if isinstance(data, dict):
        return _format_dict(data, indent=indent, depth=0, max_depth=max_depth)
    if isinstance(data, (list, tuple)):
        return _format_list(data, indent=indent, depth=0, max_depth=max_depth)
    return str(data)


def _format_dict(d: dict, *, indent: int, depth: int, max_depth: int) -> str:
    if depth >= max_depth:
        return _indent(str(d), indent)

    lines = []
    for key, value in d.items():
        if value is None or value == "" or value == []:
            continue
        key_str = str(key).replace("_", " ")
        if isinstance(value, str):
            if "\n" in value:
                # Multi-line string: put on next line indented
                lines.append(_indent(f"{key_str}:", indent))
                for vline in value.split("\n"):
                    lines.append(_indent(vline, indent + 2))
            else:
                lines.append(_indent(f"{key_str}: {value}", indent))
        elif isinstance(value, (int, float, bool)):
            lines.append(_indent(f"{key_str}: {value}", indent))
        elif isinstance(value, dict):
            lines.append(_indent(f"{key_str}:", indent))
            lines.append(_format_dict(value, indent=indent + 2, depth=depth + 1, max_depth=max_depth))
        elif isinstance(value, (list, tuple)):
            lines.append(_indent(f"{key_str}:", indent))
            lines.append(_format_list(value, indent=indent + 2, depth=depth + 1, max_depth=max_depth))
        else:
            lines.append(_indent(f"{key_str}: {value}", indent))
    return "\n".join(lines)


def _format_list(lst: list | tuple, *, indent: int, depth: int, max_depth: int) -> str:
    if depth >= max_depth:
        return _indent(str(lst), indent)

    if not lst:
        return _indent("(none)", indent)

    # Check if all items are scalars (strings, numbers, bools)
    all_scalar = all(isinstance(item, (str, int, float, bool)) for item in lst)

    if all_scalar:
        lines = []
        for item in lst:
            lines.append(_indent(f"- {item}", indent))
        return "\n".join(lines)

    # List of dicts or mixed — format each as a block with separators
    blocks = []
    for i, item in enumerate(lst):
        if isinstance(item, dict):
            block = _format_dict(item, indent=indent, depth=depth + 1, max_depth=max_depth)
            blocks.append(block)
        elif isinstance(item, (list, tuple)):
            block = _format_list(item, indent=indent + 2, depth=depth + 1, max_depth=max_depth)
            blocks.append(_indent("- ", indent) + block.lstrip())
        else:
            blocks.append(_indent(f"- {item}", indent))

    return "\n\n".join(blocks)


def _indent(text: str, spaces: int) -> str:
    if spaces <= 0:
        return text
    prefix = " " * spaces
    return prefix + text
