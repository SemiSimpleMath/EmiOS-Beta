"""Control node: the WORK FINALIZER — judge the result of the node this pass just ran, and write
that judgment to the graph. AUTHORITATIVE (step 3): it is the only thing that produces the
`closed` terminal.

Part of the TOOL RETURN PATH, and fixed to ONE node: the tool caller has just recorded a result
on the node this pass dispatched, and this judges that result. It is not a sweep. Nothing else is
looked at, because nothing else can have changed — a run makes one call, and the graph moves only
when a call returns.

The node judged is always a TOP-LEVEL one (a direct child of the goal — the architect's units;
the worker's nested checklist is its own business and never counts toward the goal). BOTH
outcomes are judged: `done` (the call returned) and `failed` (it did not). Judging the failure
is the point — the result text is the only account of the failure MODE, and the mode is what
decides whether trying again could possibly go differently. The steward and the architect deal
in top-level nodes; this is the one thing that reads what actually happened inside one.

It invokes the work_finalizer adjudicator on {the WO projection + that node's FULL, un-truncated
result} and applies the verdict it returns:

  - PROCEED  -> close the node (`done` -> `closed`). It now counts toward the goal (is_satisfied keys on
               `closed`), so its dependents unblock and, when all the goal's children are closed, the rollup
               completes the WorkObject.
  - AMEND    -> the same close, PLUS the revised intent recorded on the node; the architect
               revises the graph from it on a later tick.
  - REPLAN   -> re-open the failed node (`failed` -> `proposed`, the architect's inbox) with an
               instruction naming what must be DIFFERENT. Rare: an unchanged retry reproduces the
               error, which is how one bad argument became 22 identical attempts in two hours.
  - BLOCKED  -> the failed node stays failed, with the reason recorded. Nothing can change; the
               steward sees a goal it must decide about.

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

# What each verdict writes. `blocked` maps to None on purpose: the node is already `failed` and
# that is the truthful status — only the finalizer's account needs recording, which is why the
# write still happens (with the status unchanged) rather than being skipped.
_STATUS_FOR = {"proceed": "closed", "amend": "closed", "replan": "proposed", "blocked": None}
_APPLICABLE = set(_STATUS_FOR)


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
        if node is None or node.status not in _AWAITING_JUDGMENT:
            return None
        if node.id == wo.goal_node_id or node.parent_id != wo.goal_node_id:
            # Only the architect's own units are judged. A worker's nested step is internal
            # to the call that grew it and never counts toward the goal.
            logger.info("[%s] %s is not a top-level node — nothing to judge.", self.name, ref)
            return None
        return wo, node

    def _judge(self, wo, node, scope) -> dict:
        from app.assistant.dayflow_orchestrator.work_portfolio import (
            node_result, render_work_portfolio, STATUS_LEGEND,
        )
        projection = STATUS_LEGEND + "\n\n" + render_work_portfolio(wo)
        # The node's RESULT is the evidence it produced — `content` is its (immutable) directive.
        result_text = node_result(wo, node) or "(no result recorded)"
        # Which verdicts are even available follows from the node's status, and the store enforces
        # it anyway (`done` may only become `closed`; `failed` may only re-open or stay). Saying so
        # here keeps the agent from returning one that cannot be written.
        if node.status == "failed":
            outcome = ("THE CALL FAILED — the tool reported it could not run. Your verdict is "
                       "'replan' (only if you can name something that will genuinely be different) "
                       "or 'blocked'.")
            # Repetition is the thing the finalizer cannot see from one result: each failure looks
            # like a first failure. The count is kept on the node, so say it plainly.
            failures = int((node.payload or {}).get("failure_count") or 0)
            if failures >= _REPEAT_FAILURE_LIMIT:
                outcome += (
                    f"\n\nTHIS STEP HAS NOW FAILED {failures} TIMES. It keeps failing at the same "
                    f"thing, so trying it again is very unlikely to work. Unless the result below "
                    f"names something concrete that has CHANGED since the last attempt, do not "
                    f"replan it: either say 'blocked' and let the steward decide what becomes of "
                    f"the goal, or — if a person could unblock it by answering something (a "
                    f"credential, an account, a missing detail, a decision) — say 'blocked' and "
                    f"name that question in your reasoning so the user can be asked.")
        else:
            outcome = ("THE CALL RETURNED. Your verdict is 'proceed' (the plan still fits) or "
                       "'amend' (this result changes the plan).")
        info = (f"JUST-COMPLETED NODE — id: {node.id} | title: {node.title}\n"
                f"{outcome}\n"
                f"ITS FULL RESULT:\n{result_text}")
        agent = DI.agent_factory.create_agent("dayflow_orchestrator::work_finalizer")
        res = agent.action_handler(Message(task=projection, information=info, scope_context=scope))
        return getattr(res, "data", {}) or {}

    def _apply(self, wo, node, data: dict) -> list:
        """Turn the agent's schema into graph state. The only thing that writes a finalizer verdict.

        PROCEED -> `done` -> `closed`. `closed` is the satisfied terminal `is_satisfied` keys on, so
                   the node now counts toward its goal, its dependents unblock, and when every child
                   of the goal is closed the store's rollup completes the WorkObject.
        AMEND   -> the same close, PLUS the revised intent on the node for the architect.
        REPLAN  -> `failed` -> `proposed`: back to the architect's inbox carrying the instruction
                   naming what must be DIFFERENT. The node is NOT simply re-run — re-running a step
                   whose circumstances have not changed reproduces its error.
        BLOCKED -> the status is untouched (it already says `failed`, which is the truth) and only
                   the reason is recorded, where the steward and the architect read it.
        """
        verdict = str(data.get("verdict") or "").strip().lower()
        if verdict not in _APPLICABLE:
            logger.error("[%s] unusable verdict %r for %s::%s — not applied",
                         self.name, verdict, wo.id, node.id)
            return []

        reason = str(data.get("reasoning") or "").strip()
        # The architect replans FROM the instruction and gets the REASONING with it: the instruction
        # says what to do, the reasoning says why, and the architect is deciding whether to keep this
        # step, change it, or drop it and do something else. A recommendation without its grounds is
        # one the architect can only obey or ignore.
        instruction = str((data.get("amend_intent") if verdict == "amend"
                           else data.get("replan_instruction")) or "")
        target = _STATUS_FOR.get(verdict) or node.status

        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        try:
            get_dayflow_work_store().apply("set_status", {
                "work_id": wo.id, "node_id": node.id, "status": target,
                "verdict": verdict,
                "reason": reason or "finalizer accepted the result",
                "finalizer": {"verdict": verdict, "instruction": instruction, "reasoning": reason},
            }, actor="finalizer")
        except Exception as e:
            logger.error("[%s] could not set %s::%s to %r: %s",
                         self.name, wo.id, node.id, target, e)
            return []

        logger.info("[%s] %s::%s -> %s (%s)", self.name, wo.id, node.id,
                    target if _STATUS_FOR.get(verdict) else "failed (unchanged)", verdict)
        return [{"work_id": wo.id, "node_id": node.id, "verdict": verdict}]

    def _scope(self, message):
        scope = getattr(message, "scope_context", None)
        if scope is not None:
            return scope
        from app.assistant.scope.loader import load_scope_for_source
        return load_scope_for_source(kind="pipeline", source_id="dayflow", actor_id=self.name)
