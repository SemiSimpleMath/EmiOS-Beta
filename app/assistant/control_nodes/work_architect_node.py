"""Decompose freshly-created goals into their DAG, and re-plan flagged ones (Part 2 of the split planner).

Runs right after strategic_planner_wo_persist_node. Per tick, for ONE work object at a time:
- CREATE: for each work object the evaluator created this tick, invoke dayflow_orchestrator::work_architect
  on its objective and lay the resulting DAG (subtask nodes, depends_on edges, wait-gates) into the graph
  via apply_architect_dag.
- RE-PLAN: for each work object flagged in `replan_work_ids` (by the evaluator from intake, OR by the
  work_finalizer's verdicts — each rides on its node as `payload.finalizer` with a route), re-invoke the
  architect with the goal + the existing graph and apply the DELTA: ADD the missing steps and ABANDON the
  nodes the situation made moot.

The materializer → action_selector → switchboard → dispatch loop then runs the ready nodes. Never raises — a per-object failure leaves that object as-is
and the pipeline continues.

Inert until the dayflow manager's state_map routes to it.
"""
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.dayflow_orchestrator.work_context import render_view
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)

_MAX_REPLANS_PER_TICK = 3
_TERMINAL_WO_STATES = {"done", "abandoned"}


def _undecomposed_goals(store, created):
    """Recover initial decomposition from durable empty graphs after a lost tick."""
    result = {entry["work_id"]: entry for entry in created}
    for summary in store.list_work_objects():
        if summary.get("status") != "active" or summary["id"] in result:
            continue
        wo = store.load(summary["id"])
        if any(wo.is_work_unit(node) for node in wo.nodes.values()):
            continue
        goal = wo.nodes.get(wo.goal_node_id)
        if goal is not None:
            result[wo.id] = {"work_id": wo.id, "objective": goal.content or goal.title,
                             "rationale": wo.constraints.get("rationale", "")}
    return list(result.values())


def _pending_finalizer_instructions(store) -> dict[str, list[dict]]:
    """Every unconsumed finalizer verdict that asks for something, per work object, off the GRAPH.

    The finalizer judges a result at the tail of a dispatch; the architect acts on it at the head
    of a LATER tick. Nothing in memory bridges that gap — each tick builds its own manager and its
    own Blackboard — so the handoff is persisted on the node the verdict was about
    (nodes.payload.finalizer), next to the step it judged.

    A verdict asks for something when it carries a `next_step`: plan_changes, retry, stop,
    new_approach, or ask_user. `achieved` carries none and is never here. Active work objects
    only; consumed entries are skipped.
    """
    out: dict[str, list[dict]] = {}
    for summary in store.list_work_objects():
        if str(summary.get("status") or "").lower() != "active":
            continue
        wo = store.load(summary["id"])
        for node in wo.nodes.values():
            if not wo.is_work_unit(node):
                continue
            entry = (node.payload or {}).get("finalizer")
            if not isinstance(entry, dict) or entry.get("consumed_at"):
                continue
            if not str(entry.get("next_step") or "").strip():
                continue
            out.setdefault(wo.id, []).append({**entry, "node_id": node.id})
    for entries in out.values():
        entries.sort(key=lambda e: str(e.get("at") or ""))
    return out


def _finalizer_block(entries) -> tuple[str, str]:
    """Render instructions in Jinja and separately prepare the pruning licence data."""
    entries = [e for e in (entries or []) if str(e.get("next_step") or "").strip()]
    if not entries:
        return "", ""
    licence = " ".join(str(e.get("recommendation") or e.get("outcome") or "").strip() for e in entries)
    return render_view("instructions", entries=entries), licence


def _duplicate_pairs(data: dict) -> dict[str, str]:
    """{duplicate_node_id: keep_node_id} from the agent's `duplicate_of` list.

    The form carries a LIST of pairs because OpenAI structured output rejects a free-form object;
    the graph writer wants the lookup, so the shape is converted once, here.
    """
    pairs = data.get("duplicate_of")
    if pairs is None:
        return {}
    if not isinstance(pairs, list):
        raise ValueError("architect: duplicate_of must be a list of pairs")
    out: dict[str, str] = {}
    for pair in pairs:
        if not isinstance(pair, dict):
            raise ValueError("architect: duplicate_of contains a malformed pair")
        dup = pair.get("duplicate_node_id")
        keep = pair.get("keep_node_id")
        if not isinstance(dup, str) or not isinstance(keep, str):
            raise ValueError("architect: duplicate pair IDs must be strings")
        dup, keep = dup.strip(), keep.strip()
        if not dup or not keep or dup == keep:
            raise ValueError("architect: duplicate pair requires distinct nonempty task IDs")
        if dup in out:
            raise ValueError(f"architect: duplicate task {dup!r} is listed more than once")
        out[dup] = keep
    return out


