"""The task pipeline runner — the active drive loop.

Evaluates the work object's ready-set and drives it wave-by-wave with NO scheduler round-trip
between nodes: each pass fans the whole ready frontier out — in ONE parallel thread pool —
`tool` nodes via the deterministic tool executor and `action` nodes via the work-object SUBSTRATE
driver (`work_objects.discharge.discharge_node` → the node's manager), then re-evaluates. Tasks depend
on the substrate (work_objects/) and nothing in dayflow: dayflow is a separate consumer of the same
substrate, not a dependency. The runner parks when only future-wake nodes remain ("parked", resumed
when the wake fires) and completes on reach-end or when it runs out of runnable work.

Conditionals are in the graph as per-node guards (crisp predicates over recorded facts); a branch
whose guard is provably false is closed (`abandoned`); an abandoned dep is neutral for a join.
Loops are re-arm (a node whose done-condition isn't met stays claimable and runs again) — not yet
exercised here (Phase 4).
"""
from __future__ import annotations

import concurrent.futures

from work_objects.model import utcnow
from app.assistant.utils.logging_config import get_logger
from app.assistant.task_runtime.guard_eval import UnsupportedGuard, facts_context, guard_value
from app.assistant.task_runtime.tool_executor import execute_claimed_tool_node

logger = get_logger(__name__)

_WORK_TYPES = {"tool", "action"}
_CLAIMABLE = {"proposed", "waiting", "actionable"}
# A dep counts as resolved (downstream may proceed) at any of these — `abandoned`/`skipped` included:
# a not-taken branch is neutral for a join (§3), decided here at the runner level so the store's
# is_ready stays untouched (dayflow keeps its own semantics).
_RESOLVED = {"closed", "done", "verified", "passed", "answered", "abandoned", "skipped", "superseded"}
_MAX_PARALLEL = 8
_MAX_WAVES = 500   # backstop against a malformed graph looping forever


def _deps_resolved(wo, node) -> bool:
    return all(wo.nodes.get(d) is not None and wo.nodes[d].status in _RESOLVED
               for d in wo.deps_of(node.id))


def _is_wait_like(n) -> bool:
    """A wait or loop node's condition is a RELEASE condition ("not yet"), never a branch
    guard ("not taken") — it must not be branch-closed when it evaluates False, and a False
    release simply keeps the node waiting for its next promotion."""
    return bool(n.payload.get("is_wait") or n.payload.get("is_loop"))


def _guard_or_fail(store, wo, n, facts):
    """Evaluate a node's guard; a malformed/unsupported expression fails the NODE loudly
    (work_repair territory) instead of crashing the whole drive on every attempt."""
    try:
        return guard_value(n, facts)
    except UnsupportedGuard as e:
        logger.error("[task_runner] %s::%s has an unrunnable guard — failing the node: %s",
                     wo.id, n.id, e)
        store.apply("set_status", {"work_id": wo.id, "node_id": n.id, "status": "failed",
                                   "content": f"unrunnable guard: {e}"[:500]}, actor="task_runner")
        return "failed"


def _ready_frontier(store, wo, facts, now) -> list:
    out = []
    for n in wo.nodes.values():
        if n.type not in _WORK_TYPES or n.status not in _CLAIMABLE:
            continue
        # an event-parked node (status `waiting` on an event/signal/user_reply wake) runs only when a
        # delivered wake promotes it to `actionable` (wake-promotion) — never via a static guard over
        # ACCUMULATED events (else a re-arming loop would re-fire on the same event forever).
        if n.status == "waiting" and n.wake_kind in ("event", "signal", "user_reply"):
            continue
        if n.wake_at is not None and n.wake_at > now:
            continue
        if not _deps_resolved(wo, n):
            continue
        if _guard_or_fail(store, wo, n, facts) in (None, True):
            out.append(n)
    return out


def _close_dead_branches(store, wo, facts) -> None:
    """A claimable node whose deps are resolved and whose guard is provably FALSE can never fire →
    abandon it (branch close). Its `abandoned` status is neutral for any downstream join.

    EXEMPT: wait/loop nodes (`is_wait`/`is_loop`). Their condition is a RELEASE condition —
    False means "not released yet", not "branch not taken". Without the exemption, the first
    delivered event made every OTHER pending wait's `"<ev>" in events_observed` evaluate
    provably False → abandoned → downstream joins unblocked → the task completed without doing
    the skipped waits (verification finding, probe-confirmed 2026-07-11).

    Phase 1 closes only the guarded node itself; a MULTI-node not-taken branch (a guardless node
    that depends only on an abandoned one) is not yet cascaded — Phase 4 adds branch-subtree
    abandonment + AND/OR-join semantics when it lands real multi-node branching."""
    for n in list(wo.nodes.values()):
        if n.type in _WORK_TYPES and n.status in _CLAIMABLE and _deps_resolved(wo, n):
            if _is_wait_like(n):
                continue
            if _guard_or_fail(store, wo, n, facts) is False:
                store.apply("set_status", {"work_id": wo.id, "node_id": n.id, "status": "abandoned",
                                       "verdict": "branch_closed",
                                       "reason": "task runner: guard provably false — branch not taken"},
                            actor="task_runner")


