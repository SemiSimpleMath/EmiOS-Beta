"""Decompose freshly-created goals into their DAG, and own each goal to its end (Part 2 of the split planner).

The architect owns a work object from creation to end (docs/design/dayflow_goal_ownership_2026-09-30.md):
the steward decides what work exists; everything that happens to a work object after, including its
end, is the architect's. Runs right after strategic_planner_wo_persist_node. Per tick, for ONE work
object at a time:
- CREATE: for each work object the evaluator created this tick, invoke dayflow_orchestrator::work_architect
  on its objective and lay the resulting DAG (subtask nodes, depends_on edges, wait-gates) into the graph
  via apply_architect_dag.
- RE-PLAN, woken per work object by: a finalizer verdict that asks for something (on its node as
  `payload.finalizer`); an instruction to end it (the steward's, or the brain's for a cancelled list entry) or the
  brain's instruction to re-plan it (its list entry revised) (`constraints.architect_instructions`); the
  steward's `replan_work_ids` (intake, a user directive); or the work object being STRANDED (nothing in
  it can still run and no scheduled time lies ahead, `stranded_fingerprint`). It re-invokes the architect
  with the goal + the existing graph and applies the DELTA: add the missing steps, abandon the moot ones,
  revise the objective, or end the goal. An ended goal's concern and belief feedback is delivered after
  the commit.

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


_LIVE = {"proposed", "waiting", "actionable", "dispatched"}
_DEAD = {"abandoned", "failed", "superseded"}
_OUTSIDE_WAKES = {"event", "signal", "user_reply"}


def stranded_fingerprint(wo) -> str | None:
    """The task state of a work object in which nothing can move it any more, or None.

    Stranded (owner, 2026-09-30: every scheduled time in it has passed with nothing settling it): active,
    has tasks, no judgment pending (a result not yet judged, or a finalizer instruction not yet acted
    on), and no task that can still run: none in flight, none waiting on a future time or on the
    outside world, none runnable now. A task whose prerequisite is abandoned or failed can never run.
    The fingerprint names that state; a state already reviewed (`constraints.stranded_review`) is not
    stranded again until it changes.
    """
    if str(wo.status or "").lower() != "active" or wo.has_pending_revision():
        return None
    units = [n for n in wo.nodes.values() if wo.is_work_unit(n)]
    if not units:
        return None
    goal = wo.nodes.get(wo.goal_node_id)
    if goal is not None and wo.is_satisfied(goal):
        return None
    for n in units:
        if n.status in {"done", "failed"} and not (n.payload or {}).get("finalizer"):
            return None                                   # its result is not judged yet
        if n.status not in _LIVE:
            continue
        if n.status == "dispatched" or n.wake_kind in _OUTSIDE_WAKES:
            return None
        if any(wo.nodes[d].status in _DEAD for d in wo.deps_of(n.id) if d in wo.nodes):
            continue                                      # can never run
        return None                                       # runnable now, or when its time comes
    fingerprint = "|".join(sorted(f"{n.id}:{n.status}" for n in units))
    if ((wo.constraints or {}).get("stranded_review") or {}).get("fingerprint") == fingerprint:
        return None
    return fingerprint


def _goal_triggers(store) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Per active work object: the steward's unconsumed end instructions, and the stranded state."""
    instructions: dict[str, list[dict]] = {}
    stranded: dict[str, str] = {}
    for summary in store.list_work_objects():
        if str(summary.get("status") or "").lower() != "active":
            continue
        wo = store.load(summary["id"])
        pending = [e for e in (wo.constraints or {}).get("architect_instructions") or []
                   if not e.get("consumed_at")]
        if pending:
            instructions[wo.id] = pending
        fingerprint = stranded_fingerprint(wo)
        if fingerprint:
            stranded[wo.id] = fingerprint
    return instructions, stranded


def _finalizer_block(entries, labels=None) -> tuple[str, str]:
    """Render instructions in Jinja and separately prepare the pruning licence data."""
    entries = [e for e in (entries or []) if str(e.get("next_step") or "").strip()]
    if not entries:
        return "", ""
    licence = " ".join(str(e.get("recommendation") or e.get("outcome") or "").strip() for e in entries)
    return render_view("instructions", entries=entries, labels=labels or {}), licence


