"""Pre-LLM prep node for strategic_planner_wo (the work-object steward).

Builds the steward's context: the ACTIVE work objects rendered as `work_portfolio` (its primary input),
plus the situational context that lets it judge timing and act on user decisions — active tickets,
recent ticket responses, recent dispatch results, recent action-selector nudges. The situational
builders are REUSED from strategic_planner_prep_node (the old planner's proven prep) so the steward has
the same awareness the planner had. Resource-backed context (schedule/presence/routine/etc.) is
auto-resolved by the context injector and needs no building here.

Inert until the dayflow manager's state_map routes to it.
"""
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_TERMINAL_WO_STATES = {"done", "abandoned"}


class StrategicPlannerWoPrepNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        # 1) The portfolio — the steward's primary input.
        portfolio = "(no active work objects)"
        n_active = 0
        try:
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            from app.assistant.dayflow_orchestrator.work_portfolio import render_portfolio
            store = get_dayflow_work_store()
            active = [store.load(s["id"]) for s in store.list_work_objects()
                      if str(s.get("status") or "").lower() not in _TERMINAL_WO_STATES]
            n_active = len(active)
            portfolio = render_portfolio(active)
        except Exception as e:
            logger.error("[%s] portfolio build failed: %s", self.name, e)
            logger.debug("[%s] portfolio build exception", self.name, exc_info=True)
        self.blackboard.update_state_value("work_portfolio", portfolio)

        # 2) Situational context — reuse strategic_planner_prep_node's proven builders.
        try:
            self._build_situational_context()
        except Exception as e:
            logger.error("[%s] situational context build failed: %s", self.name, e)
            logger.debug("[%s] situational context exception", self.name, exc_info=True)

        logger.info("[%s] prepared: %d active work object(s)", self.name, n_active)
        self.blackboard.update_state_value("last_agent", self.name)

    def _build_situational_context(self):
        from datetime import datetime, timedelta, timezone

        from app.assistant.control_nodes.strategic_planner_prep_node import (
            _ACTION_LOG_HOURS, _build_recent_dispatch_results, _load_active_tickets,
            _load_responded_tickets,
        )
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
        self.blackboard.update_state_value("recent_responded_tickets",
                                           _load_responded_tickets(now_utc - timedelta(hours=12)))
        self.blackboard.update_state_value("recent_dispatch_results",
                                           _build_recent_dispatch_results(all_items, now_utc))
        self.blackboard.update_state_value("recent_action_selector_actions", action_log)
