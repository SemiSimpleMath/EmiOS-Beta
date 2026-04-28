from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Tuple, Type

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class McpPydanticModels:
    inner_args_model: Type
    outer_arguments_model: Type


def _safe_model_name(name: str) -> str:
    # Pydantic model name must be a valid identifier-ish; keep it readable.
    import re

    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if cleaned and cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    return cleaned or "Model"


def _schema_type(schema: dict[str, Any]) -> Any:
    t = schema.get("type")
    if isinstance(t, list):
        return t
    if isinstance(t, str):
        return [t]
    return []


def _is_nullable(schema: dict[str, Any]) -> bool:
    return "null" in _schema_type(schema)


def _type_for_schema(
    name_hint: str,
    schema: dict[str, Any],
    *,
    depth: int = 0,
) -> Tuple[Any, Any]:
    """
    Returns (python_type, default_value) for pydantic.create_model().
    This is intentionally conservative. Unsupported constructs become `Any`.
    """
    from typing import Any as TypingAny, Literal, Optional as TypingOptional

    # Enum -> Literal
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        lit = Literal[tuple(enum)]  # type: ignore[misc]
        if _is_nullable(schema):
            return (TypingOptional[lit], None)
        return (lit, ...)

    types = [t for t in _schema_type(schema) if t != "null"]
    t0 = types[0] if types else schema.get("type")

    # Basic scalars
    if t0 == "string":
        py_t: Any = str
        return ((TypingOptional[py_t], None) if _is_nullable(schema) else (py_t, ...))
    if t0 == "integer":
        py_t = int
        return ((TypingOptional[py_t], None) if _is_nullable(schema) else (py_t, ...))
    if t0 == "number":
        py_t = float
        return ((TypingOptional[py_t], None) if _is_nullable(schema) else (py_t, ...))
    if t0 == "boolean":
        py_t = bool
        return ((TypingOptional[py_t], None) if _is_nullable(schema) else (py_t, ...))

    # Arrays
    if t0 == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            item_type, _ = _type_for_schema(f"{name_hint}_item", items, depth=depth + 1)
        else:
            item_type = TypingAny
        py_t = list[item_type]  # type: ignore[valid-type]
        return ((TypingOptional[py_t], None) if _is_nullable(schema) else (py_t, ...))

    # Objects (nested)
    if t0 == "object" and depth < 6:
        props = schema.get("properties")
        if isinstance(props, dict):
            nested = _model_for_object_schema(_safe_model_name(f"{name_hint}_obj"), schema, depth=depth + 1)
            return ((TypingOptional[nested], None) if _is_nullable(schema) else (nested, ...))
        # free-form object
        py_t = dict[str, TypingAny]
        return ((TypingOptional[py_t], None) if _is_nullable(schema) else (py_t, ...))

    # anyOf/oneOf etc (fallback)
    return (TypingAny, None if _is_nullable(schema) else ...)


def _model_for_object_schema(model_name: str, schema: dict[str, Any], *, depth: int = 0) -> Type:
    """
    Create a Pydantic model for a JSON Schema object with properties.
    """
    from pydantic import ConfigDict, create_model

    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []

    fields: dict[str, tuple[Any, Any]] = {}
    for prop_name, prop_schema in props.items():
        if not isinstance(prop_schema, dict):
            continue

        py_t, default = _type_for_schema(prop_name, prop_schema, depth=depth)
        if prop_name not in required and default is ...:
            # Optional (not required) field -> default None if nullable, else optional None.
            from typing import Optional as TypingOptional, Any as TypingAny

            if py_t is TypingAny:
                fields[prop_name] = (TypingOptional[TypingAny], None)
            else:
                fields[prop_name] = (TypingOptional[py_t], None)  # type: ignore[valid-type]
        else:
            fields[prop_name] = (py_t, default)

    # For tool-argument generation we always want a *closed* schema.
    # OpenAI Structured Outputs requires object schemas to declare
    # `additionalProperties: false`, and we do not want the LLM inventing keys.
    extra_mode = "forbid"

    return create_model(
        model_name,
        __config__=ConfigDict(extra=extra_mode),
        **fields,
    )


def build_mcp_tool_models(
    *,
    namespaced_tool_name: str,
    input_schema: dict[str, Any] | None,
) -> McpPydanticModels:
    """
    Build two Pydantic models to match EmiAi's existing tool envelope:

    - inner_args_model: derived from MCP tool inputSchema (arguments only)
    - outer_arguments_model: { tool_name: <namespaced>, arguments: <inner> }
    """
    from typing import Literal
    from pydantic import ConfigDict, Field, create_model

    inner_schema = input_schema or {"type": "object", "properties": {}}
    if not isinstance(inner_schema, dict):
        inner_schema = {"type": "object", "properties": {}}

    inner_name = _safe_model_name(f"{namespaced_tool_name}_args")
    outer_name = _safe_model_name(f"{namespaced_tool_name}_arguments")

    # Build inner model from schema (best-effort)
    if inner_schema.get("type") == "object" or isinstance(inner_schema.get("properties"), dict):
        inner_model = _model_for_object_schema(inner_name, inner_schema, depth=0)
    else:
        # Non-object schema: treat arguments as Any
        inner_model = _model_for_object_schema(inner_name, {"type": "object", "properties": {}}, depth=0)

    # Outer envelope model mirrors existing tools: tool_name + arguments
    outer_model = create_model(
        outer_name,
        __config__=ConfigDict(extra="forbid"),
        tool_name=(Literal[namespaced_tool_name], Field(default=namespaced_tool_name)),
        arguments=(inner_model, Field(default_factory=dict)),
    )

    return McpPydanticModels(inner_args_model=inner_model, outer_arguments_model=outer_model)