def _render_existing_graph(wo) -> str:
    """Share the complete strategic task view with the steward, excluding provenance."""
    from app.assistant.dayflow_orchestrator.work_context import render_view, work_data
    return render_view("portfolio", work=work_data(wo))


def _situational_context(bb) -> str:
    """Prepare the tick's situational data for the architect's Jinja context block."""
    responses = bb.get_state_value("recent_responded_tickets", {}) or {}
    return render_view("situation",
        responses=[{"category": category, "ticket": ticket}
                   for category in ("accepted", "acknowledged", "declined", "snoozed")
                   for ticket in (responses.get(category) or [])],
        active=bb.get_state_value("active_tickets", []) or [],
        portfolio=bb.get_state_value("work_portfolio", "") or "",
        completed=bb.get_state_value("recent_completed_work", "") or "")


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
            created = _undecomposed_goals(store, created)
            replan_ids = [str(w).strip() for w in (self.blackboard.get_state_value("replan_work_ids", []) or [])
                          if str(w or "").strip()]
            # The finalizer's outstanding verdicts, read off the graph — the authoritative
            # "why re-plan". A work object carrying one needs re-planning whether or not the
            # steward also flagged it: the verdict is a deterministic signal and must not depend
            # on an agent happening to notice the node in the portfolio.
            pending = _pending_finalizer_instructions(store)
            replan_ids = list(dict.fromkeys([*replan_ids, *pending]))
            # Steward-classed user directives: replans that carry out something the USER said. Together
            # with a finalizer verdict these LICENSE the replan to prune queued/held nodes; an unlicensed
            # replan (the steward's own read of progress) may add but not kill — the store's churn
            # fence refuses those prunes. The judgment is the model's; only its transport is typed.
            user_directed = {str(w).strip()
                             for w in (self.blackboard.get_state_value("user_directed_replan_ids", []) or [])
                             if str(w or "").strip()}

            if created or replan_ids:
                scope = self._scope(message)
                info = _situational_context(self.blackboard)
                # Its OWN blackboard, never the tick's. Sharing the tick's looks right — it is
                # what makes blackboard-sourced context items resolve — but the tick's blackboard
                # already carries `task` = "Dayflow cadence tick" (dayflow_tick builds the
                # Message that way), and that value WINS over the Message this node passes. The
                # architect was then asked to decompose "Dayflow cadence tick" with the whole
                # portfolio as context, and dutifully wrote nodes for everything it could see:
                # a picture-day goal acquired an AC setpoint, a whole-house lights ramp and an
                # evening dog walk, two of which failed there and blocked the goal forever.
                #
                # The objective reaches the agent through the Message below. Anything else the
                # architect needs has to arrive the same way (see _situational_context) or as a
                # resource — never by borrowing a blackboard whose keys it does not own.
                agent = DI.agent_factory.create_agent("dayflow_orchestrator::work_architect")

                # CREATE — decompose each freshly-minted goal.
                for c in created:
                    work_id = c["work_id"]
                    try:
                        rationale = str(c.get("rationale") or "").strip()
                        why = render_view("architect_context", rationale=rationale, situation=info)
                        wo = store.load(work_id)
                        goal = wo.nodes.get(wo.goal_node_id)
                        objective = (goal.content or goal.title) if goal else c["objective"]
                        task = render_view("architect_task", mode="create", objective=objective)
                        result = agent.action_handler(Message(task=task, information=why, scope_context=scope))
                        nodes = (getattr(result, "data", {}) or {}).get("nodes", []) or []
                        res = apply_architect_dag(store, work_id, nodes, expected_updated_at=wo.updated_at)
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
                        finalizer_block, licence = _finalizer_block(pending.get(work_id))
                        licensed = bool(licence) or (work_id in user_directed)
                        task = render_view("architect_task", mode="replan", objective=objective,
                                           finalizer_block=finalizer_block, graph=_render_existing_graph(wo))
                        result = agent.action_handler(Message(task=task, information=info, scope_context=scope))
                        data = getattr(result, "data", {}) or {}
                        res = apply_architect_dag(store, work_id, data.get("nodes", []) or [],
                                                  abandon_reason=str(data.get("abandon_reason") or "").strip(),
                                                  abandon_node_ids=data.get("abandon_node_ids", []) or [],
                                                  licensed=licensed,
                                                  duplicate_of=_duplicate_pairs(data),
                                                  finalizer_instructions=pending.get(work_id, []),
                                                  expected_updated_at=wo.updated_at)
                        replanned.append({"work_id": work_id, "added": len(res.get("added", [])),
                                          "abandoned": len(res.get("abandoned", [])),
                                          "deduplicated": len(res.get("deduplicated", []))})
                        logger.info("[%s] re-planned %s: +%d node(s), -%d abandoned (%d duplicate)",
                                    self.name, work_id, len(res.get("added", [])),
                                    len(res.get("abandoned", [])), len(res.get("deduplicated", [])))
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
