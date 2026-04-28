from __future__ import annotations

from typing import Any

from app.assistant.utils.pydantic_classes import ToolResult

ALLOWED_ABORT_POLICIES = {"abort_task", "abort_tool", "retryable"}


def make_tool_error(
    *,
    error_code: str,
    message: str,
    abort_policy: str = "abort_tool",
    retryable: bool = False,
    user_visible: bool = True,
    details: dict[str, Any] | None = None,
) -> ToolResult:
    if not isinstance(error_code, str) or not error_code.strip():
        raise ValueError("error_code must be a non-empty string.")
    if abort_policy not in ALLOWED_ABORT_POLICIES:
        raise ValueError(
            f"abort_policy must be one of {sorted(ALLOWED_ABORT_POLICIES)}; got {abort_policy!r}."
        )
    payload: dict[str, Any] = {
        "error_code": error_code.strip(),
        "abort_policy": abort_policy,
        "retryable": bool(retryable),
        "user_visible": bool(user_visible),
    }
    if isinstance(details, dict) and details:
        payload["details"] = details
    return ToolResult(result_type="error", content=str(message), data=payload)


def validate_tool_error_contract(tool_result: ToolResult) -> tuple[bool, str]:
    if getattr(tool_result, "result_type", None) != "error":
        return True, ""
    data = getattr(tool_result, "data", None)
    if not isinstance(data, dict):
        return False, "Tool error contract invalid: data must be a dict."

    error_code = data.get("error_code")
    abort_policy = data.get("abort_policy")
    retryable = data.get("retryable")
    user_visible = data.get("user_visible")

    if not isinstance(error_code, str) or not error_code.strip():
        return False, "Tool error contract invalid: missing non-empty data.error_code."
    if abort_policy not in ALLOWED_ABORT_POLICIES:
        return (
            False,
            f"Tool error contract invalid: data.abort_policy must be one of {sorted(ALLOWED_ABORT_POLICIES)}.",
        )
    if not isinstance(retryable, bool):
        return False, "Tool error contract invalid: data.retryable must be boolean."
    if not isinstance(user_visible, bool):
        return False, "Tool error contract invalid: data.user_visible must be boolean."
    return True, ""
