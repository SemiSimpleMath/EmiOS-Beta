"""Deterministic tool-node executor for the task runner.

Executes an already-claimed (`dispatched`) `tool` node's `payload.tools` in order, directly through
the tool registry — but, unlike the former task-IR tool-sequence executor, EACH call is GATED by
`check_tool_access` (the authority floor + task/scope restriction the former tool_sequence executor
bypassed) and carries the run's `scope_context`. The final result is recorded as an `evidence` node
keyed by the node's produced `data_id`(s); then the node is closed. Any error fails the node so
work_repair can adjudicate.

Arg substitution (`${data_id}` from recorded facts, `${now}`/`${today}`/`${hours_ago_N}`/
`${days_from_now_N}` system vars) was copied from the former task-IR tool-sequence executor (since
removed) — morning_briefing depends on it — and owned here.
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
# Per-loop-node iteration backstop (overridable via payload.max_iterations). Bounds a loop whose
# done_when can never become true to N real executions failing LOUDLY — not _MAX_WAVES=500 silent
# re-runs of side-effecting tools (verification finding, probe: 500 executions before stall).
_LOOP_MAX_ITERATIONS = 50


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
            args = _substitute_args(_coerce_args(spec), facts, last,
                                    text_args=_declared_text_args(name))
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

        write_produced_output(store, work_id, node, last,
                              tool_name=str((node.payload.get("tools") or [{}])[-1].get("tool") or ""))
        if node.payload.get("is_loop"):
            _record_loop_iteration(store, work_id, node_id)
        if _rearm_if_incomplete(store, work_id, node_id):
            return   # loop: the node's done-condition isn't met yet -> re-armed, runs again
        store.apply("set_status", {"reason": "task runner: tool node result recorded",
                                   "work_id": work_id, "node_id": node_id, "status": "done"}, actor="task_runner")
        store.apply("set_status", {"reason": "task runner: tool node result recorded",
                                   "work_id": work_id, "node_id": node_id, "status": "closed"}, actor="task_runner")
        if node.payload.get("is_end"):     # reach-end completion (not all-children-done)
            store.apply("set_work_status", {"work_id": work_id, "status": "done",
                                            "reason": "task runner: goal satisfied"}, actor="task_runner")
    except Exception as e:
        logger.error("[task_runner] tool node %s::%s failed: %s", work_id, node_id, e)
        store.apply("set_status", {"reason": "task runner: tool node result recorded",
                                   "work_id": work_id, "node_id": node_id, "status": "failed",
                                   "content": str(e)[:500]}, actor="task_runner")


def _loop_marker(node_id: str) -> str:
    return f"__loop_runs::{node_id}"


def _record_loop_iteration(store, work_id: str, node_id: str) -> None:
    """One evidence marker per completed loop execution — the iteration count backing both the
    per-loop cap and the missed-delivery check at re-arm."""
    store.apply("add_node", {
        "work_id": work_id, "id": new_id("ev"), "type": "evidence", "parent_id": node_id,
        "status": "assumed", "title": "loop iteration",
        "payload": {"data_id": _loop_marker(node_id), "value": 1},
    }, actor="task_runner")


def _loop_iterations(wo, node_id: str) -> int:
    marker = _loop_marker(node_id)
    return sum(1 for n in wo.nodes.values()
               if n.type == "evidence" and n.payload.get("data_id") == marker)


def _rearm_if_incomplete(store, work_id: str, node_id: str) -> bool:
    """Loop = re-arm: a node with `payload.done_when` re-runs until that condition holds over the recorded
    facts. If it's not yet satisfied, re-arm the node and return True so the caller skips closing — the
    drive loop picks it up again. A cyclic template becomes a linear trace of runs; the node only
    terminalizes when done_when is met.

    Two verification fixes live here:
    - iteration CAP: a loop that hits max_iterations raises (node fails LOUDLY via the caller's
      except) instead of burning _MAX_WAVES side-effecting executions toward a silent stall.
    - missed delivery (lost wakeup): an event that arrived WHILE this iteration was dispatched was
      recorded but couldn't promote (promotion targets `waiting` only). If subscribed deliveries
      outnumber completed iterations, re-arm to `actionable` — one owed iteration — instead of
      parking on a promotion that already happened."""
    from app.assistant.task_runtime.guard_eval import eval_expr
    node = store.load(work_id).nodes[node_id]
    done_when = node.payload.get("done_when")
    if not done_when:
        return False
    wo = store.load(work_id)
    facts = facts_context(wo)
    if eval_expr(done_when, facts) is True:   # UnsupportedGuard propagates -> node failed, loud
        return False   # done -> caller closes

    if node.payload.get("is_loop"):
        iterations = _loop_iterations(wo, node_id)
        cap = int(node.payload.get("max_iterations") or _LOOP_MAX_ITERATIONS)
        if iterations >= cap:
            raise RuntimeError(
                f"loop node {node_id} hit its iteration cap ({iterations}/{cap}) with done_when "
                f"still unmet ({str(done_when)[:120]!r}) — failing loudly instead of spinning")
        subs = [str(s) for s in (node.payload.get("subscriptions") or [])]
        if subs and node.wake_kind in ("event", "signal", "user_reply"):
            delivered = sum(1 for ev in (facts.get("events_observed") or []) if str(ev) in subs)
            if delivered > iterations:
                store.apply("set_status", {"work_id": work_id, "node_id": node_id,
                                           "status": "actionable"}, actor="task_runner")
                return True

    store.apply("set_status", {"reason": "task runner: tool node result recorded",
                                   "work_id": work_id, "node_id": node_id, "status": "waiting"}, actor="task_runner")
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


def _declared_text_args(tool_name: str) -> set:
    """Argument names the tool's own contract declares as text.

    A `${fact}` placeholder always sits inside a JSON string in `args_json` — JSON cannot
    express an unquoted substitution — so a structured fact resolved whole into one of these
    has to be serialised. Handing `write_text_file` a dict for its declared-string `content`
    is what killed three morning_briefing runs on 2026-09-13, reported as
    "Missing required argument: content" for an argument that was present but the wrong type.
    Arguments a tool declares as `object` still receive the structure untouched.
    """
    cfg = DI.tool_registry.get_tool(tool_name) or {}
    contract = cfg.get("tool_contract") or {}
    return {str(i.get("name")) for i in (contract.get("inputs") or [])
            if str(i.get("type") or "").strip().lower() in ("string", "text")}


def _declared_payload_key(tool_name: str) -> str:
    """The single `data.<key>` object a tool's contract declares as its payload, if any.

    Some tools return a provenance ENVELOPE in `data` rather than their result:
    `invoke_agent` declares `data.agent_output` as the structured output and puts
    `data.agent_name` beside it. Recording the envelope as the artifact stored the wrapper —
    so the briefing file would have carried `agent_name` around the actual briefing — and
    obscured what downstream steps consume. Driven entirely by the declared contract; a tool
    that declares no such payload keeps `data` as-is.
    """
    cfg = DI.tool_registry.get_tool(tool_name) or {}
    contract = cfg.get("tool_contract") or {}
    keys = [str(o.get("path"))[len("data."):] for o in (contract.get("outputs") or [])
            if str(o.get("path") or "").startswith("data.")
            and str(o.get("type") or "").strip().lower() == "object"]
    return keys[0] if len(keys) == 1 else ""


def _payload_of(tool_name: str, last: ToolResult) -> Any:
    key = _declared_payload_key(tool_name)
    data = last.data if isinstance(last.data, dict) else None
    if key and data and key in data:
        return data[key]
    return data if data else (last.content or "")


def write_produced_output(store, work_id: str, node, last: ToolResult | None,
                          tool_name: str = "") -> None:
    """Record the node's produced output as evidence child node(s) keyed by data_id (the #3
    convention). Shared with the action-node path, whose `produces` was previously dropped."""
    produces = node.payload.get("produces") or []
    if not produces or last is None:
        return
    value: Any = _payload_of(tool_name, last)
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
        except json.JSONDecodeError as e:
            # A compiler-emitted broken args_json must fail the node loudly — running the
            # tool with silently-emptied args is exactly the swallow CLAUDE.md bans.
            raise ValueError(f"tool spec has unparseable args_json {raw[:200]!r}: {e}") from e
    if not isinstance(args, dict):
        raise ValueError(f"tool spec args must be an object, got {type(args).__name__}")
    return dict(args)


# --- arg substitution (copied from the former task-IR tool-sequence executor; owned here) ---

def _substitute_args(args: dict[str, Any], facts: dict[str, Any], last: ToolResult | None,
                     text_args: set | None = None) -> dict[str, Any]:
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

    out: dict[str, Any] = {}
    for key, raw in args.items():
        value = _resolve(raw)
        # The tool declared this argument as text, so give it text. Only whole-value
        # substitutions can arrive structured — a placeholder embedded in a larger string is
        # already serialised above — and an argument declared `object` is left untouched.
        if text_args and key in text_args and value is not None and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        out[key] = value
    return out


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
