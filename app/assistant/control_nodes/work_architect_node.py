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


def _pending_finalizer_instructions(store) -> dict[str, list[dict]]:
    """Every unconsumed finalizer instruction, per work object, read off the GRAPH.

    The finalizer judges a result at the tail of a tick; the architect acts on it at the head of
    a LATER one. Nothing in memory bridges that gap — each tick builds its own manager and its own
    Blackboard (MultiAgentManager.__init__), so the handoff has to be persisted. It is, on the
    node the verdict was about (nodes.payload.finalizer in emi.db), which also puts the judgment
    next to the step it judged instead of in a side channel.

    Active work objects only, and only nodes still carrying an unconsumed entry.
    """
    out: dict[str, list[dict]] = {}
    for summary in store.list_work_objects():
        if str(summary.get("status") or "").lower() != "active":
            continue
        wo = store.load(summary["id"])
        for node in wo.nodes.values():
            entry = (node.payload or {}).get("finalizer")
            if not isinstance(entry, dict) or entry.get("consumed_at"):
                continue
            if not str(entry.get("instruction") or "").strip():
                continue          # proceed/blocked record a verdict but ask for nothing
            out.setdefault(wo.id, []).append({**entry, "node_id": node.id})
    for entries in out.values():
        entries.sort(key=lambda e: str(e.get("at") or ""))
    return out


def _finalizer_block(entries) -> tuple[str, str]:
    """The finalizer's outstanding verdicts for one work object, as the architect reads them.

    Returns ``(prompt_block, instruction)``. The instruction is also the PRUNE LICENCE (see
    `licensed` at the call site), so it is returned separately rather than only rendered.

    Both the instruction and the REASONING go in: the instruction says what should happen, the
    reasoning says why, and a recommendation without its grounds can only be obeyed or ignored,
    never judged. What the architect then DOES about it is its own business — it has the
    primitives and does not need them listed. The wording follows the verdict, because 'a
    just-completed step' is the wrong thing to say about one that failed.

    Normally there is exactly one. Several accumulate only when re-planning failed for a few
    ticks, and then they are all still live — each is about a different step.
    """
    blocks, instructions = [], []
    for entry in entries or []:
        instruction = str(entry.get("instruction") or "").strip()
        if not instruction:
            continue
        reasoning = str(entry.get("reasoning") or "").strip()
        node_id = str(entry.get("node_id") or "").strip()
        if str(entry.get("verdict") or "") == "replan":
            lead = f"The step {node_id} FAILED and is back in your inbox."
        else:
            lead = f"The step {node_id} ran, and its result changed the plan."
        block = f"{lead}\n"
        if reasoning:
            block += f"WHY (the finalizer, having read the full result): {reasoning}\n"
        block += f"WHAT SHOULD NOW HAPPEN: {instruction}\n"
        blocks.append(block)
        instructions.append(instruction)
    if not blocks:
        return "", ""
    return "\n".join(blocks) + "\n", " ".join(instructions)


def _duplicate_pairs(data: dict) -> dict[str, str]:
    """{duplicate_node_id: keep_node_id} from the agent's `duplicate_of` list.

    The form carries a LIST of pairs because OpenAI structured output rejects a free-form object;
    the graph writer wants the lookup, so the shape is converted once, here.
    """
    out: dict[str, str] = {}
    for pair in (data.get("duplicate_of") or []):
        if not isinstance(pair, dict):
            continue
        dup = str(pair.get("duplicate_node_id") or "").strip()
        keep = str(pair.get("keep_node_id") or "").strip()
        if dup and keep and dup != keep:
            out[dup] = keep
    return out