# ── the projection (docs/design/objects_and_projections_2026-10-08.md, module 3) ─────────────────────────────
# The architect sees its goal G, every task as T, other work as W, the sources work came from as S and the records
# they point to as P, reminders as R; never an id. Its answer names existing tasks and other objects only by label
# (depends_on, uses, abandon_node_ids, duplicate_of, covered_by); a new task keeps the short node_id it chooses.

_USES_KINDS = ("task", "pod", "work", "source_item")


def _projection(wo, others, ended_ids):
    """The labels of one architect call: (labels: label -> [kind, id], ids: id -> label for the templates)."""
    from app.assistant.dayflow_orchestrator.work_context import source_context
    from app.assistant.subconscious.projection import Projection
    p = Projection()
    p.fixed("G", "work", wo.id)

    def objects_of(w):
        for n in sorted((n for n in w.nodes.values() if w.is_work_unit(n)), key=lambda n: str(n.created_at)):
            p.label("T", "task", n.id)
        for s in source_context(w):
            if s.get("item_id"):
                p.label("S", "source_item", s["item_id"])
            if s.get("pod_id"):
                p.label("P", "pod", s["pod_id"])
    objects_of(wo)
    for other in others:
        if other.id != wo.id:
            p.label("W", "work", other.id)
            objects_of(other)
    for work_id in ended_ids:
        p.label("W", "work", work_id)
    return p, {ref[1]: label for label, ref in p.labels.items()}


def _view(store, wo, *, history, replan: bool):
    """The architect's situational context for one goal, as a projection: (labels, ids, text): the portfolio of
    active work (on a re-plan this goal is marked, not repeated: its full graph is in the task; until 2026-10-08 it
    was printed twice in a ~96k-character prompt), the work completed recently, and the reminders sent.

    No tickets: the architect owns one goal, and the user's replies about it are recorded on the step that asked
    (evidence the finalizer judges). The tick's list of every answered ticket once reached every architect call as
    "directives"; on 2026-10-01 a reply to a test goal's question ("this task should be aborted") ended the goal that
    had just emailed a family member."""
    from app.assistant.control_nodes.strategic_planner_wo_prep_node import recent_ended_items, render_recent_ended
    from app.assistant.dayflow_orchestrator.work_portfolio import active_work_objects, render_portfolio
    summaries = store.list_work_objects()
    active, unreadable = active_work_objects(store, summaries)
    done, _ = recent_ended_items(summaries, store)
    p, ids = _projection(wo, active, [item["id"] for item in done])
    portfolio = render_portfolio(active, shown_above=wo.id if replan else None, labels=ids)
    if unreadable:
        portfolio = render_view("incomplete_portfolio", unreadable=unreadable, portfolio=portfolio)
    completed, _ = render_recent_ended(done, [], labels=ids)
    reminders = None
    if history:
        reminders = {**history, "records": [{**r, "ref": p.label("R", "reminder", r["id"])}
                                            for r in history.get("records") or []]}
        ids = {ref[1]: label for label, ref in p.labels.items()}
    text = render_view("situation", scheduled_reminder_history=reminders, portfolio=portfolio, completed=completed)
    return p.labels, ids, text


