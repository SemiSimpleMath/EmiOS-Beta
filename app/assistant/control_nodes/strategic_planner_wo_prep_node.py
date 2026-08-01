"""Pre-LLM prep node for strategic_planner_wo (the work-object steward).

Builds the steward's context: the ACTIVE work objects rendered as `work_portfolio` (its primary input),
plus the situational context that lets it judge timing and act on user decisions — active tickets,
recent ticket responses, recent dispatch results, recent action-selector nudges. The situational
builders live in this module (relocated when the legacy strategic_planner_prep_node was retired — this
prep is now their sole user). Resource-backed context (schedule/presence/routine/etc.) is
auto-resolved by the context injector and needs no building here.

Inert until the dayflow manager's state_map routes to it.
"""
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_TERMINAL_WO_STATES = {"done", "abandoned"}
_ACTION_LOG_HOURS = 18
_DAYFLOW_TYPES = frozenset({"dayflow_orchestrator"})


# Situational-context builders — relocated here when the legacy strategic_planner_prep_node was retired
# (this work-object steward prep is now their sole user).
def _load_active_tickets():
    """Active dayflow tickets from the ticket manager (pending/proposed/snoozed)."""
    from app.assistant.ticket_manager import get_ticket_manager, TicketState
    active = get_ticket_manager().get_tickets(
        states=[TicketState.PENDING, TicketState.PROPOSED, TicketState.SNOOZED], limit=50)
    result = []
    for t in active:
        if (getattr(t, "ticket_type", "") or "").lower() not in _DAYFLOW_TYPES:
            continue
        trigger_context = getattr(t, "trigger_context", None) or {}
        if not isinstance(trigger_context, dict):
            trigger_context = {}
        acted_on = trigger_context.get("acted_on_item_ids", [])
        if not isinstance(acted_on, list):
            acted_on = []
        result.append({
            "title": str(getattr(t, "title", "") or "").strip(),
            "message": str(getattr(t, "message", "") or "").strip(),
            "suggestion_type": str(getattr(t, "suggestion_type", "") or "").strip(),
            "acted_on_item_ids": acted_on,
        })
    return result


def _load_responded_tickets(since_utc):
    """Recent dayflow ticket responses, categorized by action."""
    from app.assistant.pipelines.dayflow.utils.context_sources import get_responded_tickets_categorized
    return get_responded_tickets_categorized(since_utc=since_utc, ticket_type="dayflow_orchestrator")


def _work_status_by_id() -> dict:
    """WorkObject id -> status, one summaries fetch. The id-chain resolvers below join
    against this instead of asking the evaluator to reconstruct links from wording."""
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return {s["id"]: str(s.get("status") or "unknown")
            for s in get_dayflow_work_store().list_work_objects()}


def _annotate_work_refs(rows, status_by_id) -> None:
    """Resolve each ticket-reply row's recorded work edge (ticket.trigger_context.work_node,
    written at ticket creation) to its work object and current status. Rows without the
    edge stay unannotated; an id that no longer resolves renders as unresolved — sink,
    not drop."""
    for row in rows:
        ref = str(row.get("work_node") or "")
        if "::" not in ref:
            continue
        work_id = ref.split("::", 1)[0]
        row["work_ref"] = f"{work_id} — {status_by_id.get(work_id, 'unresolved')}"


def _resolve_ticket_provenance(ticket_id: str, ticket_manager, status_by_id) -> str:
    """Chase a schedule entry's verbatim ticket id to the originating work object:
    ticket -> trigger_context.work_node -> work object -> current status, plus the
    user's recorded response. Pure lookups on edges recorded at write time."""
    if not ticket_id:
        return "source ticket id empty — unresolved"
    ticket = ticket_manager.get_ticket_by_id(ticket_id)
    if ticket is None:
        return f"source ticket:{ticket_id} unresolved"
    trigger_context = getattr(ticket, "trigger_context", None)
    ref = str(trigger_context.get("work_node") or "") if isinstance(trigger_context, dict) else ""
    if "::" in ref:
        work_id = ref.split("::", 1)[0]
        out = f"outcome of {work_id} — {status_by_id.get(work_id, 'unresolved')}"
    else:
        out = f"from ticket {ticket_id}"
    action = str(getattr(ticket, "user_action", "") or "").strip()
    if action:
        out += f"; user {action}"
    return out


