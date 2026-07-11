"""Deterministic tool-node executor for the task runner.

Executes an already-claimed (`dispatched`) `tool` node's `payload.tools` in order, directly through
the tool registry — but, unlike task_ir's tool-sequence executor, EACH call is GATED by
`check_tool_access` (the authority floor + task/scope restriction that task_ir's tool_sequence
bypassed) and carries the run's `scope_context`. The final result is recorded as an `evidence` node
keyed by the node's produced `data_id`(s); then the node is closed. Any error fails the node so
work_repair can adjudicate.

Arg substitution (`${data_id}` from recorded facts, `${now}`/`${today}`/`${hours_ago_N}`/
`${days_from_now_N}` system vars) is copied from task_ir's tool-sequence executor (retired at the
Phase-5 cutover) — morning_briefing depends on it — and owned here.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.tool_execution.tool_access_control import (
    check_tool_access,
    resolve_tool_min_authority,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from work_objects.model import new_id
from app.assistant.task_runtime.guard_eval import facts_context

logger = get_logger(__name__)

_ERROR_RESULT_TYPES = {"error", "tool_error", "manager_aborted"}


def execute_claimed_tool_node(store, work_id: str, node_id: str, scope,
                              scope_contract_enforced: bool = True) -> None:
    """Run an already-dispatched tool node to completion (close) or failure. The drive loop claims
    the node BEFORE spawning this (no double-dispatch), so this does not re-claim."""
    node = store.load(work_id).nodes[node_id]
    try:
        facts = facts_context(store.load(work_id))
        last: ToolResult | None = None
        for spec in (node.payload.get("tools") or []):
            if not isinstance(spec, dict):
                raise ValueError(f"tool node {node_id}: tool spec must be an object")
            name = str(spec.get("tool") or "").strip()
            if not name:
                raise ValueError(f"tool node {node_id}: tool spec missing 'tool'")
            args = _substitute_args(_coerce_args(spec), facts, last)
            _gate(name, scope, scope_contract_enforced)                    # #4 — the gate task_ir lacked
            tool_cls = DI.tool_registry.get_tool_class(name)
            if tool_cls is None:
                raise ValueError(f"tool node {node_id}: tool '{name}' not found in registry")
            result = tool_cls().execute(ToolMessage(
                tool_name=name,
                tool_data={"tool_name": name, "arguments": args},
                scope_context=scope,                                       # task_ir passed NO scope here
                content=f"task_tool:{node_id}:{name}",
            ))
            if not isinstance(result, ToolResult):
                raise RuntimeError(f"tool '{name}' returned non-ToolResult: {type(result).__name__}")
            rt = str(result.result_type or "").strip().lower()
            if rt in _ERROR_RESULT_TYPES or getattr(result, "error", False):
                raise RuntimeError(f"tool '{name}' errored: {str(result.content or '')[:300]}")
            last = result

        _write_output(store, work_id, node, last)
        if _rearm_if_incomplete(store, work_id, node_id):
            return   # loop: the node's done-condition isn't met yet -> re-armed (waiting), runs again
        store.apply("set_status", {"work_id": work_id, "node_id": node_id, "status": "done"}, actor="task_runner")
        store.apply("set_status", {"work_id": work_id, "node_id": node_id, "status": "closed"}, actor="task_runner")
        if node.payload.get("is_end"):     # reach-end completion (not all-children-done)
            store.apply("set_work_status", {"work_id": work_id, "status": "done"}, actor="task_runner")
    except Exception as e:
        logger.error("[task_runner] tool node %s::%s failed: %s", work_id, node_id, e)
        store.apply("set_status", {"work_id": work_id, "node_id": node_id, "status": "failed",
                                   "content": str(e)[:500]}, actor="task_runner")


def _rearm_if_incomplete(store, work_id: str, node_id: str) -> bool:
    """Loop = re-arm: a node with `payload.done_when` re-runs until that condition holds over the recorded
    facts. If it's not yet satisfied, set the node back to `waiting` (claimable) and return True so the
    caller skips closing — the drive loop picks it up again. A cyclic template becomes a linear trace of
    runs (each recorded as an event); the node only terminalizes when done_when is met."""
    from app.assistant.task_runtime.guard_eval import eval_expr
    node = store.load(work_id).nodes[node_id]
    done_when = node.payload.get("done_when")
    if not done_when:
        return False
    facts = facts_context(store.load(work_id))
    if eval_expr(done_when, facts) is True:
        return False   # done -> caller closes
    store.apply("set_status", {"work_id": work_id, "node_id": node_id, "status": "waiting"}, actor="task_runner")
    return True


def _gate(tool_name: str, scope, scope_contract_enforced: bool) -> None:
    """The authority/approval gate ToolCaller enforces — task_ir's tool_sequence skipped it entirely."""
    tool_cfg = DI.tool_registry.get_tool(tool_name)
    allowed, reason = check_tool_access(
        tool_name=tool_name,
        scope_contract_enforced=scope_contract_enforced,
        scope_context=scope,
        task_allowed_tools=None,
        task_except_tools=None,
        caller_name="task_runner",
        tool_min_authority=resolve_tool_min_authority(tool_name, tool_cfg),
    )
    if not allowed:
        raise PermissionError(reason)


