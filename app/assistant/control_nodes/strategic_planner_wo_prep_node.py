"""Pre-LLM prep node for strategic_planner_wo (the work-object steward).

Builds the steward's context: the ACTIVE work objects rendered as `work_portfolio` (its primary input),
plus the situational context that lets it judge timing and act on user decisions — active tickets,
recent ticket responses, recent dispatch results, recent action-selector nudges. The situational
builders live in this module (relocated when the legacy strategic_planner_prep_node was retired — this
prep is now their sole user). Resource-backed context (schedule/presence/routine/etc.) is
auto-resolved by the context injector and needs no building here.

Retiring the steward (configs/brain_lanes.json steward.mode: retired; owner's principle 2026-10-05: all input flows
to the brain, the brain decides, consumers carry it out): this node keeps the work others read every tick (pending
concern and belief feedback delivered, the portfolio, recent work and reminder history for the architect), skips the
steward's own context, hands the architect an empty steward result, and routes straight to the list-item intake node.
Intake still admitted then has no reviewer: it is logged as an ERROR every tick until it is resolved.

Inert until the dayflow manager's state_map routes to it.
"""
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.dayflow_orchestrator.work_context import render_view
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

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


def _work_status_by_id() -> dict:
    """WorkObject id -> status, one summaries fetch. The id-chain resolvers below join
    against this instead of asking the evaluator to reconstruct links from wording."""
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return {s["id"]: str(s.get("status") or "unknown")
            for s in get_dayflow_work_store().list_work_objects()}


def _resolve_ticket_provenance(ticket_id: str, ticket_manager, status_by_id) -> str:
    """Chase a schedule entry's verbatim ticket id to the originating work object:
    ticket -> trigger_context.work_node -> work object -> current status, plus the
    user's recorded response. Pure lookups on edges recorded at write time."""
    ticket = ticket_manager.get_ticket_by_id(ticket_id) if ticket_id else None
    trigger = getattr(ticket, "trigger_context", None) or {}
    ref = str(trigger.get("work_node") or "") if isinstance(trigger, dict) else ""
    work_id = ref.split("::", 1)[0] if "::" in ref else ""
    return render_view("ticket_provenance", ticket_id=ticket_id, found=ticket is not None,
                       work_id=work_id, status=status_by_id.get(work_id, "unresolved"),
                       action=str(getattr(ticket, "user_action", "") or "").strip())


def _goal_epitaph(wo) -> str:
    """The WO-level ending reason: the goal node's payload.terminal.reason, where
    set_work_status records WHY the object ended (steward drop reasons, finalizer
    resolve reasoning, hand-written DO-NOT-RECREATE epitaphs)."""
    nodes = getattr(wo, "nodes", {}) or {}
    goal = nodes.get(getattr(wo, "goal_node_id", "") or "")
    term = (getattr(goal, "payload", {}) or {}).get("terminal") if goal is not None else None
    return str(term.get("reason") or "").strip().replace("\n", " ") if isinstance(term, dict) else ""


def _ended_item(wo, *, finalizers: bool) -> dict:
    """One recently ended work object: its FULL objective (the goal node's content; the stored title is
    the objective cut at 80 characters by work_persist), when it ended in local time, and why.

    The end time is the goal's `terminal.at`, which every ending writes (set_work_status, the rollup, a
    terminal set_status); an ended goal without one raises."""
    from app.assistant.utils.time_utils import local_time_text

    goal = wo.nodes.get(wo.goal_node_id)
    terminal = (goal.payload.get("terminal") if goal is not None else None) or {}
    if not terminal.get("at"):
        raise ValueError(f"ended work object {wo.id} records no end time on its goal")
    return {"id": wo.id, "title": (goal.content or goal.title).strip(), "when": local_time_text(terminal["at"]),
            "reason": _goal_epitaph(wo),
            "finalizers": [task.payload["finalizer"] for task in wo.nodes.values()
                           if wo.is_work_unit(task) and task.payload.get("finalizer")] if finalizers else []}