def _abandoned_line(summary, wo) -> str:
    """One RECENTLY DROPPED entry: title + when + WHY.

    The reason is the load-bearing part — the last user reply recorded in the
    graph and the goal node's final content. Without it the evaluator re-mints
    a goal the user just settled (the 2026-07-27 July-timesheets double
    reminder: the WO was dropped on "those are done first of next month
    always", the DROPPED section showed only the bare title, and an identical
    WO was created 11 minutes later)."""
    from app.assistant.utils.time_utils import parse_iso_utc, utc_to_local

    title = str(summary.get("title") or "").strip()
    when = parse_iso_utc(str(summary.get("updated_at") or ""))
    when_s = utc_to_local(when).strftime("%a %I:%M %p") if when else ""
    line = f"- {title}" + (f"  (dropped {when_s})" if when_s else "")
    if wo is None:
        return line
    nodes = getattr(wo, "nodes", {}) or {}
    replies = [
        n for n in nodes.values()
        if getattr(n, "type", "") == "evidence"
        and getattr(n, "created_by", "") == "reply"
        and (getattr(n, "content", "") or "").strip()
    ]
    if replies:
        # Nodes rebuild in event order — the last reply is the newest, the
        # user's final word on this goal.
        text = (replies[-1].content or "").strip().replace("\n", " ")
        line += f"\n  the user's last word on it: \"{text}\""
    goal = nodes.get(getattr(wo, "goal_node_id", "") or "")
    goal_content = (getattr(goal, "content", "") or "").strip().replace("\n", " ") if goal else ""
    if goal_content and goal_content.lower() != title.lower():
        line += f"\n  final note on the goal: {goal_content}"
    return line


def _build_recent_dispatch_results(all_items, now_utc, *, max_age_hours=6, limit=10):
    """Recently-completed dispatches with their full manager result text, so the evaluator can act on what
    came back instead of treating it as ambient context. Newest first, capped at `limit`."""
    from datetime import timedelta
    from app.assistant.dayflow_orchestrator.contracts import get_meta
    from app.assistant.utils.time_utils import parse_iso_utc
    cutoff = now_utc - timedelta(hours=max_age_hours)
    out = []
    seen_task_ids = set()
    try:
        from app.assistant.chat_narrator.display_names import display_name_for
    except Exception:
        display_name_for = lambda x: x  # type: ignore[assignment]
    for item in all_items:
        meta = get_meta(item)
        dispatched_to = str(meta.get("dispatched_to") or "").strip()
        execution_result = str(meta.get("execution_result") or "").strip()
        if not dispatched_to or not execution_result:
            continue
        lower_exec = execution_result.lower()
        if "inbound ui chat message from system" in lower_exec and "cadence tick" in lower_exec:
            continue
        dispatched_at = parse_iso_utc(str(meta.get("dispatched_at") or ""))
        if dispatched_at is None or dispatched_at < cutoff:
            continue
        task_id = str(meta.get("short_id") or meta.get("task_id") or meta.get("item_id") or item.get("id") or "").strip()
        if task_id and task_id in seen_task_ids:
            continue
        if task_id:
            seen_task_ids.add(task_id)
        friendly = display_name_for(dispatched_to)
        out.append({
            "task_id": task_id,
            "summary": str(meta.get("summary") or "").strip(),
            "dispatched_to": dispatched_to,
            "dispatched_to_display": (friendly if friendly and friendly.lower() != dispatched_to.lower() else ""),
            "dispatched_at": str(meta.get("dispatched_at_local") or meta.get("dispatched_at") or "").strip(),
            "execution_result": execution_result,
            "pod_references": meta.get("pod_references") or [],
        })
    out.sort(key=lambda r: r.get("dispatched_at") or "", reverse=True)
    return out[:limit]


class StrategicPlannerWoPrepNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        # 1) The portfolio (active work objects) + the DONE-LOG (recently completed/abandoned) — the
        #    evaluator's primary inputs. The done-log is its memory so it does NOT recreate work it just
        #    finished (especially recurring routine automations that re-appear in the routine each tick).
        portfolio = "(no active work objects)"
        recent_completed, recent_abandoned = "(none)", ""
        n_active = 0
        try:
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            from app.assistant.dayflow_orchestrator.work_portfolio import render_portfolio
            store = get_dayflow_work_store()
            summaries = store.list_work_objects()   # newest-updated first
            active = [store.load(s["id"]) for s in summaries
                      if str(s.get("status") or "").lower() not in _TERMINAL_WO_STATES]
            n_active = len(active)
            portfolio = render_portfolio(active)
            recent_completed, recent_abandoned = self._render_recent_completed(summaries, store)
        except Exception as e:
            logger.error("[%s] portfolio build failed: %s", self.name, e)
            logger.debug("[%s] portfolio build exception", self.name, exc_info=True)
        self.blackboard.update_state_value("work_portfolio", portfolio)
        self.blackboard.update_state_value("recent_completed_work", recent_completed)
        self.blackboard.update_state_value("recent_abandoned_work", recent_abandoned)

        # 2) Situational context — reuse strategic_planner_prep_node's proven builders.
        try:
            self._build_situational_context()
        except Exception as e:
            logger.error("[%s] situational context build failed: %s", self.name, e)
            logger.debug("[%s] situational context exception", self.name, exc_info=True)

        logger.info("[%s] prepared: %d active work object(s)", self.name, n_active)
        self.blackboard.update_state_value("last_agent", self.name)

    def _render_recent_completed(self, summaries, store, window_hours=18):
        """Two SEPARATE logs of recent terminal work objects — they mean different things to the steward:
        DONE = finished (do not recreate, esp. recurring routine automations); ABANDONED = DROPPED, not
        finished. Each abandoned entry is annotated with when it was dropped and why (the user's last
        recorded reply + the goal node's final content) via ``_abandoned_line`` — the reason is what
        stops the evaluator from re-minting a goal the user just settled.
        Returns (done_str, abandoned_str). The WO title IS the objective (work_persist sets
        title=objective[:80]); summaries are newest-updated first."""
        from datetime import datetime, timedelta, timezone

        from app.assistant.utils.time_utils import parse_iso_utc

        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        done, abandoned = [], []
        for s in summaries:
            upd = parse_iso_utc(str(s.get("updated_at") or ""))
            if upd is not None and upd < cutoff:
                break   # newest-first → everything after is older than the window
            status = str(s.get("status") or "").lower()
            title = str(s.get("title") or "").strip()
            if not title:
                continue
            if status == "done":
                done.append(f"- {title}")
            elif status == "abandoned":
                wo = None
                try:
                    wo = store.load(s["id"])
                except Exception as e:
                    logger.debug("[%s] could not load abandoned WO %r for annotation: %s",
                                 self.name, s.get("id"), e)
                abandoned.append(_abandoned_line(s, wo))
        done_str = "\n".join(done) if done else f"(nothing completed in the last {window_hours}h)"
        abandoned_str = "\n".join(abandoned) if abandoned else ""
        return done_str, abandoned_str

    def _build_situational_context(self):
        from datetime import datetime, timedelta, timezone

        from app.assistant.dayflow_orchestrator.contracts import get_meta
        from app.assistant.dayflow_orchestrator.state_store import get_dayflow_items
        from app.assistant.utils.time_utils import parse_iso_utc

        now_utc = datetime.now(timezone.utc)
        all_items = get_dayflow_items()

        # Recent action-selector nudges — same source_type filter / window as the planner prep.
        action_cutoff = now_utc - timedelta(hours=_ACTION_LOG_HOURS)
        action_log = []
        for item in all_items:
            meta = get_meta(item)
            if str(meta.get("source_type") or "").strip().lower() != "action_log":
                continue
            created = parse_iso_utc(str(meta.get("created_at") or ""))
            if created is not None and created >= action_cutoff:
                action_log.append({
                    "time_local": str(meta.get("time_local") or "").strip(),
                    "summary": str(meta.get("summary") or "").strip(),
                    "plan_id": str(meta.get("plan_id") or "").strip(),
                    "task_id": str(meta.get("task_id") or "").strip(),
                })

        self.blackboard.update_state_value("active_tickets", _load_active_tickets())
        responded = _load_responded_tickets(now_utc - timedelta(hours=12))
        # Categorized dict stays for the architect's context builder; the
        # evaluator prompt renders the FLAT newest-first list so every reply —
        # acknowledged ones included — is visible with the user's words primary.
        from app.assistant.pipelines.dayflow.utils.context_sources import flatten_responded_tickets
        self.blackboard.update_state_value("recent_responded_tickets", responded)
        status_by_id = _work_status_by_id()
        reply_rows = flatten_responded_tickets(responded)
        _annotate_work_refs(reply_rows, status_by_id)
        self.blackboard.update_state_value("recent_ticket_replies", reply_rows)
        self.blackboard.update_state_value("recent_dispatch_results",
                                           _build_recent_dispatch_results(all_items, now_utc))
        self.blackboard.update_state_value("recent_action_selector_actions", action_log)
        self._build_expected_schedule_view(status_by_id)

    def _build_expected_schedule_view(self, status_by_id) -> None:
        """TODAY'S SCHEDULE for the evaluator with provenance resolved: an entry whose
        `source` carries a verbatim ticket id (written by the daily_context_tracker) is
        chased ticket -> work node -> work object -> status, and the resolved facts are
        rendered on the entry. Deterministic lookups only — the evaluator judges what to
        do; it is never asked whether two phrasings are the same task (the 2026-07-29
        trash re-mint: the tracker's echo of an accepted ticket read as a bare unowned
        ongoing activity and was re-minted as new work)."""
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.ticket_manager import get_ticket_manager

        resource = DI.resource_manager.get_resource(
            scope_context=self.blackboard.get_state_value("scope_context"),
            resource_id="resource_expected_calendar",
        )
        entries = (resource or {}).get("expected_schedule") or []
        ticket_manager = get_ticket_manager()
        view = []
        for entry in entries:
            row = {
                "title": str(entry.get("title") or ""),
                "start_local": str(entry.get("start_local") or ""),
                "end_local": str(entry.get("end_local") or ""),
                "status": str(entry.get("status") or ""),
                "provenance": "",
            }
            source = str(entry.get("source") or "")
            if source.startswith("ticket:"):
                row["provenance"] = _resolve_ticket_provenance(
                    source[len("ticket:"):].strip(), ticket_manager, status_by_id)
            view.append(row)
        self.blackboard.update_state_value("expected_schedule_view", view)