def _write_output(store, work_id: str, node, last: ToolResult | None) -> None:
    """Record the node's produced output as evidence child node(s) keyed by data_id (the #3 convention)."""
    produces = node.payload.get("produces") or []
    if not produces or last is None:
        return
    value: Any = last.data if isinstance(last.data, dict) and last.data else (last.content or "")
    for data_id in produces:
        store.apply("add_node", {
            "work_id": work_id, "id": new_id("ev"), "type": "evidence", "parent_id": node.id,
            "status": "assumed", "title": str(data_id), "content": str(last.content or "")[:2000],
            "payload": {"data_id": str(data_id), "value": value},
        }, actor="task_runner")


def _coerce_args(spec: dict) -> dict:
    args = spec.get("args")
    if args is None:
        raw = str(spec.get("args_json") or "{}").strip()
        try:
            args = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            args = {}
    return dict(args) if isinstance(args, dict) else {}


# --- arg substitution (copied from the archived task_ir tool-sequence executor; owned here) ---

def _substitute_args(args: dict[str, Any], facts: dict[str, Any], last: ToolResult | None) -> dict[str, Any]:
    values = dict(facts)
    if last is not None:
        values["prev_result"] = last.content or ""
        values["prev_result_data"] = last.data if isinstance(last.data, dict) else {}
    system_vars = {
        "now": datetime.now(timezone.utc).isoformat(),
        "now_local": datetime.now().astimezone().isoformat(),
        "today": datetime.now().astimezone().strftime("%Y-%m-%d"),
    }
    merged = {**system_vars, **values}

    def _resolve(v: Any) -> Any:
        if isinstance(v, str):
            v = _resolve_dynamic_time_vars(v)
            for ref_key, ref_val in merged.items():
                placeholder = f"${{{ref_key}}}"
                if placeholder in v:
                    if v == placeholder:
                        return ref_val
                    v = v.replace(placeholder, ref_val if isinstance(ref_val, str)
                                  else json.dumps(ref_val, ensure_ascii=False))
            return v
        if isinstance(v, dict):
            return {dk: _resolve(dv) for dk, dv in v.items()}
        if isinstance(v, list):
            return [_resolve(item) for item in v]
        return v

    return {k: _resolve(v) for k, v in args.items()}


def _resolve_dynamic_time_vars(text: str) -> str:
    def _ago(m: re.Match) -> str:
        unit, n = m.group(1), int(m.group(2))
        delta = timedelta(hours=n) if unit == "hours" else timedelta(minutes=n)
        return (datetime.now(timezone.utc) - delta).isoformat()

    def _from_now(m: re.Match) -> str:
        unit, n = m.group(1), int(m.group(2))
        delta = timedelta(days=n) if unit == "days" else timedelta(hours=n) if unit == "hours" else timedelta(minutes=n)
        return (datetime.now(timezone.utc) + delta).isoformat()

    text = re.sub(r"\$\{(hours|minutes)_ago_(\d+)\}", _ago, text)
    text = re.sub(r"\$\{(days|hours|minutes)_from_now_(\d+)\}", _from_now, text)
    return text
