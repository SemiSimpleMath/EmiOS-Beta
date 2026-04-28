"""
Gate node that decides whether the relevance cleaner should run this tick.

Runs the cleaner agent if 30+ minutes have elapsed since the last run.
Otherwise skips directly to the next node in the pipeline.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_CLEANER_INTERVAL_MINUTES = 30
_LAST_RUN_KEY = "relevance_cleaner_last_run_utc"
_CLEANER_PREP_NODE = "relevance_cleaner_prep_node"


class RelevanceCleanerGateNode(ControlNode):
    """
    Checks whether the relevance cleaner should run on this tick.

    If enough time has passed since the last run, routes to the cleaner
    agent. Otherwise skips to the configured skip_to node.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        now_utc = datetime.now(timezone.utc)

        last_run_raw = str(self.blackboard.get_state_value(_LAST_RUN_KEY, "") or "").strip()
        should_run = True

        if last_run_raw:
            try:
                last_run = datetime.fromisoformat(last_run_raw.replace("Z", "+00:00"))
                if last_run.tzinfo is None:
                    last_run = last_run.replace(tzinfo=timezone.utc)
                elapsed = now_utc - last_run
                should_run = elapsed >= timedelta(minutes=_CLEANER_INTERVAL_MINUTES)
            except (ValueError, TypeError):
                should_run = True

        if should_run:
            logger.info(
                "[%s] Relevance cleaner eligible — routing to %s.",
                self.name, _CLEANER_PREP_NODE,
            )
            self.blackboard.update_state_value("next_agent", _CLEANER_PREP_NODE)
        else:
            logger.debug(
                "[%s] Relevance cleaner skipped — last run %s, interval %dm.",
                self.name, last_run_raw, _CLEANER_INTERVAL_MINUTES,
            )

        self.blackboard.update_state_value("last_agent", self.name)
