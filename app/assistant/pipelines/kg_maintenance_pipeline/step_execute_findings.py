"""
Step: execute_findings

Automatically executes pending maintenance findings that have safe,
well-defined actions:

  - orphan_node  → delete (if node still has zero edges)
  - duplicate_node → merge (reroute edges, merge fields, delete loser)

Manual-only path (executor present but not in AUTO_EXECUTE_TYPES, so the
pipeline never runs them autopilot — only the UI's "execute" button does):

  - wiki_contradiction with investigation_report_json.proposed_action.op
    == "split_state" → undo a wrong-merge by creating N new State/Event
    nodes (one per participant group), rewiring edges to the right new
    node, and deleting the old hub. Reversible via kg_revision_log
    op="split_state".

  - wiki_contradiction with investigation_report_json.proposed_action.op
    == "batch_text_substitute" → apply per-record text edits listed in
    investigation_report_json.affected_records (e.g. replace 'age 15' with
    'age 14' across N rows). Each record is verified against the live DB
    before mutating; drift aborts the whole batch. Reversible via
    kg_revision_log op="batch_text_substitute".

Suspect-node findings are NOT auto-executed — they require human review
or a redesigned evaluation pipeline.

Each finding is processed in its own transaction.  On failure the finding
is marked ``execute_error`` and the step continues to the next one.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.database.kg_revision_log import KGRevisionLog
from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)

AUTO_EXECUTE_TYPES = {"orphan_node", "duplicate_node"}


# ---------------------------------------------------------------------------
# Orphan execution
# ---------------------------------------------------------------------------

def _execute_orphan(finding: dict) -> dict:
    """
    Delete an orphan node if it still has zero edges.

    Returns a result dict with ``executed`` bool and ``detail`` string.
    """
    from app.assistant.kg.db.knowledge_graph_db import Edge, Node
    from app.assistant.kg_core.kg_utils.kg_tools import delete_node

    node_id = finding["primary_node_id"]

    session = get_session()
    try:
        node = session.query(Node).filter_by(id=node_id).first()
        if node is None:
            return {"executed": True, "detail": "Node already deleted."}

        edge_count = (
            session.query(Edge.id)
            .filter((Edge.source_id == node_id) | (Edge.target_id == node_id))
            .count()
        )
        if edge_count > 0:
            return {"executed": False, "detail": f"Node gained {edge_count} edge(s) since scan — skipping."}

        label = node.label or ""
        node_type = node.node_type or ""
        delete_node(node_id, session)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    _delete_node_embedding(node_id)

    return {
        "executed": True,
        "detail": f"Deleted orphan node '{label}' (type={node_type}).",
    }


# ---------------------------------------------------------------------------
# Duplicate execution
# ---------------------------------------------------------------------------

def _execute_duplicate(finding: dict) -> dict:
    """
    Merge two duplicate nodes: keep the canonical one (winner), reroute the
    loser's edges, merge fields via LLM, delete loser.

    The LLM agent (kg_maintenance::node_data_merger) intelligently combines
    aliases, tags, descriptions, dates, and semantic fields.  The DB session
    is closed before the LLM call and reopened for the write phase.

    Returns a result dict.
    """
    from app.assistant.kg.db.knowledge_graph_db import Node
    from app.assistant.kg_core.kg_utils.node_merge import (
        merge_nodes_in_session,
        snapshot_node,
    )
    from app.assistant.pipelines.kg_shared import (
        apply_node_data_merger_result,
        merge_node_fields_into_existing,
    )

    node_a_id = finding["primary_node_id"]
    node_b_id = finding["secondary_node_id"]

    if not node_b_id:
        return {"executed": False, "detail": "Finding has no secondary_node_id."}

    # Phase 1: read both nodes (short session)
    session = get_session()
    try:
        node_a = session.query(Node).filter_by(id=node_a_id).first()
        node_b = session.query(Node).filter_by(id=node_b_id).first()

        if node_a is None and node_b is None:
            return {"executed": True, "detail": "Both nodes already deleted."}
        if node_a is None:
            return {"executed": True, "detail": f"Node A ({node_a_id[:12]}) already deleted; B remains."}
        if node_b is None:
            return {"executed": True, "detail": f"Node B ({node_b_id[:12]}) already deleted; A remains."}

        winner, loser = _pick_canonical(node_a, node_b, session)
        winner_id = str(winner.id)
        loser_id = str(loser.id)
        winner_label = winner.label
        loser_label = loser.label
        winner_data = _node_to_dict(winner)
        loser_data = _node_to_dict(loser)
    finally:
        session.close()

    # Phase 2: LLM field merge (no session open)
    merged_fields = _llm_merge_fields(winner_data, loser_data)

    # Phase 3: apply merge in a single transaction via the central helper.
    #   - Snapshot the winner BEFORE applying field merges so the merge log
    #     captures the pre-mutation state (needed for unmerge to restore the
    #     winner's original fields).
    #   - merge_nodes_in_session does: reroute edges, rebind dependents via
    #     NODE_ID_REFERENCES, write kg_merge_log, delete loser.
    session = get_session()
    try:
        winner_node = session.query(Node).filter_by(id=winner_id).first()
        loser_node = session.query(Node).filter_by(id=loser_id).first()

        if winner_node is None:
            return {"executed": False, "detail": f"Winner node '{winner_label}' disappeared during LLM call."}
        if loser_node is None:
            return {"executed": True, "detail": f"Loser node '{loser_label}' disappeared during LLM call; winner remains."}

        winner_pre_snapshot = snapshot_node(winner_node)

        if merged_fields:
            apply_node_data_merger_result(winner_node, merged_fields)
            if merged_fields.get("merged_description"):
                winner_node.description = merged_fields["merged_description"]
        else:
            merge_node_fields_into_existing(winner_node, loser_data)
        session.flush()

        merge_log_id = merge_nodes_in_session(
            session,
            loser_node=loser_node,
            winner_node=winner_node,
            merge_actor="kg_maintenance::_execute_duplicate",
            notes=f"merge_method={'LLM' if merged_fields else 'field-copy'}",
            winner_pre_snapshot=winner_pre_snapshot,
        )

        # Pull counts from the log row before we commit so the return detail
        # stays faithful.
        from app.assistant.database.kg_merge_log import KGMergeLog
        log_row = session.query(KGMergeLog).filter_by(id=merge_log_id).first()
        edges_rerouted = len(log_row.rerouted_edge_ids_json or [])
        edges_dropped = len(log_row.dropped_edge_snapshots_json or [])

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    _delete_node_embedding(loser_id)

    merge_method = "LLM" if merged_fields else "field-copy"
    return {
        "executed": True,
        "detail": (
            f"Merged '{loser_label}' into '{winner_label}' ({merge_method}). "
            f"Edges rerouted: {edges_rerouted}, dropped (conflict): {edges_dropped}. "
            f"Merge log id: {merge_log_id[:8]}."
        ),
    }


def _llm_merge_fields(winner_data: dict, loser_data: dict) -> Optional[dict]:
    """
    Call kg_maintenance::node_data_merger to intelligently merge fields.
    Returns the agent's merged field dict, or None on failure (caller falls
    back to simple field-copy merge).
    """
    import json
    from app.assistant.pipelines.scope_policy import build_pipeline_scope_context
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import Message

    try:
        agent = DI.agent_factory.create_agent("kg_maintenance::node_data_merger")
        if agent is None:
            logger.error("[execute_findings] Failed to create kg_maintenance::node_data_merger agent")
            return None

        scope_context = build_pipeline_scope_context(
            pipeline_id="kg_maintenance_pipeline",
            actor_id="kg_maintenance_executor",
        )

        response = agent.action_handler(
            Message(
                agent_input={
                    "existing_node_data": json.dumps(winner_data, ensure_ascii=True, default=str),
                    "new_node_data": json.dumps(loser_data, ensure_ascii=True, default=str),
                },
                scope_context=scope_context,
            )
        )

        if response and response.data:
            logger.info(
                "[execute_findings] LLM merge for '%s' ← '%s': confidence=%.2f",
                winner_data.get("label", "?"),
                loser_data.get("label", "?"),
                response.data.get("merge_confidence", 0),
            )
            return response.data

        logger.error("[execute_findings] node_data_merger returned empty result")
        return None

    except Exception as exc:
        logger.error("[execute_findings] LLM merge failed, falling back to field-copy: %s", exc)
        logger.debug("[execute_findings] LLM merge exception", exc_info=True)
        return None


def _pick_canonical(node_a, node_b, session) -> tuple:
    """
    Choose which node to keep (winner) and which to absorb (loser).
    Prefer: higher pagerank_score → more edges → older created_at.
    """
    from app.assistant.kg.db.knowledge_graph_db import Edge

    def _edge_count(node_id: str) -> int:
        return (
            session.query(Edge.id)
            .filter((Edge.source_id == node_id) | (Edge.target_id == node_id))
            .count()
        )

    pr_a = node_a.pagerank_score or 0.0
    pr_b = node_b.pagerank_score or 0.0
    if pr_a != pr_b:
        return (node_a, node_b) if pr_a >= pr_b else (node_b, node_a)

    ec_a = _edge_count(str(node_a.id))
    ec_b = _edge_count(str(node_b.id))
    if ec_a != ec_b:
        return (node_a, node_b) if ec_a >= ec_b else (node_b, node_a)

    if node_a.created_at and node_b.created_at:
        return (node_a, node_b) if node_a.created_at <= node_b.created_at else (node_b, node_a)

    return (node_a, node_b)


def _node_to_dict(node) -> dict[str, Any]:
    """Extract a plain dict from a Node ORM object for merge_node_fields_into_existing."""
    return {
        "label": node.label,
        "node_type": node.node_type,
        "aliases": node.aliases or [],
        "hash_tags": node.hash_tags or [],
        "semantic_label": node.semantic_label,
        "goal_status": node.goal_status,
        "valid_during": node.valid_during,
        "category": node.category,
        "start_date": node.start_date,
        "end_date": node.end_date,
        "start_date_confidence": node.start_date_confidence,
        "end_date_confidence": node.end_date_confidence,
        "confidence": node.confidence,
        "importance": node.importance,
    }


# ---------------------------------------------------------------------------
# ChromaDB cleanup (best-effort)
# ---------------------------------------------------------------------------

def _delete_node_embedding(node_id: str) -> None:
    try:
        from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
        cm = get_chroma_manager()
        cm.delete_node_embedding(node_id)
        cm.delete_node_context_embedding(node_id)
    except Exception as exc:
        logger.debug("ChromaDB embedding cleanup failed for node %s: %s", node_id, exc)


# ---------------------------------------------------------------------------
# Finding-level dispatcher
# ---------------------------------------------------------------------------

_EXECUTORS = {
    "orphan_node": _execute_orphan,
    "duplicate_node": _execute_duplicate,
}


# ---------------------------------------------------------------------------
# State-split execution (undo a wrong-merge)
# ---------------------------------------------------------------------------

def _parse_iso_dt(value: Any) -> Optional[datetime]:
    """Parse an ISO datetime string, return UTC-aware datetime. None passes through."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Try date-only "YYYY-MM-DD"
        try:
            dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise ValueError(f"Bad date {value!r}: {exc}") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _snapshot_node(n) -> dict:
    return {c.name: getattr(n, c.name) for c in n.__table__.columns}


