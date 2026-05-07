"""
Step: cluster_resolve

Distills the pending kg_maintenance_finding queue so the user sees the
ROOT QUESTIONS, not every redundant finding pointing at the same fact.

Two-stage: candidate-then-verify.

  Stage 1 (mechanical): group pending, non-superseded findings by
  primary_node_id. Any group of size >= 2 is a CANDIDATE CLUSTER —
  these findings might share one root question (e.g. 8 wiki_contradiction
  findings on Annika all pointing at "did she stop art lessons?", or 5
  duplicate_node findings on Friday Night Meats listing 5 candidate
  twins).

  Stage 2 (LLM): the kg_finding_cluster_resolver agent reads each
  candidate cluster's reasons + evidence and decides whether the findings
  actually share one root question. If yes:
    - the agent picks a `lead_finding_id` (the one whose reason best
      states the issue),
    - synthesizes a `root_question` (what the user would actually answer),
    - lists the cluster's `member_finding_ids` (subset of the candidate).
  Members other than the lead get superseded_by=lead_id; the lead's
  evidence_json gets a `cluster` block with the root question + sibling
  ids. The maintenance UI hides superseded findings by default.

Conservative defaults:
  - The agent never auto-resolves; it only clusters. The lead still goes
    through the normal review/execute path.
  - We skip already-superseded findings. A re-run can re-cluster newly
    arrived findings around the same lead (idempotent — the lead's
    evidence_json gets updated with new sibling_ids).
  - Single-finding "clusters" are no-ops; we don't even call the LLM.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.kg_maintenance.store import get_findings
from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.models.base import get_session

logger = get_logger(__name__)


# Don't bother the LLM with massive clusters in one shot; the prompt gets
# unwieldy and the agent's judgment degrades. If a primary node has more
# than this many pending findings, we still cluster — but we hand them
# over in chunks of this size. Each chunk gets its own verdict; if the
# agent says "same root" on multiple chunks for the same primary, the
# leads of later chunks get rolled into the first chunk's lead in a
# follow-up sweep (deferred — clusters this large are rare).
MAX_CLUSTER_SIZE_PER_LLM_CALL = 12


def run(ctx: PipelineContext) -> dict:
    """Cluster pending findings by primary_node_id, verify with LLM, apply.

    Returns {"candidates_examined": int, "clusters_confirmed": int,
             "findings_superseded": int}.
    """
    from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding

    candidates = _build_candidate_clusters()
    if not candidates:
        logger.info("[cluster_resolve] no candidate clusters; nothing to do")
        return {
            "candidates_examined": 0,
            "clusters_confirmed": 0,
            "findings_superseded": 0,
        }

    agent = DI.agent_factory.create_agent("kg_finding_cluster_resolver")
    if agent is None:
        raise RuntimeError("Failed to create kg_finding_cluster_resolver agent")

    confirmed = 0
    superseded = 0
    examined = 0

    for primary_node_id, findings in candidates.items():
        # Process in chunks if the cluster is huge (rare).
        for chunk_start in range(0, len(findings), MAX_CLUSTER_SIZE_PER_LLM_CALL):
            chunk = findings[chunk_start : chunk_start + MAX_CLUSTER_SIZE_PER_LLM_CALL]
            if len(chunk) < 2:
                continue
            examined += 1

            payload = _serialize_cluster_for_llm(primary_node_id, chunk)
            agent_input = {"candidate_cluster": payload}
            try:
                result = agent.action_handler(Message(agent_input=agent_input))
            except Exception as e:
                logger.error(
                    "[cluster_resolve] agent call failed for primary=%s: %s",
                    primary_node_id, e, exc_info=True,
                )
                continue

            data = getattr(result, "data", None)
            if not isinstance(data, dict):
                logger.warning(
                    "[cluster_resolve] agent returned non-dict for primary=%s: %s",
                    primary_node_id, type(data).__name__,
                )
                continue

            if not data.get("is_same_root"):
                continue

            lead_id = data.get("lead_finding_id")
            member_ids = list(data.get("member_finding_ids") or [])
            root_question = (data.get("root_question") or "").strip()
            if not lead_id or not member_ids or not root_question:
                logger.warning(
                    "[cluster_resolve] agent said is_same_root but lead/members/question missing: %s",
                    {k: data.get(k) for k in ("lead_finding_id", "member_finding_ids", "root_question")},
                )
                continue
            # Lead must be one of the chunk members; siblings must be a subset.
            chunk_ids = {f["id"] for f in chunk}
            if lead_id not in chunk_ids:
                logger.warning(
                    "[cluster_resolve] lead_finding_id %s not in candidate chunk; skipping",
                    lead_id,
                )
                continue
            member_ids = [m for m in member_ids if m in chunk_ids]
            if lead_id not in member_ids:
                member_ids.append(lead_id)
            sibling_ids = [m for m in member_ids if m != lead_id]
            if not sibling_ids:
                continue  # cluster of one — no consolidation gain

            n = _apply_verdict(
                lead_id=lead_id,
                sibling_ids=sibling_ids,
                root_question=root_question,
                agent_reason=str(data.get("reason") or "")[:500],
            )
            confirmed += 1
            superseded += n

    logger.info(
        "[cluster_resolve] candidates_examined=%d clusters_confirmed=%d findings_superseded=%d",
        examined, confirmed, superseded,
    )
    return {
        "candidates_examined": examined,
        "clusters_confirmed": confirmed,
        "findings_superseded": superseded,
    }


def _build_candidate_clusters() -> dict[str, list[dict]]:
    """Group pending, non-superseded findings by primary_node_id; keep
    groups of size >= 2 as cluster candidates.

    Excluded finding types:
      - state_auto_closed (already-applied closures, not work)
      - duplicate_edge (mechanically dedup'd elsewhere)
    """
    EXCLUDED_TYPES = {"state_auto_closed", "duplicate_edge"}

    items = get_findings(status="pending", limit=10000)
    by_primary: dict[str, list[dict]] = defaultdict(list)
    for f in items:
        if f.get("superseded_by"):
            continue
        if f.get("finding_type") in EXCLUDED_TYPES:
            continue
        pid = f.get("primary_node_id") or ""
        if not pid:
            continue
        by_primary[pid].append(f)

    return {pid: lst for pid, lst in by_primary.items() if len(lst) >= 2}


def _serialize_cluster_for_llm(primary_node_id: str, chunk: list[dict]) -> str:
    """Render a candidate cluster as a compact prompt-friendly block."""
    primary_label = chunk[0].get("primary_label") or chunk[0].get("label") or ""
    primary_type = chunk[0].get("primary_node_type") or ""

    lines = [
        f"Primary node: {primary_label!r} (id={primary_node_id[:8]}, type={primary_type or 'unknown'})",
        f"Pending findings on this node: {len(chunk)}",
        "",
    ]
    for i, f in enumerate(chunk, 1):
        lines.append(f"--- finding {i} ---")
        lines.append(f"finding_id: {f.get('id')}")
        lines.append(f"finding_type: {f.get('finding_type', '')}")
        sec_label = f.get("secondary_label") or ""
        sec_id = f.get("secondary_node_id") or ""
        if sec_id:
            lines.append(f"secondary_node: {sec_label!r} (id={sec_id[:8]})")
        lines.append(f"reason: {f.get('reason', '')}")
        evidence = f.get("evidence") or f.get("evidence_json")
        if evidence:
            try:
                ev_str = json.dumps(evidence, ensure_ascii=False, default=str)[:600]
            except Exception:
                ev_str = str(evidence)[:600]
            lines.append(f"evidence: {ev_str}")
        lines.append("")
    return "\n".join(lines)


def _apply_verdict(
    *,
    lead_id: str,
    sibling_ids: list[str],
    root_question: str,
    agent_reason: str,
) -> int:
    """Stamp superseded_by on siblings; write cluster block to lead's
    evidence_json. Returns count of siblings actually superseded."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding

    session = get_session()
    try:
        lead = session.query(KGMaintenanceFinding).filter_by(id=lead_id).first()
        if lead is None:
            logger.warning("[cluster_resolve] lead %s vanished before apply", lead_id)
            return 0

        # Stamp siblings.
        sibs = (
            session.query(KGMaintenanceFinding)
            .filter(KGMaintenanceFinding.id.in_(sibling_ids))
            .filter(KGMaintenanceFinding.superseded_by.is_(None))
            .all()
        )
        applied = 0
        for s in sibs:
            s.superseded_by = lead_id
            applied += 1

        # Annotate the lead with the cluster block. Merge into existing
        # evidence_json if present so producers' fields are preserved.
        # SQLAlchemy doesn't track in-place dict mutations on JSON columns
        # — without flag_modified, our reassignment looks like "same dict"
        # and the update silently drops on commit. (Bug observed: 2 live
        # leads ended up with cluster_size>1 but evidence_json.cluster={}.)
        existing = lead.evidence_json or {}
        if not isinstance(existing, dict):
            existing = {"_legacy_evidence": existing}
        cluster_block = {
            "is_lead": True,
            "root_question": root_question,
            "sibling_ids": [s.id for s in sibs],
            "verified_by": "kg_finding_cluster_resolver",
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "agent_reason": agent_reason,
        }
        existing["cluster"] = cluster_block
        lead.evidence_json = existing
        flag_modified(lead, "evidence_json")

        session.commit()
        return applied
    except Exception:
        session.rollback()
        logger.error("[cluster_resolve] apply_verdict failed", exc_info=True)
        raise
    finally:
        session.close()


def cascade_status_to_siblings(lead_id: str, new_status: str) -> int:
    """When a cluster lead's status changes (executed/rejected/investigated),
    propagate the same status to every finding superseded_by lead_id.

    Returns the number of siblings updated. Called from the maintenance API
    after the lead's status is set, NOT inside the action handler — keeps
    the cascade explicit and skippable in tests."""
    from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding

    session = get_session()
    try:
        sibs = (
            session.query(KGMaintenanceFinding)
            .filter(KGMaintenanceFinding.superseded_by == lead_id)
            .filter(KGMaintenanceFinding.status == "pending")
            .all()
        )
        if not sibs:
            return 0
        now = datetime.now(timezone.utc)
        for s in sibs:
            s.status = new_status
            s.executed_by = "cluster_cascade"
            s.executed_at = now
            s.execution_notes = f"Cascaded from cluster lead {lead_id} (status={new_status})."
        session.commit()
        return len(sibs)
    except Exception:
        session.rollback()
        logger.error("[cluster_resolve] cascade failed for lead=%s", lead_id, exc_info=True)
        raise
    finally:
        session.close()
