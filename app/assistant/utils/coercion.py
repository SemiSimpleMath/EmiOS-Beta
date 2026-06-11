"""Shared value-coercion helpers."""
from __future__ import annotations

from typing import Any


def coerce_authority_level(value: Any, *, field_name: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer between 0 and 100.")
    try:
        level = int(value)
    except Exception:
        raise ValueError(f"{field_name} must be an integer between 0 and 100.")
    if level < 0 or level > 100:
        raise ValueError(f"{field_name} must be between 0 and 100.")
    return level


def coerce_numeric(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field_name} must be numeric.")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError(f"{field_name} must be numeric.")
        normalized = raw.replace(",", "")
        if normalized.count(".") > 1:
            raise ValueError(f"{field_name} must be numeric.")
        if normalized.startswith("-"):
            normalized_digits = normalized[1:]
        else:
            normalized_digits = normalized
        if not normalized_digits or not normalized_digits.replace(".", "", 1).isdigit():
            raise ValueError(f"{field_name} must be numeric.")
        return float(normalized)
    raise ValueError(f"{field_name} must be numeric.")
