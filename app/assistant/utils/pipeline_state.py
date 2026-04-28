from __future__ import annotations

from typing import Any, Dict, Optional

from app.assistant.utils.pydantic_classes import PipelineState, PipelineToolOp


def ensure_pipeline_state(blackboard) -> Dict[str, Any]:
    """
    Ensure a PipelineState dict exists on the global scope.
    """
    ps = None
    try:
        ps = blackboard.get_state_value("pipeline_state")
    except Exception:
        ps = None

    if isinstance(ps, PipelineState):
        ps = ps.model_dump()
    if not isinstance(ps, dict):
        ps = PipelineState().model_dump()
        try:
            blackboard.update_global_state_value("pipeline_state", ps)
        except Exception:
            blackboard.update_state_value("pipeline_state", ps)
    return ps


def set_pipeline_state(blackboard, ps: Dict[str, Any]) -> None:
    try:
        blackboard.update_global_state_value("pipeline_state", ps)
    except Exception:
        blackboard.update_state_value("pipeline_state", ps)


def get_pending_tool(blackboard) -> Optional[Dict[str, Any]]:
    ps = ensure_pipeline_state(blackboard)
    pending = ps.get("pending_tool")
    return pending if isinstance(pending, dict) else None


def set_pending_tool(
    blackboard,
    *,
    name: Optional[str],
    calling_agent: Optional[str],
    action_input: Optional[Any] = None,
    arguments: Optional[Dict[str, Any]] = None,
    kind: Optional[str] = None,
) -> None:
    ps = ensure_pipeline_state(blackboard)
    op = PipelineToolOp(
        name=name,
        arguments=arguments,
        action_input=action_input,
        calling_agent=calling_agent,
        kind=kind,
    ).model_dump()
    ps["pending_tool"] = op
    set_pipeline_state(blackboard, ps)


def clear_pending_tool(blackboard) -> None:
    ps = ensure_pipeline_state(blackboard)
    ps["pending_tool"] = None
    set_pipeline_state(blackboard, ps)


def set_pending_tool_arguments(blackboard, arguments: Dict[str, Any]) -> None:
    ps = ensure_pipeline_state(blackboard)
    pending = ps.get("pending_tool")
    if isinstance(pending, dict):
        pending["arguments"] = arguments
        ps["pending_tool"] = pending
        set_pipeline_state(blackboard, ps)


def set_last_tool_result_ref(blackboard, ref: Optional[Dict[str, str]], meta: Optional[Dict[str, Any]] = None) -> None:
    ps = ensure_pipeline_state(blackboard)
    ps["last_tool_result_ref"] = ref
    if isinstance(meta, dict):
        ps["last_tool_result_meta"] = meta
    set_pipeline_state(blackboard, ps)


def get_last_tool_result_ref(blackboard) -> Optional[Dict[str, str]]:
    ps = ensure_pipeline_state(blackboard)
    ref = ps.get("last_tool_result_ref")
    return ref if isinstance(ref, dict) else None


def get_last_tool_result_meta(blackboard) -> Optional[Dict[str, Any]]:
    ps = ensure_pipeline_state(blackboard)
    meta = ps.get("last_tool_result_meta")
    return meta if isinstance(meta, dict) else None


def set_resume_target(blackboard, target: Optional[str]) -> None:
    ps = ensure_pipeline_state(blackboard)
    ps["resume_target"] = target
    set_pipeline_state(blackboard, ps)


def get_resume_target(blackboard) -> Optional[str]:
    ps = ensure_pipeline_state(blackboard)
    target = ps.get("resume_target")
    return target if isinstance(target, str) and target.strip() else None


def set_flag(blackboard, key: str, value: Any) -> None:
    ps = ensure_pipeline_state(blackboard)
    flags = ps.get("flags") if isinstance(ps.get("flags"), dict) else {}
    flags[key] = value
    ps["flags"] = flags
    set_pipeline_state(blackboard, ps)


def get_flag(blackboard, key: str, default: Any = None) -> Any:
    ps = ensure_pipeline_state(blackboard)
    flags = ps.get("flags") if isinstance(ps.get("flags"), dict) else {}
    return flags.get(key, default)


def set_scratch(blackboard, key: str, value: Any) -> None:
    ps = ensure_pipeline_state(blackboard)
    scratch = ps.get("scratch") if isinstance(ps.get("scratch"), dict) else {}
    scratch[key] = value
    ps["scratch"] = scratch
    set_pipeline_state(blackboard, ps)


def get_scratch(blackboard, key: str, default: Any = None) -> Any:
    ps = ensure_pipeline_state(blackboard)
    scratch = ps.get("scratch") if isinstance(ps.get("scratch"), dict) else {}
    return scratch.get(key, default)
