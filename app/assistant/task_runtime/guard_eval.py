"""Guard evaluation for the task runner.

`ConditionEvaluator` is a pure, safe AST evaluator (names / constants / attribute+subscript /
and·or·not / comparisons / in — NO calls). It is the task runtime's OWN condition evaluator
(seeded from the now-removed task-IR evaluator; re-deriving a safe-eval subset from scratch
invites subtle bugs, so we copied proven code and own it — nothing is shared between them).

A node's *guard* (in `payload.guard`) is a crisp predicate over recorded facts; `facts_context`
builds `{data_id: value}` from the work object's evidence nodes. A genuinely SEMANTIC decision is
a judge node that records a fact, which a crisp guard then reads — so guard evaluation stays pure
and synchronous.
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
from typing import Any

_MISSING = object()


class MissingFact(ValueError):
    """The expression references a fact not (yet) recorded — legitimately unknowable NOW.
    eval_expr maps this to 'indeterminate' (the node stays pending)."""


class UnsupportedGuard(ValueError):
    """The expression itself is malformed or outside the safe-eval subset (syntax error,
    unsupported operator, type-mismatched comparison). Never 'not yet' — always a defect in
    the compiled expression, so callers fail the NODE loudly instead of stalling forever."""


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
                raise MissingFact(f"Condition references unknown name '{node.id}'.")
            return value
        if isinstance(node, ast.Attribute):
            base = self._eval_node(node.value, context)
            if not isinstance(base, dict):
                raise UnsupportedGuard(f"Condition attribute access requires object; got {type(base)}.")
            if node.attr not in base:
                raise MissingFact(f"Condition references missing attribute '{node.attr}'.")
            return base[node.attr]
        if isinstance(node, ast.Subscript):
            base = self._eval_node(node.value, context)
            idx = self._eval_node(node.slice, context)
            if isinstance(base, dict):
                if idx not in base:
                    raise MissingFact(f"Condition references missing key '{idx}'.")
                return base[idx]
            if isinstance(base, list):
                if not isinstance(idx, int):
                    raise UnsupportedGuard("Condition list subscript must be integer.")
                if idx < 0 or idx >= len(base):
                    raise MissingFact(f"Condition list index out of range: {idx}.")
                return base[idx]
            raise UnsupportedGuard(f"Unsupported subscript base type in condition: {type(base)}.")
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
            raise UnsupportedGuard("Unsupported boolean operator")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not bool(self._eval_node(node.operand, context))
        if isinstance(node, ast.Compare):
            left = self._eval_node(node.left, context)
            for op, comparator_node in zip(node.ops, node.comparators):
                right = self._eval_node(comparator_node, context)
                try:
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
                        raise UnsupportedGuard("Unsupported comparison operator")
                except TypeError as e:
                    # e.g. `count > 2` against a string fact — a defect in the expression
                    # or the fact's type, never "not yet": loud, node-scoped.
                    raise UnsupportedGuard(f"Type-mismatched comparison: {e}") from e
                if not ok:
                    return False
                left = right
            return True
        raise UnsupportedGuard(f"Unsupported condition expression node: {node.__class__.__name__}")


_EVAL = ConditionEvaluator()


def facts_context(wo) -> dict[str, Any]:
    """{data_id: value} from the work object's evidence nodes tagged `payload.data_id`.

    Prefers the structured `payload.value`; falls back to the node's `content`. This is the
    #3 out_fact convention — a producer node writes an evidence child keyed by its produced
    data_id, and guards / ${data_id} substitution read from here."""
    ctx: dict[str, Any] = {}
    events: list[Any] = []
    for n in wo.nodes.values():
        if n.type != "evidence":
            continue
        did = n.payload.get("data_id")
        if not did:
            continue
        if str(did) == "event_observed":   # each fired event is one evidence; aggregate to a list
            events.append(n.payload.get("value", n.content))
            continue
        ctx[str(did)] = n.payload.get("value", n.content)
    if events:
        ctx["events_observed"] = events
    # Intrinsics the compile contract allows in conditions (task_compile_post_node's
    # intrinsic_fact_keys) — supplied at evaluation time, never recorded as evidence.
    now_local = datetime.now().astimezone()
    ctx.setdefault("time_local_hhmm", now_local.strftime("%H:%M"))
    ctx.setdefault("time_utc_iso", datetime.now(timezone.utc).isoformat())
    return ctx


def eval_expr(expr, facts: dict[str, Any]):
    """Evaluate a raw condition expression against `facts`.

    Returns None (no expr) | True | False | 'indeterminate' (the expr references a fact not yet
    recorded — MissingFact). A malformed/unsupported expression raises UnsupportedGuard (incl.
    SyntaxError and type-mismatched comparisons) so the caller fails the NODE loudly instead of
    stalling forever on an expression that could never resolve. Shared by guards (payload.guard),
    loop done-conditions (payload.done_when), and release conditions."""
    if not expr:
        return None
    try:
        return bool(_EVAL.evaluate_condition(condition=str(expr), context=facts))
    except MissingFact:
        return "indeterminate"
    except SyntaxError as e:
        raise UnsupportedGuard(f"Condition does not parse: {expr!r}: {e}") from e


_ALLOWED_AST_NODES = (
    ast.Expression, ast.Constant, ast.Name, ast.Attribute, ast.Subscript,
    ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.Compare,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Load,
)


def validate_expression(expr: str) -> None:
    """Static build-time check: the expression parses and uses only the safe-eval subset.
    Raises UnsupportedGuard otherwise — so an unrunnable condition fails the COMPILE, not a
    live run three days later. (Missing facts are fine — they're runtime state.)"""
    try:
        tree = ast.parse(str(expr), mode="eval")
    except SyntaxError as e:
        raise UnsupportedGuard(f"Condition does not parse: {expr!r}: {e}") from e
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_AST_NODES):
            raise UnsupportedGuard(
                f"Condition uses unsupported syntax ({node.__class__.__name__}): {expr!r}")


def expression_names(expr: str) -> set[str]:
    """The bare fact names an expression reads — used by build-time termination checks
    (a no-wait loop's done_when must reference a fact the loop itself produces)."""
    try:
        tree = ast.parse(str(expr), mode="eval")
    except SyntaxError as e:
        raise UnsupportedGuard(f"Condition does not parse: {expr!r}: {e}") from e
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def guard_value(node, facts: dict[str, Any]):
    """A node's readiness guard (payload.guard): None (no guard) | True | False | 'indeterminate'."""
    return eval_expr(node.payload.get("guard"), facts)
