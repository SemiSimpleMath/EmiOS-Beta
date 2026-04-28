from __future__ import annotations

from typing import Any, Dict, List

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.dayflow_orchestrator.dayflow_item_writer import resolve_short_id
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class TriageSpawnGuardNode(ControlNode):
    """
    Deterministic guard between intake_triage and strategic_planner.

    Rule:
    - intake_triage must emit one artifact_decision per eligible artifact.
    - Decisions are validated and converted into admitted_artifacts for planner.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        eligible_items = self.blackboard.get_state_value("eligible_items_now", [])
        if eligible_items is None:
            eligible_items = []
        if not isinstance(eligible_items, list):
            raise ValueError(
                f"[{self.name}] eligible_items_now must be a list, got {type(eligible_items).__name__}."
            )

        artifact_decisions = self.blackboard.get_state_value("artifact_decisions", [])
        if artifact_decisions is None:
            artifact_decisions = []
        if not isinstance(artifact_decisions, list):
            raise ValueError(
                f"[{self.name}] artifact_decisions must be a list, got {type(artifact_decisions).__name__}."
            )

        from app.assistant.dayflow_orchestrator.contracts import validate_artifact_decisions

        validate_artifact_decisions(artifact_decisions, f"{self.name}::artifact_decisions")

        eligible_by_id: Dict[str, Dict[str, Any]] = {}
        for item in eligible_items:
            if not isinstance(item, dict):
                continue
            meta = get_meta(item)
            item_id = str(meta.get("item_id") or "").strip()
            if item_id:
                eligible_by_id[item_id] = item

        auto_admitted = self.blackboard.get_state_value("auto_admitted_artifacts", []) or []
        if not isinstance(auto_admitted, list):
            auto_admitted = []

        if not eligible_by_id and not artifact_decisions:
            # Still merge auto-admitted items even when there's nothing to triage.
            self.blackboard.update_state_value("admitted_artifacts", auto_admitted)
            self.blackboard.update_state_value("admitted_artifacts_count", len(auto_admitted))
            logger.info("[%s] no eligible items and no decisions — pass-through (auto_admitted=%d).", self.name, len(auto_admitted))
            self.blackboard.update_state_value("last_agent", self.name)
            return

        if len(artifact_decisions) != len(eligible_by_id):
            logger.warning(
                "[%s] decision count mismatch: eligible=%d decisions=%d. "
                "Processing available decisions.",
                self.name,
                len(eligible_by_id),
                len(artifact_decisions),
            )

        admitted_artifacts: List[Dict[str, Any]] = []
        rejected_artifacts: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        for dec in artifact_decisions:
            raw_artifact_id = str(dec.get("artifact_id") or "").strip()
            if not raw_artifact_id:
                logger.warning("[%s] artifact_decision missing artifact_id, skipping.", self.name)
                continue
            # Resolve against eligible items first (new items not yet in DB),
            # then fall back to DB lookup for items from prior ticks.
            artifact_id = raw_artifact_id
            if raw_artifact_id not in eligible_by_id:
                # Try short_id → item_id from eligible items
                for eid, eitem in eligible_by_id.items():
                    emeta = eitem.get("metadata") or {}
                    if str(emeta.get("short_id", "")) == raw_artifact_id:
                        artifact_id = eid
                        break
                else:
                    artifact_id = resolve_short_id(raw_artifact_id)
            if artifact_id in seen_ids:
                logger.warning("[%s] duplicate artifact_decision for '%s', skipping.", self.name, artifact_id)
                continue
            seen_ids.add(artifact_id)
            if artifact_id not in eligible_by_id:
                logger.warning(
                    "[%s] artifact_decision artifact_id '%s' (raw='%s') does not match any eligible item_id, skipping.",
                    self.name,
                    artifact_id,
                    raw_artifact_id,
                )
                continue
            decision = str(dec.get("decision") or "").strip().upper()
            if decision in ("ADMIT", "ADMIT_ARTIFACT"):
                meta = eligible_by_id[artifact_id].get("metadata")
                if isinstance(meta, dict):
                    meta["state"] = "artifact"
                    meta["state_reason"] = "triage_admit"
                admitted_artifacts.append(eligible_by_id[artifact_id])
            elif decision.startswith("REJECT"):
                # Mutate in-memory; triage_persist_node persists the suppression
                # so the item exits the eligible bucket and triage doesn't
                # re-evaluate it next tick.
                meta = eligible_by_id[artifact_id].get("metadata")
                if isinstance(meta, dict):
                    meta["state"] = "suppressed"
                    meta["state_reason"] = f"triage_{decision.lower()}"
                rejected_artifacts.append(eligible_by_id[artifact_id])

        # auto_admitted already loaded above (before early-return check).
        all_admitted = auto_admitted + admitted_artifacts

        self.blackboard.update_state_value("admitted_artifacts", all_admitted)
        self.blackboard.update_state_value("admitted_artifacts_count", len(all_admitted))
        self.blackboard.update_state_value("rejected_artifacts", rejected_artifacts)
        self.blackboard.update_state_value("rejected_artifacts_count", len(rejected_artifacts))
        logger.info(
            "[%s] admission: eligible=%d triage_admitted=%d triage_rejected=%d auto_admitted=%d total=%d",
            self.name,
            len(eligible_by_id),
            len(admitted_artifacts),
            len(rejected_artifacts),
            len(auto_admitted),
            len(all_admitted),
        )

        self.blackboard.update_state_value("last_agent", self.name)
