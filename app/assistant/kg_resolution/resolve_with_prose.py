"""Synchronous helper that runs `kg_resolution_manager` against a
cluster lead + user prose, then returns a structured report.

Called from the maintenance UI's `/api/finding/<id>/resolve_with_prose`
route. The manager has full read+write KG authority, can call
regeneration tools, and resolves cluster findings via kg_finding_resolve.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.scope.loader import load_scope_for_source
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message, ScopeContext
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)

MANAGER_NAME = "kg_resolution_manager"


def _resolution_scope() -> ScopeContext:
    """Authority for kg_resolution_manager. Writes to the KG (mutators)
    AND to entity_cards / wiki vault (regen tools). Owner authority is
    high because the manager carries out user-confirmed instructions."""
    return load_scope_for_source(
        kind="subsystem",
        source_id="kg_resolution",
        actor_id="kg_resolution_manager",
        identity_overrides={
            "owner_id": "jukka",
            "actor_id": "kg_resolution_manager",
            "scope_id": "scope::kg_resolution::resolve_with_prose",
        },
    )


def _build_brief(*, lead_finding_id: str, prose: str) -> Optional[Dict[str, Any]]:
    """Pull the lead + cluster context and shape it as task + information
    for the manager. Returns None when the lead doesn't exist or isn't a
    cluster lead."""
    from app.assistant.kg_maintenance.store import get_finding
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node

    lead = get_finding(lead_finding_id)
    if lead is None:
        return None

    primary_node_id = lead.get("primary_node_id") or ""
    # get_finding returns the raw row — primary_label / primary_node_type
    # are populated only by get_findings (via _enrich_node_labels). Look
    # them up directly here so the brief carries human-readable context.
    primary_label = ""
    primary_node_type = ""
    if primary_node_id:
        with get_db_manager().read_session() as session:
            row = (
                session.query(Node.label, Node.node_type)
                .filter(Node.id == primary_node_id)
                .first()
            )
            if row:
                primary_label = row[0] or ""
                primary_node_type = row[1] or ""

    ev = lead.get("evidence_json") or {}
    cluster = ev.get("cluster") if isinstance(ev, dict) else {}
    if not isinstance(cluster, dict):
        cluster = {}
    root_question = (cluster.get("root_question") or "").strip()
    sibling_ids: List[str] = list(cluster.get("sibling_ids") or [])

    # Pull each finding in the cluster for the manager's information block.
    cluster_finding_ids = [lead_finding_id] + sibling_ids
    with get_db_manager().read_session() as session:
        rows = (
            session.query(
                KGMaintenanceFinding.id,
                KGMaintenanceFinding.finding_type,
                KGMaintenanceFinding.reason,
                KGMaintenanceFinding.confidence,
                KGMaintenanceFinding.evidence_json,
            )
            .filter(KGMaintenanceFinding.id.in_(cluster_finding_ids))
            .all()
        )
        finding_rows = [
            {
                "finding_id": str(r[0]),
                "finding_type": r[1],
                "reason": r[2] or "",
                "confidence": r[3],
            }
            for r in rows
        ]

    # Manager input: a clear task + structured information block.
    task = (
        f"Resolve the cluster of {len(cluster_finding_ids)} kg_maintenance_findings "
        f"on entity {primary_label!r} using the user's prose answer below. "
        f"Investigate the underlying KG state, make the smallest correct mutations, "
        f"regenerate the entity card and wiki page, and resolve every finding in "
        f"the cluster with kg_finding_resolve."
    )

    info_lines: List[str] = []
    info_lines.append("## User prose answer")
    info_lines.append(f"> {prose}")
    info_lines.append("")
    info_lines.append("## Cluster context")
    info_lines.append(f"- primary_node_id: `{primary_node_id}`")
    info_lines.append(f"- primary_label: {primary_label!r}")
    if primary_node_type:
        info_lines.append(f"- primary_node_type: {primary_node_type}")
    if root_question:
        info_lines.append(f"- root_question: {root_question}")
    info_lines.append(f"- cluster_size: {len(cluster_finding_ids)}")
    info_lines.append("")
    info_lines.append("## Findings in this cluster (resolve every one)")
    for f in finding_rows:
        info_lines.append(
            f"- `{f['finding_id']}` ({f['finding_type']}, conf={f['confidence']}): "
            f"{(f['reason'] or '')[:300]}"
        )

    return {
        "task": task,
        "information": "\n".join(info_lines),
        "primary_node_id": primary_node_id,
        "cluster_finding_ids": cluster_finding_ids,
    }


def _extract_report_from_audit(blackboard) -> Optional[Dict[str, Any]]:
    """Pull the resolution report from kg_resolution::final_answer."""
    try:
        msgs = blackboard.get_messages()
    except Exception as e:
        logger.warning("could not read blackboard messages: %s", e)
        return None
    for m in msgs:
        sender = str(getattr(m, "sender", "") or "")
        if sender.endswith("final_answer") and "kg_resolution" in sender:
            data = getattr(m, "data", None) or {}
            keep = (
                "summary", "user_prose", "mutations", "regenerations",
                "findings_resolved", "open_questions", "completed",
                "final_answer_answer", "result_summary",
            )
            return {k: data.get(k) for k in keep if data.get(k) is not None} or None
    return None


def resolve_cluster_with_prose(*, lead_finding_id: str, prose: str) -> Dict[str, Any]:
    """Invoke kg_resolution_manager synchronously. Returns
    {status, lead_id, report?, error?}.

    status:
      - 'completed' — manager produced a structured report with completed=True
      - 'partial'   — manager finished but report.completed=False (escalation
                       or open questions). Still a clean outcome; the UI
                       surfaces the report.
      - 'no_report' — the manager exited without a final-answer message
      - 'error'     — invocation crashed (manager broken, scope rejected, etc.)
    """
    brief = _build_brief(lead_finding_id=lead_finding_id, prose=prose)
    if brief is None:
        return {"status": "error", "error": "lead finding not found"}

    mgr = DI.multi_agent_manager_factory.create_manager(MANAGER_NAME)
    if mgr is None:
        return {"status": "error", "error": f"manager {MANAGER_NAME} not registered"}

    msg = Message(
        task=brief["task"],
        information=brief["information"],
        scope_context=_resolution_scope(),
    )

    try:
        DI.manager_invoker.invoke(mgr, msg)
    except Exception as e:
        logger.error("[resolve_with_prose] manager invocation failed: %s", e, exc_info=True)
        logger.debug("manager exception details", exc_info=True)
        return {
            "status": "error",
            "lead_id": lead_finding_id,
            "error": str(e),
        }

    report = _extract_report_from_audit(mgr.blackboard)
    if report is None:
        return {"status": "no_report", "lead_id": lead_finding_id}

    return {
        "status": "completed" if report.get("completed") else "partial",
        "lead_id": lead_finding_id,
        "report": report,
    }
