"""
Step: role_succession_scan (diachronic disambiguation)

Role-reference entities ("X's School", "X's Car") name a role whose
concrete referent changes over time; their facts silently blend eras.
This scan finds the candidates deterministically (possessive-label
Entities with an open era and enough evidence), and the succession_judge
agent decides per node whether the evidence describes one stable
referent or a referent change (LLM decides — old evidence alone is not
a succession signal).

A 'succession' verdict becomes a referent_succession finding, born
investigated with disposition='needs_user_review' (era splits are
impactful — P4.4 conservatism): it lands on /kg-maintenance/needs-input,
and on Accept the executor performs the split via kg_split_succession.
'stable'/'unclear' verdicts write a rejected finding as a cooldown stamp
so the node is re-judged only after COOLDOWN_DAYS (more evidence by then).

Splits are lazy: nothing here mutates the graph.
"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Dict, List

from app.assistant.kg_maintenance.store import upsert_finding
from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import utc_now
from app.models.base import get_session

logger = get_logger(__name__)

POSSESSIVE_RE = re.compile(r"^.+['’]s\s+.+$")
MAX_JUDGES_PER_RUN = 8
COOLDOWN_DAYS = 30
MIN_EVIDENCE = 3
CONFIDENCE_MAP = {"high": 0.9, "medium": 0.7, "low": 0.4}


def _run_judge(agent_input: Dict[str, Any]) -> Dict[str, Any] | None:
    from app.assistant.routine_handlers.subconscious import _run_subconscious_agent

    return _run_subconscious_agent(
        handler_label="succession_judge",
        agent_name="kg_maintenance::succession_judge",
        context=agent_input,
        scope_id="kg_maintenance::succession_judge",
        actor_id="role_succession_scan",
    )


def run(ctx: PipelineContext) -> dict:
    """Returns {"candidates": int, "judged": int, "succession_findings": int,
    "stable": int}."""
    from app.assistant.database.kg_chat_projection import KGNodeEvidence
    from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
    from app.assistant.kg.disambiguation import find_disambiguation
    from app.assistant.kg_core.kg_utils.date_display import format_node_date

    cutoff = utc_now() - timedelta(days=COOLDOWN_DAYS)

    # ── Read phase: deterministic candidate gathering ────────────────────
    session = get_session()
    try:
        recently_judged = {
            r[0] for r in session.query(KGMaintenanceFinding.primary_node_id)
            .filter(KGMaintenanceFinding.finding_type == "referent_succession")
            .filter(KGMaintenanceFinding.created_at >= cutoff)
            .all()
        }

        entities = (
            session.query(Node)
            .filter(Node.node_type == "Entity")
            .filter(Node.end_date.is_(None))
            .all()
        )
        candidates = []
        for n in entities:
            if not POSSESSIVE_RE.match(n.label or ""):
                continue
            if n.id in recently_judged:
                continue
            if find_disambiguation(session, n.label) is not None:
                continue  # already split — the park point handles new mentions
            ev_rows = (
                session.query(KGNodeEvidence)
                .filter(KGNodeEvidence.node_id == n.id)
                .order_by(KGNodeEvidence.message_timestamp.asc().nulls_last())
                .all()
            )
            if len(ev_rows) < MIN_EVIDENCE:
                continue
            candidates.append((n, ev_rows))

        # Most-evidenced first — era blends hurt most where facts accumulate.
        candidates.sort(key=lambda c: -len(c[1]))
        candidates = candidates[:MAX_JUDGES_PER_RUN]

        # Snapshot everything the judge needs while the session is open.
        judge_inputs: List[Dict[str, Any]] = []
        for n, ev_rows in candidates:
            evidence = [
                {
                    "date": (
                        (ev.message_timestamp or ev.created_at).date().isoformat()
                        if (ev.message_timestamp or ev.created_at) else None
                    ),
                    "sentence": (ev.derived_sentence or ev.source_text or "")[:300],
                }
                for ev in ev_rows[:30]
            ]
            edge_lines = []
            for e, other in (
                session.query(Edge, Node)
                .outerjoin(Node, Edge.target_id == Node.id)
                .filter(Edge.source_id == n.id).limit(15).all()
            ):
                if other is not None:
                    dates = format_node_date(other.start_date, other.start_date_confidence)
                    edge_lines.append(
                        f"{n.label} --{e.relationship_type}--> {other.label}"
                        + (f" (since {dates})" if dates else "")
                    )
            for e, other in (
                session.query(Edge, Node)
                .outerjoin(Node, Edge.source_id == Node.id)
                .filter(Edge.target_id == n.id).limit(15).all()
            ):
                if other is not None:
                    edge_lines.append(f"{other.label} --{e.relationship_type}--> {n.label}")
            judge_inputs.append({
                "node_id": n.id,
                "label": n.label,
                "category": n.category,
                "start_date": format_node_date(n.start_date, n.start_date_confidence),
                "end_date": None,
                "evidence": evidence,
                "edges": edge_lines,
                "today": utc_now().date().isoformat(),
            })
    finally:
        session.close()

    # ── Judge phase: LLM decides, no session held ────────────────────────
    judged = 0
    succession_findings = 0
    stable = 0
    for ji in judge_inputs:
        node_id = ji.pop("node_id")
        label = ji["label"]
        verdict = _run_judge(ji)
        if not isinstance(verdict, dict):
            continue
        judged += 1
        v = str(verdict.get("verdict") or "").strip().lower()
        conf = str(verdict.get("confidence") or "low").strip().lower()
        split_date = str(verdict.get("split_date") or "").strip()

        if v == "succession" and conf in ("high", "medium") and split_date:
            fid, created = upsert_finding(
                finding_type="referent_succession",
                primary_node_id=node_id,
                suggested_action="split_succession",
                reason=(
                    f"Referent change detected for {label!r}: "
                    f"{str(verdict.get('split_basis') or verdict.get('reasoning') or '')[:400]}"
                ),
                confidence=CONFIDENCE_MAP.get(conf, 0.5),
                priority="high" if conf == "high" else "medium",
                agent_name="role_succession_scan",
                evidence={
                    "label": label,
                    "split_date": split_date,
                    "split_basis": verdict.get("split_basis"),
                    "era_before": verdict.get("era_before"),
                    "era_after": verdict.get("era_after"),
                    "judge_confidence": conf,
                    "evidence_count": len(ji["evidence"]),
                },
                pipeline_run_id=ctx.run_id,
            )
            if created:
                _promote_to_review(fid, label=label, verdict=verdict, node_id=node_id)
                succession_findings += 1
        else:
            # Cooldown stamp: re-judged after COOLDOWN_DAYS with more evidence.
            upsert_finding(
                finding_type="referent_succession",
                primary_node_id=node_id,
                suggested_action="none",
                reason=f"succession_judge: {v or 'no verdict'} ({conf}) — "
                       f"{str(verdict.get('reasoning') or '')[:300]}",
                confidence=CONFIDENCE_MAP.get(conf, 0.5),
                priority="low",
                agent_name="role_succession_scan",
                evidence={"label": label, "verdict": v},
                pipeline_run_id=ctx.run_id,
                initial_status="rejected",
            )
            stable += 1

    summary = {
        "candidates": len(judge_inputs),
        "judged": judged,
        "succession_findings": succession_findings,
        "stable": stable,
    }
    logger.info("[role_succession_scan] %s", summary)
    return summary


def _promote_to_review(finding_id: str, *, label: str, verdict: Dict[str, Any], node_id: str) -> None:
    """Born-investigated with needs_user_review: the judge already did the
    investigation, and era splits are impactful enough to gate on Accept."""
    from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
    from app.models.db_manager import get_db_manager

    split_date = str(verdict.get("split_date") or "").strip()
    recommendation = (
        f"REFERENT SUCCESSION on node `{node_id}` ({label!r}): the evidence shows the "
        f"real-world referent changed around {split_date}. "
        f"Old era: {str(verdict.get('era_before') or '?')[:200]} "
        f"New era: {str(verdict.get('era_after') or '?')[:200]}\n"
        f"Apply with kg_split_succession(node_id, split_date={split_date!r}, "
        f"date_confidence='estimated' unless the user stated a precise date). "
        f"Before applying, read the node's edges with kg_query: edges whose facts "
        f"belong to the NEW era go in repoint_edge_ids; era-ambiguous edges stay put "
        f"(the disambiguation scan re-points parked mentions by date later). "
        f"If the user's review notes correct the split date or reject the split, "
        f"the notes are ground truth."
    )
    with get_db_manager().transaction(op="role_succession_scan.promote") as s:
        f = s.query(KGMaintenanceFinding).filter_by(id=finding_id).first()
        if f is None:
            return
        f.status = "investigated"
        f.investigated_at = utc_now()
        f.investigation_report_json = {
            "recommendation": recommendation,
            "diagnosis": str(verdict.get("reasoning") or "")[:500],
            "evidence": [{"kind": "judge_basis", "text": str(verdict.get("split_basis") or "")[:400]}],
            "confidence": str(verdict.get("confidence") or "medium"),
            "disposition": "needs_user_review",
        }