def _run_action_node(store, work_id: str, node_id: str, scope=None) -> None:
    """Run an already-claimed action node via the work-object SUBSTRATE — discharge_node drives
    the node's manager (its `payload.executor`, default work_emi_team_manager) and records the result;
    we then close done -> closed (a task has no separate finalizer). NOTHING from dayflow. The RUN
    scope is threaded through so the action's manager executes at the run's authority (98), not the
    substrate's orchestrator scope (99) — a tool floored above the run scope was denied on a tool
    node but reachable inside an action node's manager (verification finding R8)."""
    from work_objects.discharge import discharge_node
    node = store.load(work_id).nodes[node_id]
    executor = str(node.payload.get("executor") or "").strip() or "work_emi_team_manager"
    try:
        discharge_node(store, work_id, node_id, manager_name=executor, scope_context=scope)
    except Exception as e:
        logger.error("[task_runner] action node %s::%s failed: %s", work_id, node_id, e)
        cur = store.load(work_id).nodes.get(node_id)
        if cur is not None and cur.status not in ("done", "failed", "closed", "abandoned"):
            store.apply("set_status", {"work_id": work_id, "node_id": node_id, "status": "failed",
                                       "content": str(e)[:500]}, actor="task_runner")
        return
    cur = store.load(work_id).nodes.get(node_id)
    if cur is not None and cur.status == "done":
        store.apply("set_status", {"work_id": work_id, "node_id": node_id, "status": "closed",
                                   "reason": "task runner: action node done (a task has no separate finalizer)"}, actor="task_runner")


def _has_future_wake(wo, now) -> bool:
    return any(n.status in _CLAIMABLE and n.wake_at is not None and n.wake_at > now
               for n in wo.nodes.values())


def _has_event_wait(wo) -> bool:
    """A claimable node waiting on an event/signal/user_reply (no time wake) — the run is parked on an
    external event (resumed when it fires + is recorded), not out of work."""
    return any(n.status in _CLAIMABLE and n.wake_kind in ("event", "signal", "user_reply")
               for n in wo.nodes.values())


def _nonterminal_work_remains(wo) -> list:
    from work_objects.model import _TERMINAL_STATUSES
    return [n.id for n in wo.nodes.values() if n.type in _WORK_TYPES and n.status not in _TERMINAL_STATUSES]


def failure_reason(store, work_id: str) -> str:
    """A short human-readable reason for a failed/stalled run — the failed node(s) and their
    recorded errors. This is what reaches the routine's last_error and the auto-disable ticket;
    without it a failed briefing showed status=error with no WHY (2026-07-12 incident)."""
    try:
        wo = store.load(work_id)
    except Exception as e:
        return f"(could not load {work_id}: {e})"
    parts = []
    for n in wo.nodes.values():
        if n.type in _WORK_TYPES and n.status == "failed":
            detail = str(n.content or "").strip().splitlines()[0][:160] if n.content else ""
            parts.append(f"{n.id}: {detail}" if detail else n.id)
    return "; ".join(parts[:3]) or "no failed node recorded (stalled graph)"


def drive(store, work_id: str, *, scope, scope_contract_enforced: bool = True) -> str:
    """Run the active phase. Returns 'done' | 'parked' | 'failed' | 'stalled' | 'abandoned'."""
    for _ in range(_MAX_WAVES):
        now = utcnow()
        wo = store.load(work_id)
        if wo.status in ("done", "abandoned"):
            return wo.status

        facts = facts_context(wo)
        _close_dead_branches(store, wo, facts)
        wo = store.load(work_id)
        facts = facts_context(wo)
        ready = _ready_frontier(store, wo, facts, now)

        if not ready:
            if _has_future_wake(wo, now) or _has_event_wait(wo):
                return "parked"         # durable wait on the node — resumed when the time/event fires
            failed = [n.id for n in wo.nodes.values() if n.type in _WORK_TYPES and n.status == "failed"]
            if failed:                  # a node failed and nothing can advance — work_repair territory (Phase 4)
                logger.error("[task_runner] %s cannot complete — failed node(s): %s", work_id, failed)
                return "failed"
            remaining = _nonterminal_work_remains(wo)
            if remaining:
                logger.error("[task_runner] %s STALLED — non-terminal work with nothing runnable: %s",
                             work_id, remaining)
                return "stalled"
            store.apply("set_work_status", {"work_id": work_id, "status": "done",
                                        "reason": "task runner: reach-end — all work resolved"}, actor="task_runner")
            return "done"

        # Claim the whole ready frontier synchronously (no double-dispatch), then run it in ONE
        # parallel wave — both kinds on the work-object SUBSTRATE, nothing from dayflow: `tool` nodes
        # via the deterministic tool executor, `action` nodes via discharge.discharge_on (the node's
        # manager). The wave joins before re-evaluating.
        for n in ready:
            store.apply("set_status", {"work_id": work_id, "node_id": n.id, "status": "dispatched"},
                        actor="task_runner")
        workers = min(_MAX_PARALLEL, len(ready))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futures = []
            for n in ready:
                if n.type == "tool":
                    futures.append(ex.submit(execute_claimed_tool_node, store, work_id, n.id, scope,
                                             scope_contract_enforced))
                else:  # action -> the substrate manager driver, under the RUN scope
                    futures.append(ex.submit(_run_action_node, store, work_id, n.id, scope))
            for f in futures:
                f.result()   # join; the executors fail their own node on error

    logger.error("[task_runner] %s exceeded %d waves — aborting drive", work_id, _MAX_WAVES)
    return "stalled"