def _render_existing_graph(wo) -> str:
    """The architect's OWN units, in full, for the re-plan prompt.

    This is the only thing standing between a re-plan and a duplicate, so it shows the things
    you need to recognise your own work: the node_id to reuse, the full DETAIL (what the node is
    actually for — two nodes titled "Give the user the assessment" are indistinguishable by title
    alone), and whether the node is still live.

    It shows ONLY direct children of the goal. Everything below them is the worker's own
    decomposition, which the architect neither writes nor prunes, and which drowned the list it
    was supposed to read: one work object rendered 49 lines, 41 of them worker-grown subtasks and
    evidence rows, with the architect's 8 real units scattered through them. The subtree is
    reported as a count instead.

    LIVE nodes come first and are never abbreviated — a duplicate is created against the live
    set, so that is the part that must be impossible to miss. Finished nodes follow as a record,
    with their epitaphs (also never truncated: a cut-off "user declined ... DO NOT RE" is how
    dead chains get re-laid).
    """
    _FINISHED = {"done", "closed", "abandoned", "superseded", "failed"}
    live, finished = [], []
    for n in wo.nodes.values():
        if n.id == wo.goal_node_id or n.parent_id != wo.goal_node_id:
            continue
        kids = [wo.nodes[c] for c in wo.children_of(n.id) if c in wo.nodes]
        # Steps and RESULTS are counted separately. Lumping them reads a pile of failure receipts
        # as progress: one delivery node showed "17 sub-nodes" that were 17 evidence rows all
        # carrying the SAME tool error, and that count was then used to judge which of two
        # duplicates was further along. Distinct bodies, because N copies of one error is one
        # thing that happened N times, not N things.
        steps = [k for k in kids if k.type == "subtask"]
        results = [k for k in kids if k.type in ("evidence", "artifact")]
        distinct = len({" ".join((k.content or "").split()) for k in results if (k.content or "").strip()})
        sub = ""
        if steps:
            sub += f" | {len(steps)} step(s) below"
        if results:
            sub += f" | {len(results)} result(s)"
            if distinct == 1 and len(results) > 1:
                sub += " — ALL IDENTICAL (the same thing happened repeatedly; this is not progress)"
        wake = ""
        if getattr(n, "wake_kind", None):
            when = getattr(n, "wake_at", None)
            wake = f" | wake={n.wake_kind}" + (f" at {when.isoformat()}" if when else "")
        failed_n = int((n.payload or {}).get("failure_count") or 0)
        if failed_n >= 2:
            sub += (f" | HAS FAILED {failed_n} TIMES — re-adding this work in any form will most "
                    f"likely fail again; stop, or plan a node that asks the user to unblock it")
        if n.status in _FINISHED:
            term = (n.payload or {}).get("terminal") if hasattr(n, "payload") else None
            why = f"\n    why: {term.get('reason')}" if isinstance(term, dict) else ""
            finished.append(f"  - {n.id} | {n.title} | status={n.status}{sub}{why}")
        else:
            detail = " ".join((n.content or "").split())
            live.append(f"  - {n.id} | status={n.status}{wake}{sub}\n"
                        f"    title : {n.title}\n"
                        f"    detail: {detail or '(none)'}")

    if not live and not finished:
        return "(no nodes yet)"
    out = []
    if live:
        out.append("LIVE — these WILL run. Reuse these node_ids; do not write a second node for "
                   "work one of them already covers:\n" + "\n".join(live))
    if finished:
        out.append("FINISHED — a record of what already happened. Do not re-add these:\n"
                   + "\n".join(finished))
    return "\n\n".join(out)


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
            # The finalizer's outstanding instructions, read off the graph — the authoritative
            # "why re-plan". A work object carrying one needs re-planning whether or not the
            # steward also flagged it: the verdict is a deterministic signal and must not depend
            # on an agent happening to notice the node in the portfolio.
            amend_intents = _pending_finalizer_instructions(store)
            replan_ids = list(dict.fromkeys([*replan_ids, *amend_intents]))
            # Steward-classed user directives: replans that carry out something the USER said. Together
            # with a finalizer amend these LICENSE the replan to prune queued/held nodes; an unlicensed
            # replan (the steward's own read of progress) may add but not kill — the store's churn
            # fence refuses those prunes. The judgment is the model's; only its transport is typed.
            user_directed = {str(w).strip()
                             for w in (self.blackboard.get_state_value("user_directed_replan_ids", []) or [])
                             if str(w or "").strip()}

            if created or replan_ids:
                scope = self._scope(message)
                info = _situational_context(self.blackboard)
                # Pass THIS tick's blackboard. create_agent() builds an empty one when given
                # nothing (`blackboard or Blackboard()`), which silently emptied every
                # blackboard-sourced context item the architect declares — `admitted_artifacts`
                # resolved to nothing for as long as it has been listed, and the schedule had to
                # come from the raw calendar resource because the steward's view was unreachable.
                # The steward's prep node loaded all of it earlier this same tick.
                agent = DI.agent_factory.create_agent("dayflow_orchestrator::work_architect",
                                                      self.blackboard)

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
                        amend_block, amend = _finalizer_block(amend_intents.get(work_id))
                        licensed = bool(amend) or (work_id in user_directed)
                        task = (
                            f"{objective}\n\n{amend_block}This goal ALREADY has a work graph (below). Revise it "
                            f"as a DELTA per the intent above and the CONTEXT: ADD the steps still missing, and "
                            f"ABANDON (list their node_ids in abandon_node_ids) only nodes the EVIDENCE — "
                            f"epitaphs, recorded results, user directives — makes moot or wrong; un-finished "
                            f"sub-steps go with them. Queued (actionable) and held (future-wake) nodes are the "
                            f"runtime's: they WILL run — never prune one for slowness. Do NOT recreate "
                            f"existing nodes (reference their node_ids in depends_on). Output the delta only. "
                            f"Existing nodes:\n{_render_existing_graph(wo)}"
                        )
                        result = agent.action_handler(Message(task=task, information=info, scope_context=scope))
                        data = getattr(result, "data", {}) or {}
                        res = apply_architect_dag(store, work_id, data.get("nodes", []) or [],
                                                  abandon_reason=str(data.get("abandon_reason") or "").strip(),
                                                  abandon_node_ids=data.get("abandon_node_ids", []) or [],
                                                  licensed=licensed,
                                                  duplicate_of=_duplicate_pairs(data))
                        replanned.append({"work_id": work_id, "added": len(res.get("added", [])),
                                          "abandoned": len(res.get("abandoned", [])),
                                          "deduplicated": len(res.get("deduplicated", []))})
                        logger.info("[%s] re-planned %s: +%d node(s), -%d abandoned (%d duplicate)",
                                    self.name, work_id, len(res.get("added", [])),
                                    len(res.get("abandoned", [])), len(res.get("deduplicated", [])))
                        # Acted on — stamp each instruction so the next re-plan of this object does
                        # not re-apply a judgment about a step already dealt with. Only after the
                        # graph write succeeded: a replan that raised must be able to run again.
                        for entry in amend_intents.get(work_id, []):
                            try:
                                store.apply("consume_finalizer_instruction",
                                            {"work_id": work_id, "node_id": entry["node_id"]},
                                            actor="architect")
                            except Exception as e:
                                logger.error("[%s] could not consume the finalizer instruction on "
                                             "%s::%s: %s", self.name, work_id, entry["node_id"], e)
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
