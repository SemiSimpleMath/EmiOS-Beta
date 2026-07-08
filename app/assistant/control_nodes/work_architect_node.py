"""Decompose freshly-created goals into their DAG, and re-plan flagged ones (Part 2 of the split planner).

Runs right after strategic_planner_wo_persist_node. Per tick, for ONE work object at a time:
- CREATE: for each work object the evaluator created this tick, invoke dayflow_orchestrator::work_architect
  on its objective and lay the resulting DAG (subtask nodes, depends_on edges, wait-gates) into the graph
  via apply_architect_dag.
- RE-PLAN: for each work object flagged in `replan_work_ids` (by the evaluator from intake, OR by the
  work_finalizer's AMEND verdict — its revised intent rides in `finalizer_amend_intents`), re-invoke the
  architect with the goal + the existing graph and apply the DELTA: ADD the missing steps and ABANDON the
  nodes the situation made moot.

The materializer → action_selector → switchboard → dispatch loop then runs the ready nodes. Never raises — a per-object failure leaves that object as-is
and the pipeline continues.

Inert until the dayflow manager's state_map routes to it.
"""
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)

_MAX_REPLANS_PER_TICK = 3
_TERMINAL_WO_STATES = {"done", "abandoned"}


def _render_existing_graph(wo) -> str:
    """Compact listing of a work object's current subtask nodes, for the re-plan prompt."""
    lines = []
    for n in wo.nodes.values():
        if n.id == wo.goal_node_id:
            continue
        wake = f" wake={n.wake_kind}" if getattr(n, "wake_kind", None) else ""
        lines.append(f"- {n.id} | {n.title} | status={n.status}{wake}")
    return "\n".join(lines) or "(no subtask nodes yet)"


def _situational_context(bb) -> str:
    """The same situational picture the steward sees — user ticket REPLIES (directives), active
    tickets, the portfolio, and recently-completed work — so the architect re-plans WITH the user's
    intent in view instead of blind. The steward's prep node loaded these onto the shared (manager)
    blackboard earlier this tick. Passed as the architect Message's `information` (rendered as CONTEXT)."""
    parts = []
    resp = bb.get_state_value("recent_responded_tickets", {}) or {}
    resp_lines = []
    for cat, label in (("accepted", "ACCEPTED"), ("declined", "DECLINED"), ("snoozed", "SNOOZED")):
        for t in (resp.get(cat) or []):
            c = str(t.get("user_comment") or "").strip()
            resp_lines.append(f"- {label}: {t.get('title', '')}" + (f' — user: "{c}"' if c else ""))
    if resp_lines:
        parts.append("## RECENT TICKET RESPONSES (user directives — incorporate these into the graph)\n"
                     + "\n".join(resp_lines))
    active = bb.get_state_value("active_tickets", []) or []
    if active:
        parts.append("## ACTIVE TICKETS (awaiting the user)\n"
                     + "\n".join(f"- [{t.get('suggestion_type', '')}] {t.get('title', '')}" for t in active))
    portfolio = str(bb.get_state_value("work_portfolio", "") or "").strip()
    if portfolio:
        parts.append("## WORK PORTFOLIO\n" + portfolio)
    completed = str(bb.get_state_value("recent_completed_work", "") or "").strip()
    if completed:
        parts.append("## RECENTLY COMPLETED\n" + completed)
    return "\n\n".join(parts)


class WorkArchitectNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        decomposed = []
        replanned = []
        try:
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            from app.assistant.dayflow_orchestrator.work_architect_apply import apply_architect_dag
            store = get_dayflow_work_store()

            persist = self.blackboard.get_state_value("steward_persist_result", {}) or {}
            created = [c for c in (persist.get("created") or [])
                       if isinstance(c, dict) and c.get("work_id") and c.get("objective")]
            replan_ids = [str(w).strip() for w in (self.blackboard.get_state_value("replan_work_ids", []) or [])
                          if str(w or "").strip()]
            # the finalizer's revised intent per WO (AMEND verdicts) — the authoritative "why re-plan".
            amend_intents = self.blackboard.get_state_value("finalizer_amend_intents", {}) or {}

            if created or replan_ids:
                scope = self._scope(message)
                info = _situational_context(self.blackboard)
                agent = DI.agent_factory.create_agent("dayflow_orchestrator::work_architect")

                # CREATE — decompose each freshly-minted goal.
                for c in created:
                    work_id = c["work_id"]
                    try:
                        rationale = str(c.get("rationale") or "").strip()
                        why = (f"## WHY THIS IS A WORK OBJECT — the steward's brief (its NATURE + intent; "
                               f"honor it: a reminder about the user's own activity is a single node whose "
                               f"goal is to tell them at the right time)\n{rationale}\n\n" if rationale else "")
                        result = agent.action_handler(Message(task=c["objective"], information=why + info, scope_context=scope))
                        nodes = (getattr(result, "data", {}) or {}).get("nodes", []) or []
                        res = apply_architect_dag(store, work_id, nodes)
                        decomposed.append({"work_id": work_id, "nodes": len(res.get("added", []))})
                        logger.info("[%s] decomposed %s into %d node(s)",
                                    self.name, work_id, len(res.get("added", [])))
                    except Exception as e:
                        logger.error("[%s] decompose failed for %s: %s", self.name, work_id, e)
                        logger.debug("[%s] decompose exception", self.name, exc_info=True)

                # RE-PLAN — extend the graph of each flagged work object (skip ones also created this tick).
                created_ids = {c["work_id"] for c in created}
                for work_id in [w for w in replan_ids if w not in created_ids][:_MAX_REPLANS_PER_TICK]:
                    try:
                        wo = store.load(work_id)
                    except Exception as e:
                        logger.warning("[%s] re-plan target %s not loadable: %s", self.name, work_id, e)
                        continue
                    if str(wo.status or "").lower() in _TERMINAL_WO_STATES:
                        continue
                    try:
                        goal = wo.nodes.get(wo.goal_node_id)
                        objective = (getattr(goal, "content", "") or getattr(goal, "title", "")) if goal else ""
                        amend = str(amend_intents.get(work_id) or "").strip()
                        amend_block = (f"A just-completed step changed the plan. Revised intent: {amend}\n\n"
                                       if amend else "")
                        task = (
                            f"{objective}\n\n{amend_block}This goal ALREADY has a partial work graph (below) that no "
                            f"longer fits. RE-PLAN it as a DELTA: ADD the steps still missing, and ABANDON "
                            f"(list their node_ids in abandon_node_ids) any existing node the situation now "
                            f"makes moot/wrong — its un-finished sub-steps go with it. Do NOT recreate "
                            f"existing nodes (reference their node_ids in depends_on). Output the delta only. "
                            f"Existing nodes:\n{_render_existing_graph(wo)}"
                        )
                        result = agent.action_handler(Message(task=task, information=info, scope_context=scope))
                        data = getattr(result, "data", {}) or {}
                        res = apply_architect_dag(store, work_id, data.get("nodes", []) or [],
                                                  abandon_node_ids=data.get("abandon_node_ids", []) or [])
                        replanned.append({"work_id": work_id, "added": len(res.get("added", [])),
                                          "abandoned": len(res.get("abandoned", []))})
                        logger.info("[%s] re-planned %s: +%d node(s), -%d abandoned",
                                    self.name, work_id, len(res.get("added", [])), len(res.get("abandoned", [])))
                    except Exception as e:
                        logger.error("[%s] re-plan failed for %s: %s", self.name, work_id, e)
                        logger.debug("[%s] re-plan exception", self.name, exc_info=True)
        except Exception as e:
            logger.error("[%s] architect node failed: %s", self.name, e)
            logger.debug("[%s] architect node exception", self.name, exc_info=True)

        self.blackboard.update_state_value("work_decompose_result", decomposed)
        self.blackboard.update_state_value("work_replan_result", replanned)
        self.blackboard.update_state_value("last_agent", self.name)

    def _scope(self, message):
        scope = getattr(message, "scope_context", None)
        if scope is not None:
            return scope
        from app.assistant.scope.loader import load_scope_for_source
        return load_scope_for_source(kind="pipeline", source_id="dayflow", actor_id=self.name)