def _resolve(d: dict, labels: dict, wo) -> tuple[dict, dict]:
    """The architect's answer with its labels mapped to ids: (the answer for apply_architect_dag, links to keep as
    relations: uses per new task, covered_by, duplicates). Raises ValueError naming every problem, which the
    one correction round shows the architect: a label not given, of the wrong kind, or written into words."""
    from app.assistant.subconscious.projection import labels_in
    own = {label: ref[1] for label, ref in labels.items() if ref[0] == "task" and ref[1] in wo.nodes}
    new = [str(n.get("node_id") or "").strip() for n in d.get("nodes") or [] if isinstance(n, dict)]
    numbered = [x for x in labels if any(ch.isdigit() for ch in x)]
    problems: list[str] = []

    def words(where, *texts):
        found = labels_in(" ".join(str(t or "") for t in texts), numbered)
        if found:
            problems.append(f"{where}: {found} written into its words; say it in words and put the reference in "
                            f"its own field (depends_on, uses, abandon_node_ids, duplicate_of, covered_by)")

    nodes, uses = [], {}
    for spec in d.get("nodes") or []:
        if not isinstance(spec, dict):
            continue
        slug = str(spec.get("node_id") or "").strip()
        if slug in labels:
            problems.append(f"{slug!r}: a new task's node_id is a short name you choose, not a label")
        deps = []
        for x in [str(x).strip() for x in spec.get("depends_on") or []]:
            if x in own:
                deps.append(own[x])
            elif x in new:
                deps.append(x)
            else:
                problems.append(f"{slug}: depends_on {x!r} is neither a task of this goal (its T label) nor a new "
                                f"task of this answer (its node_id)")
        refs = []
        for x in [str(x).strip() for x in spec.get("uses") or []]:
            if x in new:
                refs.append(("new", x))
            elif x in labels and labels[x][0] in _USES_KINDS and labels[x][1] != wo.id:   # not the goal itself
                refs.append((labels[x][0], labels[x][1]))
            else:
                problems.append(f"{slug}: uses {x!r} is not a task, record, source or work given")
        uses[slug] = refs
        words(slug, spec.get("title"), spec.get("detail"), spec.get("wait_reason"), spec.get("wake_ref"))
        nodes.append({**spec, "depends_on": deps})
    abandon = []
    for x in [str(x).strip() for x in d.get("abandon_node_ids") or []]:
        if x in own:
            abandon.append(own[x])
        else:
            problems.append(f"abandon_node_ids: {x!r} is not a task of this goal (its T label)")
    pairs = []
    for pair in d.get("duplicate_of") or []:
        dup, keep = str((pair or {}).get("duplicate_node_id") or "").strip(), str((pair or {}).get("keep_node_id") or "").strip()
        if dup in own and keep in own:
            pairs.append({"duplicate_node_id": own[dup], "keep_node_id": own[keep]})
        else:
            problems.append(f"duplicate_of: {dup!r} and {keep!r} must both be tasks of this goal (their T labels)")
    covered = []
    end = d.get("end_goal")
    if end:
        for x in [str(x).strip() for x in end.get("covered_by") or []]:
            if x in labels and labels[x][0] == "work" and labels[x][1] != wo.id:
                covered.append(labels[x][1])
            else:
                problems.append(f"end_goal covered_by: {x!r} is not other work given (its W label)")
        words("end_goal", end.get("reason"))
    revision = d.get("revise_objective")
    if revision:
        words("revise_objective", revision.get("objective"), revision.get("success_criteria"), revision.get("reason"))
    words("abandon_reason", d.get("abandon_reason"))
    words("architect_summary", d.get("architect_summary"))
    if problems:
        raise ValueError("; ".join(problems))
    mapped = {**d, "nodes": nodes, "abandon_node_ids": abandon, "duplicate_of": pairs}
    if end:
        mapped["end_goal"] = {k: v for k, v in end.items() if k != "covered_by"}
    return mapped, {"uses": uses, "covered_by": covered,
                    "duplicates": [(p["duplicate_node_id"], p["keep_node_id"]) for p in pairs]}


def _keep_links(wo, links: dict, added: list) -> None:
    """After the revision commits: what each new task uses, what the goal's ending was covered by, and each dropped
    duplicate's kept task, as relations (subconscious/relations.py)."""
    from datetime import datetime, timezone
    from app.assistant.dayflow_orchestrator.work_architect_apply import _work_ns
    from app.assistant.subconscious import relations
    from app.assistant.subconscious.db import connect
    ns, existing = _work_ns(wo.id), set(wo.nodes)
    real = lambda slug: slug if slug in existing else f"{slug}--{ns}"
    now, by = datetime.now(timezone.utc).isoformat(), f"architect {wo.id}"
    relations.ensure_schema()
    with connect(True) as c:
        for slug, refs in links["uses"].items():
            if refs and real(slug) in added:
                relations.replace(c, ("task", real(slug)), "uses",
                                  [("task", real(x)) if kind == "new" else (kind, x) for kind, x in refs], at=now, by=by)
        if links["covered_by"]:
            relations.replace(c, ("work", wo.id), "covered_by", [("work", w) for w in links["covered_by"]],
                              at=now, by=by)
        for dup, keep in links["duplicates"]:
            relations.replace(c, ("task", dup), "duplicate_of", [("task", keep)], at=now, by=by)


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


