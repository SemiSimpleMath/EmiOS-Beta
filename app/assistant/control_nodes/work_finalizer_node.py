"""Control node: the WORK FINALIZER — judge the result of the node this pass just ran, and write
that judgment to the graph. AUTHORITATIVE (step 3): it is the only thing that produces the
`closed` terminal.

Part of the TOOL RETURN PATH, and fixed to ONE node: the tool caller has just recorded a result
on the node this pass dispatched, and this judges that result. It is not a sweep. Nothing else is
adjudicated. Its owned provenance is read to understand prior attempts and results.
Other dispatch workers and planning passes can change the graph concurrently (see WO1).

The node judged is always a TOP-LEVEL one (a direct child of the goal — the architect's units;
the worker's nested checklist is its own business and never counts toward the goal). BOTH
outcomes are judged: `done` (the call returned) and `failed` (it did not). Judging the failure
is the point — the returned result and owned provenance explain the failure MODE, which is what
decides whether trying again could possibly go differently. The steward and the architect deal
in top-level nodes; this is the one thing that reads what actually happened inside one.

It invokes the work_finalizer adjudicator on {the WO projection + that node's FULL, un-truncated
result + full directive + owned provenance} and applies the verdict it returns. The verdict answers ONE question — was the node's goal
achieved — and the tool's own status (returned / reported failure) is input to that judgment, never a
constraint on it:

  - ACHIEVED              -> `closed`. It now counts toward the goal (is_satisfied keys on `closed`),
                             its dependents unblock, and when all the goal's children are closed the
                             rollup completes the WorkObject.
  - ACHIEVED_PLAN_CHANGES -> the same close, PLUS a recommendation on the node; the architect revises
                             the graph from it next tick (the rollup holds the goal open meanwhile).
  - RETRY                 -> `failed` (counted as an attempt that did not land) then `proposed` — back
                             in the architect's inbox with a recommendation naming what will differ.
  - UNRECOVERABLE         -> `failed`, with `next_step` typed: stop / new_approach / ask_user. The
                             architect reads it next tick and prunes, re-plans, or plans the ask.

Every verdict carries `outcome` — prose written for the planner — and it travels with the node.

REPEATED FAILURE is the runtime's, not the finalizer's: once a goal has accumulated
_REPEAT_FAILURE_LIMIT attempts that did not achieve it, a not-achieved verdict is escalated here to
`ask_user` regardless of what the finalizer recommended, so the user is asked instead of the same
thing being tried again under a new node id.

The verdict is written to the NODE (nodes.payload.finalizer in emi.db), never handed on in
memory: the architect that acts on an amend/replan runs on a LATER tick, and every tick builds a
fresh manager with a fresh Blackboard, so an in-memory handoff is discarded before its reader
exists. See work_architect_node._pending_finalizer_instructions for the read side.

(Until 2026-09-16 this was two control nodes — one judged onto the blackboard, one read the
blackboard and wrote the graph. The agent call happens HERE, so the schema is already in hand;
passing it through a blackboard to the next state_map step bought nothing. work_architect_node
has always called its agent and written the graph in one node; this now matches it.)

Its reach is THE NODE IT JUDGED. Whether the GOAL is achieved or moot belongs to the steward,
which sees every work object each run — a goal should not end on one node's result. (Until
2026-09-16 a third verdict, RESOLVE, let this set the whole WorkObject done or abandoned here.)

A worker-`done` node is NOT satisfied on its own anymore (is_satisfied -> `closed`), so nothing closes a goal
until the finalizer has judged its results — this is what removes the blind auto-completion. Bounded per
tick; never raises.

Inert until the dayflow manager's state_map routes to it.
"""
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)

_TERMINAL_WO = {"done", "abandoned"}
# A node whose call has come back, either way, and awaits judgment. `done` is a result the tool
# returned; `failed` is the tool reporting it could not run. Both carry their account in the result
# TEXT — including "notify expired, user not reached" and "could not access that account" — and the
# account is what the verdict is made from. (`incomplete` used to be listed here too: declared in the
# transition table from the start, written by nothing, taught to no agent.)
_AWAITING_JUDGMENT = {"done", "failed"}

# Failing twice at the same step means the step is what is wrong, not the luck. At this many
# failures the finalizer is told so outright — an unchanged retry reproduces its error, which is
# how one broken argument became 22 identical attempts in two hours, and how a single delivery
# node wrote the same tool error onto its graph seventeen times with nothing counting.
_REPEAT_FAILURE_LIMIT = 2

# What each verdict writes, as the SEQUENCE of statuses the node passes through. A not-achieved
# verdict is counted once by the store judgment transaction, independently of tool status,
# even when the call itself returned cleanly. `retry` then re-opens the node to the architect's
# inbox; the architect may keep it, change it, or replace it, reading the recommendation.
_STATUS_FOR = {
    "achieved": ("closed",),
    "achieved_plan_changes": ("closed",),
    "retry": ("failed", "proposed"),
    "unrecoverable": ("failed",),
}
_APPLICABLE = set(_STATUS_FOR)
# The routing word the architect reads. For two verdicts it IS the verdict; for `unrecoverable`
# it is the finalizer's `next_step`.
_ROUTE_FOR = {"achieved_plan_changes": "plan_changes", "retry": "retry"}


class WorkFinalizerNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        applied = []
        try:
            found = self._the_node_that_just_ran()
            if found is not None:
                wo, node = found
                data = self._judge(wo, node, self._scope(message))
                applied = self._apply(wo, node, data)
        except Exception as e:
            logger.error("[%s] work_finalizer node failed: %s", self.name, e)
            logger.debug("[%s] work_finalizer node exception", self.name, exc_info=True)

        self.blackboard.update_state_value("work_finalizer_result", applied)
        self.blackboard.update_state_value("last_agent", self.name)

    def _the_node_that_just_ran(self):
        """The one node this pass dispatched, if its call produced a result to judge.

        Returns ``(work_object, node)`` or None — the ordinary case for a pass that made no call.
        """
        ref = str(self.blackboard.get_state_value("work_node_ref", "") or "").strip()
        if "::" not in ref:
            return None
        work_id, node_id = ref.split("::", 1)

        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        wo = get_dayflow_work_store().load(work_id)
        if str(getattr(wo, "status", "") or "").lower() in _TERMINAL_WO:
            return None
        node = wo.nodes.get(node_id)
        if node is None or not wo.needs_finalization(node):
            return None
        expected = self.blackboard.get_state_value("dispatch_epoch", None)
        epoch = int(node.payload.get("dispatch_epoch") or 0)
        if (expected is not None and int(expected) != epoch) or node.payload.get("finalized_epoch") == epoch:
            return None
        if not wo.is_work_unit(node):
            # Only the architect's own units are judged. A worker's nested step is internal
            # to the call that grew it and never counts toward the goal.
            logger.info("[%s] %s is not a top-level node — nothing to judge.", self.name, ref)
            return None
        return wo, node

    def _judge(self, wo, node, scope) -> dict:
        from app.assistant.dayflow_orchestrator.work_portfolio import node_result, render_portfolio
        from app.assistant.dayflow_orchestrator.work_context import render_view, worker_data
        projection = render_portfolio([wo])
        info = render_view("finalizer_input", view=worker_data(wo, node.id),
                           result_text=node_result(wo, node), repeat_failure_limit=_REPEAT_FAILURE_LIMIT)
        agent = DI.agent_factory.create_agent("dayflow_orchestrator::work_finalizer")
        res = agent.action_handler(Message(task=projection, information=info, scope_context=scope))
        return getattr(res, "data", {}) or {}

    def _apply(self, wo, node, data: dict) -> list:
        """Turn the agent's schema into graph state. The only thing that writes a finalizer verdict.

        The verdict decides the status sequence (see _STATUS_FOR); the `outcome` prose and the
        recommendation ride on the node as `payload.finalizer` for the planner that acts next tick.
        The recommendation says what to do, the outcome says why — a recommendation without its
        grounds can only be obeyed or ignored, never judged.

        REPEATED FAILURE is escalated HERE, deterministically. When this verdict would be the
        goal's _REPEAT_FAILURE_LIMIT-th attempt that did not achieve it, a `retry` or any other
        not-achieved route becomes `ask_user`: the node stays `failed` rather than re-opening, and
        the architect is told to plan the question. The finalizer judged one result; the runtime is
        the thing that can see the pattern, so the runtime makes the call.
        """
        verdict = str(data.get("verdict") or "").strip().lower()
        if verdict not in _APPLICABLE:
            logger.error("[%s] unusable verdict %r for %s::%s — not applied",
                         self.name, verdict, wo.id, node.id)
            return []

        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        from work_objects.store import StaleResult
        payload = {key: data.get(key, "") for key in ("outcome", "recommendation", "question_for_user")}
        payload.update(verdict=verdict, next_step=_ROUTE_FOR.get(verdict) or data.get("next_step", ""))
        try:
            updated = get_dayflow_work_store().apply("finalize_task", {
                "work_id": wo.id, "node_id": node.id,
                "expected_dispatch_epoch": int(node.payload.get("dispatch_epoch") or 0),
                "finalizer": payload, "repeat_failure_limit": _REPEAT_FAILURE_LIMIT,
            }, actor="finalizer")
        except StaleResult:
            return []
        fin = updated.nodes[node.id].payload["finalizer"]
        return [{"work_id": wo.id, "node_id": node.id, "verdict": verdict, "next_step": fin["next_step"]}]

    def _scope(self, message):
        scope = getattr(message, "scope_context", None)
        if scope is not None:
            return scope
        from app.assistant.scope.loader import load_scope_for_source
        return load_scope_for_source(kind="pipeline", source_id="dayflow", actor_id=self.name)
