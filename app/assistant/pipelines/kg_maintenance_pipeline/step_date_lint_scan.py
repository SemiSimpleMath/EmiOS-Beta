"""
Step: date_lint_scan (fragility review #5)

Dates gained routing power (temporal drain repoints edges by date, era
splits close on dates, UPCOMING markers shape agent claims, identity
sentences embed eras) - a wrong date now moves facts and asserts states
of the world. This scan finds doctrine violations and impossible dates
deterministically; each hit becomes a finding born investigated with
disposition needs_user_review (the scanner has full context - economics
doctrine), recommending the audited mutator fixes.

Checks (deterministic proposers; the human decides):
  1. impossible era - end_date strictly before start_date
  2. ongoing contradiction - end_date set while the node's own sentence
     claims it is ongoing ("ever since", "still", "remains", ...): the
     known live exhibit is a residence asserting its occupants left in
     2011 while its sentence says "been here ever since"
  3. undocumented floor - Jan-1 date with NO confidence marker (the
     year-floor doctrine requires estimated/inferred on floored dates;
     a bare Jan-1 lies about precision)
  4. unknown confidence - values outside the doctrine vocabulary
     (KNOWN set: assignable values plus system markers like auto_decay)
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from app.assistant.kg_core.kg_utils.date_compare import (
    KNOWN_DATE_CONFIDENCES,
    end_before_start,
)
from app.assistant.kg_maintenance.store import upsert_finding
from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import utc_now
from app.models.base import get_session

logger = get_logger(__name__)

MAX_FINDINGS_PER_RUN = 20
_ONGOING_MARKERS = re.compile(
    r"ever since|to this day|still (?:live|lives|living|works|working|owns|"
    r"owning|has|have|together)|remains|are here|been here|ongoing",
    re.IGNORECASE,
)


def _node_issues(node) -> List[str]:
    issues: List[str] = []

    if end_before_start(node.start_date, node.end_date):
        issues.append(
            f"impossible era: end_date {node.end_date.date()} predates "
            f"start_date {node.start_date.date()}"
        )

    if node.end_date is not None:
        text = f"{node.original_sentence or ''} {node.description or ''}"
        m = _ONGOING_MARKERS.search(text)
        if m:
            issues.append(
                f"ongoing contradiction: end_date {node.end_date.date()} is set "
                f"but the node's own text claims it is ongoing ({m.group(0)!r})"
            )

    for field, conf_field in (("start_date", "start_date_confidence"),
                              ("end_date", "end_date_confidence")):
        d = getattr(node, field)
        conf = getattr(node, conf_field)
        if d is not None and d.month == 1 and d.day == 1 and not conf:
            issues.append(
                f"undocumented floor: {field} {d.date()} is a Jan-1 date with "
                f"no confidence marker (year-floor doctrine requires "
                f"estimated/inferred on floored dates)"
            )
        if conf and conf not in KNOWN_DATE_CONFIDENCES:
            issues.append(
                f"unknown confidence: {conf_field}={conf!r} is outside the "
                f"doctrine vocabulary {KNOWN_DATE_CONFIDENCES}"
            )

    return issues


def run(ctx: PipelineContext) -> dict:
    """Returns {"scanned": int, "flagged": int, "new_findings": int}."""
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
    from app.assistant.kg.disambiguation import DISAMBIGUATION_NODE_TYPE

    session = get_session()
    try:
        nodes = (
            session.query(Node)
            .filter(Node.node_type != DISAMBIGUATION_NODE_TYPE)
            .filter((Node.start_date.isnot(None)) | (Node.end_date.isnot(None)))
            .all()
        )
        flagged: List[Dict[str, Any]] = []
        for n in nodes:
            issues = _node_issues(n)
            if issues:
                flagged.append({
                    "node_id": n.id, "label": n.label,
                    "node_type": n.node_type, "issues": issues,
                    "sentence": (n.original_sentence or "")[:200],
                })
    finally:
        session.close()

    new_findings = 0
    for f in flagged[:MAX_FINDINGS_PER_RUN]:
        fid, created = upsert_finding(
            finding_type="date_lint",
            primary_node_id=f["node_id"],
            suggested_action="review",
            reason=(
                f"Date lint on [{f['node_type']}] {f['label']!r}: "
                + "; ".join(f["issues"])
            ),
            confidence=0.9,
            priority="medium",
            agent_name="date_lint_scan",
            evidence={"label": f["label"], "issues": f["issues"],
                      "sentence": f["sentence"]},
            pipeline_run_id=ctx.run_id,
        )
        if created:
            _promote_to_review(fid, f)
            new_findings += 1

    if len(flagged) > MAX_FINDINGS_PER_RUN:
        logger.info(
            "[date_lint_scan] %d flagged, capped at %d this run - remainder "
            "next run", len(flagged), MAX_FINDINGS_PER_RUN,
        )
    summary = {"scanned": len(nodes), "flagged": len(flagged),
               "new_findings": new_findings}
    logger.info("[date_lint_scan] %s", summary)
    return summary


def _promote_to_review(finding_id: str, f: Dict[str, Any]) -> None:
    """Born investigated (economics doctrine): the scan has full context;
    the human gate decides, the executor applies via audited mutators."""
    from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
    from app.models.db_manager import get_db_manager

    recommendation = (
        f"Node `{f['node_id']}` ({f['label']!r}) has date-doctrine "
        f"violations: {'; '.join(f['issues'])}. Original sentence: "
        f"\"{f['sentence']}\". Fix via kg_update_node_field per the "
        f"year-floor doctrine (floored dates carry confidence "
        f"estimated/inferred + the user's words in the prose field); for an "
        f"ongoing contradiction decide WHICH side is wrong - clear end_date "
        f"if the era truly continues, or accept it and note the text is "
        f"stale. The user's review notes are ground truth."
    )
    with get_db_manager().transaction(op="date_lint_scan.promote") as s:
        row = s.query(KGMaintenanceFinding).filter_by(id=finding_id).first()
        if row is None:
            return
        row.status = "investigated"
        row.investigated_at = utc_now()
        row.investigation_report_json = {
            "recommendation": recommendation,
            "diagnosis": "; ".join(f["issues"]),
            "evidence": [{"kind": "node_sentence", "text": f["sentence"]}],
            "confidence": "high",
            "disposition": "needs_user_review",
        }