def _render_existing_graph(wo, ids=None) -> str:
    """The goal's own graph, main tasks only (provenance excluded), each object by its label of the call."""
    from app.assistant.dayflow_orchestrator.work_context import render_view, work_data
    return render_view("portfolio", work=work_data(wo), labels=ids or {})


def _apply_with_correction(agent, data: dict, task: str, information: str, scope, apply):
    """Apply the architect's answer; when the graph refuses it (ValueError/KeyError from the revision:
    an invalid or cyclic dependency, a refused prune, an invalid duplicate pair), ask once more with the
    refusal, then apply that. A second refusal raises. Until 2026-10-05 the first refusal lost the whole
    revision with an ERROR the architect never saw. Returns (data, result of apply)."""
    try:
        return data, apply(data)
    except (ValueError, KeyError) as refusal:
        logger.warning("[work_architect_node] revision refused, one correction: %s", refusal)
        correction = render_view("architect_refused", reason=str(refusal))
        result = agent.action_handler(Message(task=task + correction, information=information, scope_context=scope))
        data = getattr(result, "data", {}) or {}
        return data, apply(data)


def _deliver_ending(store, work_id: str, status: str) -> None:
    """After an ending commits: the concerns and beliefs the work served get its outcome. Both
    deliveries are receipt-backed; a failure leaves the receipt for evaluator prep to retry."""
    from app.assistant.subconscious.concern_feedback import propagate_work_outcome
    from belief_engine.work_feedback import propagate_work_outcome_to_beliefs
    propagate_work_outcome(store, work_id, status)
    try:
        propagate_work_outcome_to_beliefs(store, work_id, status)
    except Exception:
        logger.error("[architect] work %s ended %s; belief feedback failed", work_id, status, exc_info=True)


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
            instructions, stranded = _goal_triggers(store)
            # Judgments and the steward's instructions first, then the steward's flags, then stranded goals.
            replan_ids = list(dict.fromkeys([*pending, *instructions, *replan_ids, *stranded]))
            # Steward-classed user directives: replans that carry out something the USER said. Together
            # with a finalizer verdict these LICENSE the replan to prune queued/held nodes; an unlicensed
            # replan (the steward's own read of progress) may add but not kill — the store's churn
            # fence refuses those prunes. The judgment is the model's; only its transport is typed.
            user_directed = {str(w).strip()
                             for w in (self.blackboard.get_state_value("user_directed_replan_ids", []) or [])
                             if str(w or "").strip()}

            if created or replan_ids:
                scope = self._scope(message)
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
                # architect needs has to arrive the same way (see _view) or as a
                # resource — never by borrowing a blackboard whose keys it does not own.
                agent = DI.agent_factory.create_agent("dayflow_orchestrator::work_architect")

                history = self.blackboard.get_state_value("scheduled_reminder_history")
                # CREATE — decompose each freshly-minted goal.
                for c in created:
                    work_id = c["work_id"]
                    try:
                        rationale = str(c.get("rationale") or "").strip()
                        wo = store.load(work_id)
                        labels, ids, goal_view = _view(store, wo, history=history, replan=False)
                        why = render_view("architect_context", rationale=rationale, situation=goal_view)
                        goal = wo.nodes.get(wo.goal_node_id)
                        objective = (goal.content or goal.title) if goal else c["objective"]
                        task = render_view("architect_task", mode="create", objective=objective)
                        result = agent.action_handler(Message(task=task, information=why, scope_context=scope))
                        data = getattr(result, "data", {}) or {}
                        # A new goal is planned or ended, never left empty: an empty one comes back every tick
                        # (2026-10-05: on goals other work already covered, the architect answered "keep the
                        # existing task" with no steps and no ending). One correction, then it is an error.
                        if not (data.get("nodes") or data.get("end_goal")):
                            correction = render_view("architect_empty_create",
                                                     summary=str(data.get("architect_summary") or "").strip())
                            result = agent.action_handler(Message(task=task + correction, information=why,
                                                                  scope_context=scope))
                            data = getattr(result, "data", {}) or {}
                            if not (data.get("nodes") or data.get("end_goal")):
                                raise ValueError(f"architect planned no steps and did not end {work_id}, after one "
                                                 f"correction: {data.get('architect_summary')!r}")
                        # A goal can be moot or need rewording before anything is planned. Until 2026-10-04 only
                        # the re-plan branch passed these on: a task-less goal the architect ended ("its useful
                        # moment has passed") stayed active and was decomposed again every tick, from 10-02 on.
                        def apply(d, wo=wo, work_id=work_id, labels=labels):
                            d, _ = _resolve(d, labels, wo)
                            return apply_architect_dag(store, work_id, d.get("nodes", []) or [],
                                                       revise_objective=d.get("revise_objective"),
                                                       end_goal=d.get("end_goal"),
                                                       expected_updated_at=wo.updated_at)
                        data, res = _apply_with_correction(agent, data, task, why, scope, apply)
                        _keep_links(wo, _resolve(data, labels, wo)[1], res.get("added", []))
                        decomposed.append({"work_id": work_id, "nodes": len(res.get("added", [])),
                                           "ended": res.get("ended")})
                        logger.info("[%s] decomposed %s into %d node(s), revised=%s ended=%s",
                                    self.name, work_id, len(res.get("added", [])), bool(res.get("revised")),
                                    res.get("ended"))
                        if res.get("ended"):
                            _deliver_ending(store, work_id, res["ended"])
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
                        labels, ids, goal_info = _view(store, wo, history=history, replan=True)
                        finalizer_block, licence = _finalizer_block(pending.get(work_id), ids)
                        # A stranded goal's prunes are licensed by its state: nothing in it can run. The brain's
                        # re-plan instruction (its list entry revised) is a directive, like the user's.
                        brain_replan = any(e.get("kind") == "replan" for e in instructions.get(work_id, []))
                        licensed = bool(licence) or (work_id in user_directed) or (work_id in stranded) or brain_replan
                        task = render_view("architect_task", mode="replan", objective=objective,
                                           finalizer_block=finalizer_block, graph=_render_existing_graph(wo, ids),
                                           goal_instructions=instructions.get(work_id, []),
                                           rationale=str((wo.constraints or {}).get("rationale") or "").strip(),
                                           stranded=work_id in stranded)
                        result = agent.action_handler(Message(task=task, information=goal_info, scope_context=scope))
                        data = getattr(result, "data", {}) or {}

                        def apply(d, wo=wo, work_id=work_id, licensed=licensed, labels=labels):
                            d, _ = _resolve(d, labels, wo)
                            return apply_architect_dag(store, work_id, d.get("nodes", []) or [],
                                                       abandon_reason=str(d.get("abandon_reason") or "").strip(),
                                                       abandon_node_ids=d.get("abandon_node_ids", []) or [],
                                                       licensed=licensed,
                                                       duplicate_of=_duplicate_pairs(d),
                                                       finalizer_instructions=pending.get(work_id, []),
                                                       goal_instructions=instructions.get(work_id, []),
                                                       review_fingerprint=stranded.get(work_id),
                                                       revise_objective=d.get("revise_objective"),
                                                       end_goal=d.get("end_goal"),
                                                       expected_updated_at=wo.updated_at)
                        data, res = _apply_with_correction(agent, data, task, goal_info, scope, apply)
                        _keep_links(wo, _resolve(data, labels, wo)[1], res.get("added", []))
                        replanned.append({"work_id": work_id, "added": len(res.get("added", [])),
                                          "abandoned": len(res.get("abandoned", [])),
                                          "deduplicated": len(res.get("deduplicated", [])),
                                          "revised": res.get("revised"), "ended": res.get("ended")})
                        logger.info("[%s] re-planned %s: +%d node(s), -%d abandoned (%d duplicate), "
                                    "revised=%s ended=%s", self.name, work_id, len(res.get("added", [])),
                                    len(res.get("abandoned", [])), len(res.get("deduplicated", [])),
                                    bool(res.get("revised")), res.get("ended"))
                        if res.get("ended"):
                            _deliver_ending(store, work_id, res["ended"])
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
