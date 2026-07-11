"""The task pipeline runner — the active drive loop.

Evaluates the work object's ready-set and drives it wave-by-wave with NO scheduler round-trip
between nodes (that round-trip is dayflow's latency): each pass fans the whole ready frontier out —
deterministic `tool` nodes in parallel (a thread pool), `action` nodes async to managers via
`node_dispatch._do_work` (resume-on-completion) — then re-evaluates. It yields when only in-flight
async work remains ("running", resumed by the node's completion signal) or only future-wake nodes
remain ("parked", resumed by the scheduler's precise wake), and completes on reach-end or when it
runs out of runnable work.

Conditionals are in the graph as per-node guards (crisp predicates over recorded facts); a branch
whose guard is provably false is closed (`abandoned`); an abandoned dep is neutral for a join.
Loops are re-arm (a node whose done-condition isn't met stays claimable and runs again) — not yet
exercised here (Phase 4).
"""
from __future__ import annotations

import concurrent.futures

from work_objects.model import utcnow
from app.assistant.utils.logging_config import get_logger
from app.assistant.task_runtime.guard_eval import facts_context, guard_value
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


def _ready_frontier(wo, facts, now) -> list:
    out = []
    for n in wo.nodes.values():
        if n.type not in _WORK_TYPES or n.status not in _CLAIMABLE:
            continue
        if n.wake_at is not None and n.wake_at > now:
            continue
        if not _deps_resolved(wo, n):
            continue
        if guard_value(n, facts) in (None, True):
            out.append(n)
    return out


def _close_dead_branches(store, wo, facts) -> None:
    """A claimable node whose deps are resolved and whose guard is provably FALSE can never fire →
    abandon it (branch close). Its `abandoned` status is neutral for any downstream join.

    Phase 1 closes only the guarded node itself; a MULTI-node not-taken branch (a guardless node
    that depends only on an abandoned one) is not yet cascaded — Phase 4 adds branch-subtree
    abandonment + AND/OR-join semantics when it lands real branching (auto_reply_katy)."""
    for n in list(wo.nodes.values()):
        if n.type in _WORK_TYPES and n.status in _CLAIMABLE and _deps_resolved(wo, n):
            if guard_value(n, facts) is False:
                store.apply("set_status", {"work_id": wo.id, "node_id": n.id, "status": "abandoned"},
                            actor="task_runner")


def _has_inflight(wo) -> bool:
    return any(n.status == "dispatched" for n in wo.nodes.values())


def _has_future_wake(wo, now) -> bool:
    return any(n.status in _CLAIMABLE and n.wake_at is not None and n.wake_at > now
               for n in wo.nodes.values())


def _nonterminal_work_remains(wo) -> list:
    from work_objects.model import _TERMINAL_STATUSES
    return [n.id for n in wo.nodes.values() if n.type in _WORK_TYPES and n.status not in _TERMINAL_STATUSES]


def drive(store, work_id: str, *, scope, scope_contract_enforced: bool = True) -> str:
    """Run the active phase. Returns 'done' | 'parked' | 'running' | 'stalled'."""
    for _ in range(_MAX_WAVES):
        now = utcnow()
        wo = store.load(work_id)
        if wo.status in ("done", "abandoned"):
            return wo.status

        facts = facts_context(wo)
        _close_dead_branches(store, wo, facts)
        wo = store.load(work_id)
        facts = facts_context(wo)
        ready = _ready_frontier(wo, facts, now)

        if not ready:
            if _has_inflight(wo):
                return "running"        # async action node(s) in flight — resumed on their completion
            if _has_future_wake(wo, now):
                return "parked"         # durable wake on the node — scheduler re-invokes resume
            failed = [n.id for n in wo.nodes.values() if n.type in _WORK_TYPES and n.status == "failed"]
            if failed:                  # a node failed and nothing can advance — work_repair territory (Phase 4)
                logger.error("[task_runner] %s cannot complete — failed node(s): %s", work_id, failed)
                return "failed"
            remaining = _nonterminal_work_remains(wo)
            if remaining:
                logger.error("[task_runner] %s STALLED — non-terminal work with nothing runnable: %s",
                             work_id, remaining)
                return "stalled"
            store.apply("set_work_status", {"work_id": work_id, "status": "done"}, actor="task_runner")
            return "done"

        action_nodes = [n for n in ready if n.type == "action"]
        tool_nodes = [n for n in ready if n.type == "tool"]

        # action nodes: node_dispatch claims + runs them on their own job threads (resume on completion)
        if action_nodes:
            from app.assistant.dayflow_orchestrator import node_dispatch
            for n in action_nodes:
                node_dispatch._do_work(store, work_id, n.id)

        # tool nodes: claim synchronously (no double-dispatch), then execute the wave in parallel
        for n in tool_nodes:
            store.apply("set_status", {"work_id": work_id, "node_id": n.id, "status": "dispatched"},
                        actor="task_runner")
        if tool_nodes:
            workers = min(_MAX_PARALLEL, len(tool_nodes))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(execute_claimed_tool_node, store, work_id, n.id, scope,
                                     scope_contract_enforced) for n in tool_nodes]
                for f in futures:
                    f.result()   # join; execute_claimed_tool_node already fails the node on error

    logger.error("[task_runner] %s exceeded %d waves — aborting drive", work_id, _MAX_WAVES)
    return "stalled"
