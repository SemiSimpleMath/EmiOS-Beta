"""Guard evaluation for the task runner.

`ConditionEvaluator` is a pure, safe AST evaluator (names / constants / attribute+subscript /
and·or·not / comparisons / in — NO calls). It is the task runtime's OWN copy of the condition
evaluator (seeded from task_ir_runtime, which is retired at the Phase-5 cutover; re-deriving a
safe-eval subset from scratch invites subtle bugs, so we copy proven code and own it — the
original is never imported: nothing shared lives between old and new).

A node's *guard* (in `payload.guard`) is a crisp predicate over recorded facts; `facts_context`
builds `{data_id: value}` from the work object's evidence nodes. A genuinely SEMANTIC decision is
a judge node that records a fact, which a crisp guard then reads — so guard evaluation stays pure
and synchronous.
"""
from __future__ import annotations

import ast
from typing import Any

_MISSING = object()


class ConditionEvaluator:
    def evaluate_condition(self, *, condition: str, context: dict[str, Any]) -> bool:
        parsed = ast.parse(condition, mode="eval")
        return bool(self._eval_node(parsed.body, context))

    def _eval_node(self, node: ast.AST, context: dict[str, Any]) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            value = context.get(node.id, _MISSING)
            if value is _MISSING:
                raise ValueError(f"Condition references unknown name '{node.id}'.")
            return value
        if isinstance(node, ast.Attribute):
            base = self._eval_node(node.value, context)
            if not isinstance(base, dict):
                raise ValueError(f"Condition attribute access requires object; got {type(base)}.")
            if node.attr not in base:
                raise ValueError(f"Condition references missing attribute '{node.attr}'.")
            return base[node.attr]
        if isinstance(node, ast.Subscript):
            base = self._eval_node(node.value, context)
            idx = self._eval_node(node.slice, context)
            if isinstance(base, dict):
                if idx not in base:
                    raise ValueError(f"Condition references missing key '{idx}'.")
                return base[idx]
            if isinstance(base, list):
                if not isinstance(idx, int):
                    raise ValueError("Condition list subscript must be integer.")
                if idx < 0 or idx >= len(base):
                    raise ValueError(f"Condition list index out of range: {idx}.")
                return base[idx]
            raise ValueError(f"Unsupported subscript base type in condition: {type(base)}.")
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                for value_node in node.values:
                    if not bool(self._eval_node(value_node, context)):
                        return False
                return True
            if isinstance(node.op, ast.Or):
                for value_node in node.values:
                    if bool(self._eval_node(value_node, context)):
                        return True
                return False
            raise ValueError("Unsupported boolean operator")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not bool(self._eval_node(node.operand, context))
        if isinstance(node, ast.Compare):
            left = self._eval_node(node.left, context)
            for op, comparator_node in zip(node.ops, node.comparators):
                right = self._eval_node(comparator_node, context)
                if isinstance(op, ast.Eq):
                    ok = left == right
                elif isinstance(op, ast.NotEq):
                    ok = left != right
                elif isinstance(op, ast.Lt):
                    ok = left < right
                elif isinstance(op, ast.LtE):
                    ok = left <= right
                elif isinstance(op, ast.Gt):
                    ok = left > right
                elif isinstance(op, ast.GtE):
                    ok = left >= right
                elif isinstance(op, ast.In):
                    ok = left in right
                elif isinstance(op, ast.NotIn):
                    ok = left not in right
                else:
                    raise ValueError("Unsupported comparison operator")
                if not ok:
                    return False
                left = right
            return True
        raise ValueError(f"Unsupported condition expression node: {node.__class__.__name__}")


_EVAL = ConditionEvaluator()


def facts_context(wo) -> dict[str, Any]:
    """{data_id: value} from the work object's evidence nodes tagged `payload.data_id`.

    Prefers the structured `payload.value`; falls back to the node's `content`. This is the
    #3 out_fact convention — a producer node writes an evidence child keyed by its produced
    data_id, and guards / ${data_id} substitution read from here."""
    ctx: dict[str, Any] = {}
    for n in wo.nodes.values():
        if n.type != "evidence":
            continue
        did = n.payload.get("data_id")
        if did:
            ctx[str(did)] = n.payload.get("value", n.content)
    return ctx


def eval_expr(expr, facts: dict[str, Any]):
    """Evaluate a raw condition expression against `facts`.

    Returns None (no expr) | True | False | 'indeterminate' (the expr references a fact not yet
    recorded). Shared by guards (payload.guard) and loop done-conditions (payload.done_when)."""
    if not expr:
        return None
    try:
        return bool(_EVAL.evaluate_condition(condition=str(expr), context=facts))
    except ValueError:
        return "indeterminate"


def guard_value(node, facts: dict[str, Any]):
    """A node's readiness guard (payload.guard): None (no guard) | True | False | 'indeterminate'."""
    return eval_expr(node.payload.get("guard"), facts)