def _snapshot_edge(e) -> dict:
    return {c.name: getattr(e, c.name) for c in e.__table__.columns}


def _json_safe(d: dict) -> dict:
    out: dict = {}
    for k, v in d.items():
        if isinstance(v, datetime):
            v2 = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
            out[k] = v2.astimezone(timezone.utc).isoformat()
        elif v is None or isinstance(v, (int, float, str, bool, list, dict)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def _execute_split_state(finding: dict) -> dict:
    """Apply a state-node split based on the investigator's proposed plan.

    Reads ``finding['investigation_report_json']['proposed_action']`` and
    expects this shape::

        {
          "op": "split_state",
          "old_node_id": "<uuid>",                # the over-merged hub
          "buckets": [
            {
              "label": "Parenthood",              # required
              "node_type": "State",               # required, must match old
              "category": "parenthood",           # optional, defaults from old
              "original_sentence": "...",         # optional, defaults from old
              "description": "...",               # optional
              "start_date": "1975-03-18",         # optional (ISO date or datetime)
              "end_date": null,                   # optional
              "edge_ids": ["<uuid>", ...]         # required, len >= 1
            },
            ...                                   # >= 2 buckets total
          ]
        }

    Validations:
      - Old node exists, is a State or Event (never an Entity).
      - Each ``edge_ids`` entry is currently attached to old_node_id.
      - Every edge on old_node_id appears in exactly one bucket — no
        leftovers, no duplicates.
      - Bucket node_type matches old node's node_type.

    Mutation (single transaction):
      1. Snapshot old node + all attached edges (for revert).
      2. Create one new node per bucket.
      3. Rewire each bucket's edges to its new node.
      4. Delete the old node.
      5. Write one ``split_state`` row to kg_revision_log with full
         before/after snapshots for revert.

    Reversal: undo the split_state row by recreating the old node from
    ``before_json.old_node`` and rewiring all edges back to it, then
    deleting the new nodes (not implemented here — record + manual undo
    until a reverse_split_state op is added).
    """
    from app.assistant.kg.db.knowledge_graph_db import Edge, Node

    report = finding.get("investigation_report_json") or {}
    proposed = report.get("proposed_action") or {}
    if not isinstance(proposed, dict):
        return {"executed": False, "detail": "investigation_report_json.proposed_action is missing or not a dict."}
    if proposed.get("op") != "split_state":
        return {"executed": False, "detail": f"proposed_action.op is {proposed.get('op')!r}, expected 'split_state'."}

    old_node_id = proposed.get("old_node_id")
    buckets = proposed.get("buckets") or []
    if not old_node_id:
        return {"executed": False, "detail": "proposed_action.old_node_id is missing."}
    if not isinstance(buckets, list) or len(buckets) < 2:
        return {"executed": False, "detail": "proposed_action.buckets must be a list with >= 2 entries."}

    session = get_session()
    try:
        old = session.query(Node).filter_by(id=old_node_id).first()
        if old is None:
            return {"executed": False, "detail": f"Old node {old_node_id[:12]} not found (already deleted?)."}
        if (old.node_type or "") not in ("State", "Event"):
            return {"executed": False, "detail": f"Refusing to split node_type={old.node_type!r}; only State/Event are splittable."}

        attached_edges = (
            session.query(Edge)
            .filter((Edge.source_id == old_node_id) | (Edge.target_id == old_node_id))
            .all()
        )
        attached_ids = {str(e.id) for e in attached_edges}

        seen_ids: set[str] = set()
        bucket_specs: list[dict] = []
        for i, b in enumerate(buckets):
            if not isinstance(b, dict):
                return {"executed": False, "detail": f"buckets[{i}] is not a dict."}
            label = (b.get("label") or "").strip()
            node_type = (b.get("node_type") or "").strip()
            edge_ids = b.get("edge_ids") or []
            if not label:
                return {"executed": False, "detail": f"buckets[{i}].label is required."}
            if node_type != (old.node_type or ""):
                return {"executed": False, "detail": f"buckets[{i}].node_type={node_type!r} doesn't match old node_type={old.node_type!r}."}
            if not isinstance(edge_ids, list) or not edge_ids:
                return {"executed": False, "detail": f"buckets[{i}].edge_ids must be a non-empty list."}
            for eid in edge_ids:
                eid_str = str(eid)
                if eid_str not in attached_ids:
                    return {"executed": False, "detail": f"buckets[{i}].edge_ids contains {eid_str[:12]} which is not attached to old node."}
                if eid_str in seen_ids:
                    return {"executed": False, "detail": f"Edge {eid_str[:12]} appears in more than one bucket."}
                seen_ids.add(eid_str)
            try:
                start_dt = _parse_iso_dt(b.get("start_date"))
                end_dt = _parse_iso_dt(b.get("end_date"))
            except ValueError as exc:
                return {"executed": False, "detail": f"buckets[{i}] has unparseable date: {exc}"}
            bucket_specs.append({
                "label": label,
                "node_type": node_type,
                "category": b.get("category") if "category" in b else old.category,
                "original_sentence": b.get("original_sentence") if "original_sentence" in b else old.original_sentence,
                "description": b.get("description") if "description" in b else None,
                "start_date": start_dt,
                "end_date": end_dt,
                "edge_ids": [str(x) for x in edge_ids],
            })

        leftover = attached_ids - seen_ids
        if leftover:
            return {
                "executed": False,
                "detail": f"{len(leftover)} edge(s) on the old node are unrouted: {sorted(leftover)[:5]}{'...' if len(leftover) > 5 else ''}",
            }

        # ---- mutate ----
        before_snapshot = {
            "old_node": _json_safe(_snapshot_node(old)),
            "edges": [_json_safe(_snapshot_edge(e)) for e in attached_edges],
        }

        new_node_ids: list[str] = []
        for spec in bucket_specs:
            nid = str(uuid.uuid4())
            new_node_ids.append(nid)
            n = Node(
                id=nid,
                label=spec["label"],
                node_type=spec["node_type"],
                category=spec["category"],
                description=spec["description"],
                original_sentence=spec["original_sentence"],
                start_date=spec["start_date"],
                end_date=spec["end_date"],
                confidence=getattr(old, "confidence", None),
                importance=getattr(old, "importance", None),
                source=getattr(old, "source", None),
            )
            session.add(n)
        session.flush()

        edge_lookup = {str(e.id): e for e in attached_edges}
        for new_id, spec in zip(new_node_ids, bucket_specs):
            for eid in spec["edge_ids"]:
                e = edge_lookup[eid]
                if e.source_id == old_node_id:
                    e.source_id = new_id
                if e.target_id == old_node_id:
                    e.target_id = new_id
        session.flush()

        session.delete(old)
        session.flush()

        after_snapshot = {
            "new_node_ids": new_node_ids,
            "new_nodes": [
                _json_safe(_snapshot_node(session.query(Node).filter_by(id=nid).first()))
                for nid in new_node_ids
            ],
        }

        rev = KGRevisionLog(
            id=str(uuid.uuid4()),
            op="split_state",
            args_json={"old_node_id": old_node_id, "n_buckets": len(bucket_specs)},
            before_json=before_snapshot,
            after_json=after_snapshot,
            reason=f"split_state via finding {finding.get('id', '?')[:8]}",
            finding_id=finding.get("id"),
            agent_id="kg_maintenance::_execute_split_state",
            succeeded=1,
        )
        session.add(rev)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    _delete_node_embedding(old_node_id)

    return {
        "executed": True,
        "detail": (
            f"Split node {old_node_id[:12]} into {len(new_node_ids)} new {bucket_specs[0]['node_type']} "
            f"nodes ({', '.join(s['label'] for s in bucket_specs)}). Logged as split_state revision."
        ),
    }


# ---------------------------------------------------------------------------
# Batch text-substitute execution (act on investigation_report_json.affected_records)
# ---------------------------------------------------------------------------

# Allowlist of text columns the executor may touch. Keeping this tight makes
# the surface auditable and prevents an investigator from suggesting an op
# that lands on a column we haven't reasoned about.
_ALLOWED_NODE_FIELDS = frozenset({"original_sentence", "description", "label"})
_ALLOWED_EDGE_FIELDS = frozenset({"sentence"})


def _execute_batch_text_substitute(finding: dict) -> dict:
    """Apply a per-record text-substitute plan from `affected_records`.

    Reads ``finding['investigation_report_json']['affected_records']`` (the
    structured per-row inventory the planner enumerated and the final-answer
    agent harvested) and applies each entry's recommendation. Validates every
    record against the live DB before mutating anything — if the
    `current_value` an investigator observed has drifted (someone else edited
    the row meanwhile), the entire batch refuses rather than overwriting an
    unfamiliar value.

    Per-record op handling:
      - ``update_text``: SET <field> = <new>. Requires current_value match.
      - ``no_change``:   skip (informational only — counted, not applied).
      - ``delete_edge`` / ``delete_node``: rejected here; those need their own
                         dedicated executor primitives (different audit
                         shapes, irreversibility profile differs).

    All updates run in one transaction. Logs a single ``batch_text_substitute``
    row to ``kg_revision_log`` with full before/after snapshots so the whole
    batch can be reverted in one operation.
    """
    from app.assistant.kg.db.knowledge_graph_db import Edge, Node

    report = finding.get("investigation_report_json") or {}
    proposed = report.get("proposed_action") or {}
    if not isinstance(proposed, dict) or proposed.get("op") != "batch_text_substitute":
        return {"executed": False, "detail": "proposed_action.op is not 'batch_text_substitute'."}

    affected = report.get("affected_records") or []
    if not isinstance(affected, list) or not affected:
        return {"executed": False, "detail": "affected_records is missing or empty — nothing to apply."}

    # Pre-validate every record before touching anything.
    plan: list[dict] = []
    skipped_no_change = 0
    for i, rec in enumerate(affected):
        if not isinstance(rec, dict):
            return {"executed": False, "detail": f"affected_records[{i}] is not a dict."}
        rec_rec = rec.get("recommendation") or {}
        op = rec_rec.get("op") if isinstance(rec_rec, dict) else None
        if op == "no_change":
            skipped_no_change += 1
            continue
        if op in ("delete_edge", "delete_node"):
            return {"executed": False, "detail": (
                f"affected_records[{i}].recommendation.op={op!r} requires a dedicated "
                "deletion executor; not handled by batch_text_substitute."
            )}
        if op != "update_text":
            return {"executed": False, "detail": f"affected_records[{i}].recommendation.op={op!r} is not supported (expected update_text|no_change)."}

        table = rec.get("table") or ""
        record_id = rec.get("id") or ""
        field = rec.get("field") or ""
        observed_current = rec.get("current_value")
        new_value = rec_rec.get("new")

        if not record_id:
            return {"executed": False, "detail": f"affected_records[{i}].id is missing."}
        if new_value is None:
            return {"executed": False, "detail": f"affected_records[{i}] op=update_text requires recommendation.new."}
        if observed_current == new_value:
            return {"executed": False, "detail": f"affected_records[{i}] new equals current_value — no-op disguised as update_text."}

        if table == "kg_node_metadata":
            if field not in _ALLOWED_NODE_FIELDS:
                return {"executed": False, "detail": f"affected_records[{i}].field={field!r} not in node allowlist {sorted(_ALLOWED_NODE_FIELDS)}."}
        elif table == "kg_edge_metadata":
            if field not in _ALLOWED_EDGE_FIELDS:
                return {"executed": False, "detail": f"affected_records[{i}].field={field!r} not in edge allowlist {sorted(_ALLOWED_EDGE_FIELDS)}."}
        else:
            return {"executed": False, "detail": f"affected_records[{i}].table={table!r} not supported (expected kg_node_metadata|kg_edge_metadata)."}

        plan.append({
            "table": table,
            "id": str(record_id),
            "field": field,
            "observed_current": observed_current,
            "new": new_value,
            "rationale": rec.get("rationale") or "",
        })

    if not plan:
        return {"executed": True, "detail": f"No records to mutate (all {skipped_no_change} were no_change)."}

    # Verify-and-apply in one transaction. On any drift we abort the whole batch.
    before_snapshot: list[dict] = []
    after_snapshot: list[dict] = []
    session = get_session()
    try:
        for entry in plan:
            cls = Node if entry["table"] == "kg_node_metadata" else Edge
            row = session.query(cls).filter_by(id=entry["id"]).first()
            if row is None:
                return {"executed": False, "detail": f"Row {entry['table']}/{entry['id'][:12]} not found."}
            live_current = getattr(row, entry["field"], None)
            if live_current != entry["observed_current"]:
                return {"executed": False, "detail": (
                    f"Drift detected on {entry['table']}/{entry['id'][:12]}.{entry['field']}: "
                    f"investigator observed {entry['observed_current']!r} but live value is "
                    f"{(live_current or '')[:80]!r}. Refusing to overwrite — re-run the investigator."
                )}

        # No drift; apply.
        for entry in plan:
            cls = Node if entry["table"] == "kg_node_metadata" else Edge
            row = session.query(cls).filter_by(id=entry["id"]).first()
            old_value = getattr(row, entry["field"])
            setattr(row, entry["field"], entry["new"])
            before_snapshot.append({
                "table": entry["table"],
                "id": entry["id"],
                "field": entry["field"],
                "value": old_value,
            })
            after_snapshot.append({
                "table": entry["table"],
                "id": entry["id"],
                "field": entry["field"],
                "value": entry["new"],
            })
        session.flush()

        rev = KGRevisionLog(
            id=str(uuid.uuid4()),
            op="batch_text_substitute",
            args_json={
                "n_updated": len(plan),
                "n_skipped_no_change": skipped_no_change,
                "tables_touched": sorted(set(e["table"] for e in plan)),
            },
            before_json=before_snapshot,
            after_json=after_snapshot,
            reason=f"batch_text_substitute via finding {finding.get('id', '?')[:8]}",
            finding_id=finding.get("id"),
            agent_id="kg_maintenance::_execute_batch_text_substitute",
            succeeded=1,
        )
        session.add(rev)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return {
        "executed": True,
        "detail": (
            f"Updated {len(plan)} record(s); {skipped_no_change} marked no_change. "
            f"Logged as batch_text_substitute revision (reversible)."
        ),
    }


def execute_single_finding(finding: dict) -> dict:
    """
    Execute a single finding.  Returns a result dict with at least
    ``executed`` (bool) and ``detail`` (str).

    Raises on unexpected errors (caller should catch and mark execute_error).
    """
    ftype = finding.get("finding_type", "")

    # Structured-action path: a wiki_contradiction whose investigator wrote
    # an executable plan into investigation_report_json.proposed_action can
    # be acted on directly. Supported ops: split_state, batch_text_substitute.
    if ftype == "wiki_contradiction":
        report = finding.get("investigation_report_json") or {}
        proposed = report.get("proposed_action") or {}
        op = proposed.get("op") if isinstance(proposed, dict) else None
        if op == "split_state":
            return _execute_split_state(finding)
        if op == "batch_text_substitute":
            return _execute_batch_text_substitute(finding)
        return {"executed": False, "detail": f"No executor for proposed_action op {op!r}; needs human review."}

    executor = _EXECUTORS.get(ftype)
    if executor is None:
        return {"executed": False, "detail": f"No executor for finding_type '{ftype}'."}
    return executor(finding)


# ---------------------------------------------------------------------------
# Pipeline step entry point
# ---------------------------------------------------------------------------

def run(ctx: PipelineContext, *, dry_run: bool = False) -> dict:
    """
    Process all pending findings of auto-executable types.

    If ``dry_run`` is True, reports what would happen without mutating.

    Returns {"executed": int, "skipped": int, "errors": int, "details": [...]}.
    """
    from app.assistant.kg_maintenance.store import get_findings, set_status

    executed = 0
    skipped = 0
    errors = 0
    details: list[dict] = []

    for ftype in sorted(AUTO_EXECUTE_TYPES):
        findings = get_findings(status="approved", finding_type=ftype, limit=500)
        logger.info("[execute_findings] %d pending %s findings", len(findings), ftype)

        for finding in findings:
            fid = finding["id"]

            if dry_run:
                details.append({"finding_id": fid, "type": ftype, "dry_run": True, "action": finding.get("suggested_action")})
                skipped += 1
                continue

            try:
                result = execute_single_finding(finding)
            except Exception as exc:
                logger.error("[execute_findings] Failed finding_id=%s: %s", fid, exc)
                logger.debug("[execute_findings] Exception details", exc_info=True)
                set_status(fid, "execute_error", executed_by="auto_pipeline", execution_notes=str(exc)[:500])
                errors += 1
                details.append({"finding_id": fid, "type": ftype, "error": str(exc)[:200]})
                continue

            if result.get("executed"):
                set_status(fid, "executed", executed_by="auto_pipeline", execution_notes=result.get("detail", ""))
                executed += 1
            else:
                set_status(fid, "rejected", executed_by="auto_pipeline", execution_notes=result.get("detail", ""))
                skipped += 1

            details.append({"finding_id": fid, "type": ftype, **result})

    logger.info(
        "[execute_findings] Done: executed=%d skipped=%d errors=%d",
        executed, skipped, errors,
    )
    return {
        "executed": executed,
        "skipped": skipped,
        "errors": errors,
        "details": details[:100],
    }