def _abandoned_line(wo) -> str:
    """One RECENTLY DROPPED entry: objective + when + WHY.

    The reason is the load-bearing part — the goal's ending reason and each task's
    finalizer account, which carries the user's last reply. Without it the evaluator
    re-mints a goal the user just settled (the 2026-07-27 July-timesheets double
    reminder: the WO was dropped on "those are done first of next month always",
    the DROPPED section showed only the bare title, and an identical WO was created
    11 minutes later)."""
    return render_view("recent_work", items=[_ended_item(wo, finalizers=True)], dropped=True,
                       window_hours=18).strip()


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


def recent_ended_items(summaries, store, window_hours=18):
    """Two SEPARATE logs of recent terminal work objects — they mean different things to the steward:
    DONE = finished (do not recreate, esp. recurring routine automations); ABANDONED = DROPPED, not
    finished. Each abandoned entry is annotated with when it was dropped and why (the user's last
    recorded reply + the goal node's final content) via ``_abandoned_line`` — the reason is what
    stops the evaluator from re-minting a goal the user just settled.
    Every entry shows the full objective and when it ended, in local time (``_ended_item``). The
    stored WO title is the objective cut at 80 characters (work_persist), so it appears only for a
    record that cannot be read, which is logged as an ERROR and marked unreadable in the log.
    Returns (done items, abandoned items) for render_recent_ended; summaries are newest-updated first."""
    from datetime import datetime, timedelta, timezone

    from app.assistant.utils.time_utils import parse_iso_utc

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    done, abandoned = [], []
    for s in summaries:
        upd = parse_iso_utc(str(s.get("updated_at") or ""))
        if upd is not None and upd < cutoff:
            break   # newest-first → everything after is older than the window
        status = str(s.get("status") or "").lower()
        if status not in ("done", "abandoned"):
            continue
        try:
            item = _ended_item(store.load(s["id"]), finalizers=status == "abandoned")
        except Exception as e:
            logger.error("[recent work] ended work object %s could not be read for the recent-work log: %s",
                         s.get("id"), e)
            item = {"id": str(s.get("id")), "title": str(s.get("title") or "").strip(), "unreadable": str(e)}
        (done if status == "done" else abandoned).append(item)
    return done, abandoned


def render_recent_ended(done, abandoned, window_hours=18, labels=None):
    """(done_str, abandoned_str); `labels` (id -> label of the call) shows each work object's label."""
    return (render_view("recent_work", items=done, dropped=False, window_hours=window_hours,
                        labels=labels or {}).strip(),
            render_view("recent_work", items=abandoned, dropped=True, window_hours=window_hours,
                        labels=labels or {}).strip())


class StrategicPlannerWoPrepNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        from app.assistant.dayflow_orchestrator.state_store import load_admitted_intake
        from app.assistant.dayflow_orchestrator.contracts import get_meta
        from copy import deepcopy
        # Stored eligibility is authoritative: stale tick memory cannot revive reviewed items.
        snapshots = {}
        inbox = {}
        for item in load_admitted_intake():
            item_id = str(get_meta(item).get("item_id") or item.get("id") or "")
            if item_id:
                snapshots[item_id] = deepcopy(get_meta(item))
                inbox[item_id] = item
        self.blackboard.update_state_value("intake_review_snapshots", snapshots)
        self.blackboard.update_state_value("intake_reviews", [])
        self.blackboard.update_state_value("admitted_artifacts", list(inbox.values()))
        self.blackboard.update_state_value("admitted_artifacts_count", len(inbox))

        # 1) The portfolio (active work objects) + the DONE-LOG (recently completed/abandoned) — the
        #    evaluator's primary inputs. The done-log is its memory so it does NOT recreate work it just
        #    finished (especially recurring routine automations that re-appear in the routine each tick).
        # The portfolio is the ONLY thing between the evaluator and recreating work that is
        # already in flight, so it must never LIE about being empty. "(no active work
        # objects)" is true of an empty store and false of a broken one, and the two read
        # identically to the planner — which then does exactly what its prompt tells it to
        # do with an empty portfolio: create the work. This swallow cost four hours on
        # 2026-08-30: three rows with NULL timestamps failed WorkObject validation, the
        # list comprehension raised, the except left the default in place, and the planner
        # minted 34 duplicate work objects and 28 notifications before anyone noticed.
        #
        # So the two failures are now told apart. A store that cannot be listed at all
        # RAISES (fail loud — a tick planned blind is worse than a tick not taken).
        # Individual objects that will not load are SKIPPED, logged, and NAMED at the top
        # of the rendered portfolio, so the planner is told its view is partial rather than
        # told the store is empty.
        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        from app.assistant.dayflow_orchestrator.work_portfolio import render_active_portfolio

        store = get_dayflow_work_store()
        from app.assistant.subconscious.concern_feedback import recover_pending_concern_feedback
        recover_pending_concern_feedback(store)
        from belief_engine.work_feedback import recover_pending_belief_feedback
        recover_pending_belief_feedback(store)
        from app.assistant.dayflow_orchestrator.work_intake import reconcile_transferred_intake
        remaining = reconcile_transferred_intake(store, list(inbox.values()))
        self.blackboard.update_state_value("admitted_artifacts", remaining)
        self.blackboard.update_state_value("admitted_artifacts_count", len(remaining))
        summaries = store.list_work_objects()   # newest-updated first; raises if unreadable

        portfolio, n_active, unreadable = render_active_portfolio(store, summaries)
        recent_completed, recent_abandoned = self._render_recent_completed(summaries, store)
        self.blackboard.update_state_value("work_portfolio", portfolio)
        self.blackboard.update_state_value("recent_completed_work", recent_completed)
        self.blackboard.update_state_value("recent_abandoned_work", recent_abandoned)

        from datetime import datetime, timezone
        from app.assistant.dayflow_orchestrator.communication_context import scheduled_reminder_history
        self.blackboard.update_state_value("scheduled_reminder_history",
                                           scheduled_reminder_history(datetime.now(timezone.utc)))

        from app.assistant.dayflow_orchestrator.brain_list_intake import steward_retired
        if steward_retired():
            if remaining:
                ids = [str(get_meta(item).get("item_id") or item.get("id") or "") for item in remaining]
                logger.error("[%s] the steward is retired and %d admitted intake item(s) have no reviewer: %s",
                             self.name, len(ids), ids)
            self.blackboard.update_state_value("steward_persist_result", {"created": [], "changed": [],
                                                                          "end_requested": []})
            self.blackboard.update_state_value("replan_work_ids", [])
            self.blackboard.update_state_value("user_directed_replan_ids", [])
            logger.info("[%s] steward retired: %d active work object(s); on to the list-item intake",
                        self.name, n_active)
            self.blackboard.update_state_value("last_agent", self.name)
            self.blackboard.update_state_value("next_agent", "brain_list_intake_node")
            return

        # 2) Situational context — reuse strategic_planner_prep_node's proven builders.
        try:
            self._build_situational_context()
        except Exception as e:
            logger.error("[%s] situational context build failed: %s", self.name, e)
            logger.debug("[%s] situational context exception", self.name, exc_info=True)

        if unreadable:
            logger.error("[%s] prepared with an INCOMPLETE portfolio: %d active work "
                         "object(s) rendered, %d unreadable (%s)",
                         self.name, n_active, len(unreadable), ", ".join(unreadable))
        else:
            logger.info("[%s] prepared: %d active work object(s)", self.name, n_active)
        self.blackboard.update_state_value("last_agent", self.name)

    def _render_recent_completed(self, summaries, store, window_hours=18):
        """The steward's two logs of recent terminal work objects (recent_ended_items, render_recent_ended)."""
        done, abandoned = recent_ended_items(summaries, store, window_hours)
        return render_recent_ended(done, abandoned, window_hours)

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
        # No ticket replies here. A ticket is a work node's question and the reply is that node's
        # result: the finalizer judges it, the owning work object's architect acts on it, and the
        # steward reads it in that work object's record. A 12-hour list of replies labelled "user
        # directives" made the steward re-create finished work every pass (2026-10-02: one handled
        # "please send it" became five more Seija email work objects).
        status_by_id = _work_status_by_id()
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
