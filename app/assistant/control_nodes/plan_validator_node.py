from __future__ import annotations

from typing import Any, Dict, List

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.dayflow_orchestrator.state_store import get_dayflow_items
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class PlanValidatorNode(ControlNode):
    """
    Cross-plan hygiene pass.

    Scans active plan steps for obvious issues and can request planner rebuild
    when strong duplicate evidence is detected.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        existing = get_dayflow_items()

        plan_rows: List[Dict[str, Any]] = []
        for item in existing:
            if not isinstance(item, dict):
                continue
            meta = get_meta(item)
            if str(meta.get("source_type") or "").strip().lower() != "plan":
                continue
            state = str(meta.get("state") or "").strip().lower()
            if state in {"closed", "suppressed"}:
                continue
            plan_id = str(meta.get("plan_id") or "").strip()
            if not plan_id:
                continue
            summary = str(meta.get("summary") or "").strip()
            if not summary:
                continue
            normalized_summary = " ".join(summary.lower().split())
            plan_rows.append(
                {
                    "item_id": str(meta.get("item_id") or item.get("id") or "").strip(),
                    "plan_id": plan_id,
                    "state": state,
                    "summary": summary,
                    "normalized_summary": normalized_summary,
                }
            )

        by_summary: Dict[str, List[Dict[str, Any]]] = {}
        for row in plan_rows:
            by_summary.setdefault(row["normalized_summary"], []).append(row)

        findings: List[Dict[str, Any]] = []
        for key, rows in by_summary.items():
            if len(rows) < 2:
                continue
            plan_ids = sorted({r["plan_id"] for r in rows})
            if len(plan_ids) < 2:
                continue
            findings.append(
                {
                    "kind": "duplicate_step_summary_across_plans",
                    "summary": rows[0]["summary"],
                    "plan_ids": plan_ids,
                    "item_ids": [r["item_id"] for r in rows],
                }
            )

        self.blackboard.update_state_value("plan_validator_findings", findings)
        self.blackboard.update_state_value("plan_validator_findings_count", len(findings))

        if findings:
            self.blackboard.update_state_value("force_plan_rebuild_tf", True)
            logger.info(
                "[%s] found %d cross-plan issue(s); forcing planner rebuild.",
                self.name,
                len(findings),
            )
        else:
            self.blackboard.update_state_value("force_plan_rebuild_tf", False)

        self.blackboard.update_state_value("last_agent", self.name)
