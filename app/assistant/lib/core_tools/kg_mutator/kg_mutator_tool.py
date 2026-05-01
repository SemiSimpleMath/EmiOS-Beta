"""KGMutatorTool — narrow typed mutators against the live KG, audit-wired.

Backs nine tool names (one BaseTool, dispatched on tool_name):

  kg_merge_nodes(keep_id, fold_id, reason, dry_run=False)
  kg_rename_label(node_id, new_label, reason, dry_run=False)
  kg_update_node_field(node_id, field, value, reason, dry_run=False)
  kg_delete_edge(edge_id, reason, dry_run=False)
  kg_create_state_node(owner_node_id, predicate, label, ..., reason, dry_run=False)
  kg_create_edge(source_id, target_id, relationship_type, ..., reason, dry_run=False)
  kg_close_state(node_id, end_date, ..., reason, dry_run=False)
  kg_finding_resolve(finding_id, action, notes, reason)
  kg_finding_escalate(finding_id, summary, suggested_action, reason)

Common contract:
  - Every successful commit writes a row to kg_revision_log with
    before/after snapshots so the change can be reverted.
  - Every call requires a non-empty ``reason``.
  - dry_run=True returns the diff that would be applied without
    touching the KG.
  - Optional ``finding_id`` ties the revision back to its source
    kg_maintenance_finding row.

Errors are surfaced as ToolResult(content="…", data={"ok": False, ...})
rather than raised exceptions so the caller agent can react.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import text as sql_text

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.database.kg_revision_log import KGRevisionLog
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)


# ---- node-field allowlist for kg_update_node_field -------------------------
# Only narrow, low-blast-radius fields. Anything bigger should be a dedicated
# typed op (rename_label, merge_nodes, etc.).
_ALLOWED_UPDATE_FIELDS = frozenset({
    "category",
    "description",
    "original_sentence",
    "valid_during",
    "start_date",
    "end_date",
    "start_date_prose",
    "end_date_prose",
    "start_date_confidence",
    "end_date_confidence",
    "importance",
    "goal_status",
    "semantic_label",
})

# Fields where the value is a JSON list and we may want to add/remove items
# rather than overwrite. The tool input takes:
#   {field: "aliases", value: [...], list_op: "add" | "remove" | "set"}
_LIST_FIELDS = frozenset({"aliases", "hash_tags"})

_DATE_FIELDS = frozenset({"start_date", "end_date"})


def _parse_date(s: Any) -> Optional[datetime]:
    if s is None or s == "":
        return None
    if isinstance(s, datetime):
        return s
    s = str(s).strip()
    # Try fromisoformat first (handles 'YYYY-MM-DD', 'YYYY-MM-DDTHH:MM:SS',
    # plus offsets like '+00:00' on Python 3.11+).
    try:
        # Normalize a trailing 'Z' to +00:00 so fromisoformat accepts it.
        normalized = s.replace("Z", "+00:00") if s.endswith("Z") else s
        return datetime.fromisoformat(normalized)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    raise ValueError(f"Could not parse date {s!r}")


# ---- snapshot helpers -------------------------------------------------------


def _node_snapshot(node: Node) -> Dict[str, Any]:
    return {
        "id": node.id,
        "label": node.label,
        "node_type": node.node_type,
        "category": node.category,
        "aliases": list(node.aliases or []),
        "description": node.description,
        "original_sentence": node.original_sentence,
        "start_date": node.start_date.isoformat() if node.start_date else None,
        "end_date": node.end_date.isoformat() if node.end_date else None,
        "start_date_prose": node.start_date_prose,
        "end_date_prose": node.end_date_prose,
        "valid_during": getattr(node, "valid_during", None),
        "importance": getattr(node, "importance", None),
        "goal_status": getattr(node, "goal_status", None),
        "semantic_label": getattr(node, "semantic_label", None),
        "hash_tags": list(getattr(node, "hash_tags", None) or []),
        "window_id": node.window_id,
    }


def _edge_snapshot(edge: Edge) -> Dict[str, Any]:
    return {
        "id": edge.id,
        "source_id": edge.source_id,
        "target_id": edge.target_id,
        "relationship_type": edge.relationship_type,
        "sentence": getattr(edge, "sentence", None),
        "window_id": getattr(edge, "window_id", None),
    }


def _write_revision_log(
    *,
    session,
    op: str,
    args: Dict[str, Any],
    before: Any,
    after: Any,
    reason: str,
    finding_id: Optional[str],
    agent_id: Optional[str],
    succeeded: bool = True,
    error_message: Optional[str] = None,
) -> str:
    rid = str(uuid.uuid4())
    session.add(KGRevisionLog(
        id=rid,
        op=op,
        args_json=args,
        before_json=before,
        after_json=after,
        reason=reason,
        finding_id=finding_id,
        agent_id=agent_id,
        succeeded=1 if succeeded else 0,
        error_message=error_message,
    ))
    return rid


# ---- the tool ---------------------------------------------------------------


class KGMutatorTool(BaseTool):
    """Six narrow KG-mutation ops, audit-wired to kg_revision_log."""

    def __init__(self) -> None:
        super().__init__("kg_mutator_tool")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            arguments = (tool_message.tool_data or {}).get("arguments", {}) or {}
            tool_name = tool_message.tool_name or (tool_message.tool_data or {}).get("tool_name")
            if not tool_name:
                raise ValueError("Missing tool_name in tool_data.")
            handler = getattr(self, f"handle_{tool_name}", None)
            if handler is None:
                raise ValueError(f"Unsupported tool_name for KGMutatorTool: {tool_name}")
            return handler(arguments, tool_message)
        except ValueError as e:
            return self.publish_error(make_tool_error(
                error_code="kg_mutator_invalid",
                message=str(e),
                abort_policy="abort_tool",
                retryable=False,
            ))
        except Exception as e:
            logger.error("KGMutatorTool.execute failed: %s", e)
            logger.debug("kg_mutator exception details", exc_info=True)
            return self.publish_error(make_tool_error(
                error_code="kg_mutator_failed",
                message=str(e),
                abort_policy="abort_tool",
                retryable=False,
            ))

    def publish_result(self, result: ToolResult) -> ToolResult:
        return result

    def publish_error(self, error_result: ToolResult) -> ToolResult:
        return error_result

    # ---------------------- shared validators ----------------------

    @staticmethod
    def _require_reason(arguments: Dict[str, Any]) -> str:
        reason = str(arguments.get("reason") or "").strip()
        if not reason:
            raise ValueError("`reason` is required for every mutation op.")
        return reason

    @staticmethod
    def _agent_id(tool_message: ToolMessage) -> Optional[str]:
        scope = getattr(tool_message, "scope_context", None)
        if scope is not None:
            actor = getattr(scope, "actor_id", None)
            if actor:
                return str(actor)
        return None

    # ---------------------- handlers ----------------------

    def handle_kg_merge_nodes(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        keep_id = str(arguments.get("keep_id") or "").strip()
        fold_id = str(arguments.get("fold_id") or "").strip()
        if not keep_id or not fold_id:
            raise ValueError("`keep_id` and `fold_id` are both required.")
        if keep_id == fold_id:
            raise ValueError("`keep_id` and `fold_id` must differ.")
        reason = self._require_reason(arguments)
        dry_run = bool(arguments.get("dry_run", False))
        finding_id = arguments.get("finding_id") or None

        with get_db_manager().transaction(op="kg_mutator.merge_nodes") as session:
            keep = session.query(Node).filter(Node.id == keep_id).first()
            fold = session.query(Node).filter(Node.id == fold_id).first()
            if keep is None:
                raise ValueError(f"keep node {keep_id!r} not found")
            if fold is None:
                raise ValueError(f"fold node {fold_id!r} not found")
            if (keep.node_type or "") != (fold.node_type or ""):
                raise ValueError(
                    f"node_type mismatch: keep={keep.node_type!r} fold={fold.node_type!r}; "
                    "merging across types is not allowed."
                )

            # Edges to rewrite from fold → keep.
            in_edges = session.query(Edge).filter(Edge.target_id == fold_id).all()
            out_edges = session.query(Edge).filter(Edge.source_id == fold_id).all()

            # Index keep's existing edges so we can detect collisions BEFORE the
            # rewrite hits the (source_id, target_id, relationship_type) UNIQUE
            # constraint. Two ways a collision arises:
            #   (a) keep already has the same (other_endpoint, rel_type) pair —
            #       rewriting fold's edge to point at keep would duplicate it.
            #   (b) the fold edge connects fold↔keep — rewriting both/one
            #       endpoint would create a self-loop (source==target).
            # In both cases we drop the redundant fold edge instead of rewriting.
            keep_in_keys = {
                (e.source_id, (e.relationship_type or "").strip().lower())
                for e in session.query(Edge).filter(Edge.target_id == keep_id).all()
            }
            keep_out_keys = {
                (e.target_id, (e.relationship_type or "").strip().lower())
                for e in session.query(Edge).filter(Edge.source_id == keep_id).all()
            }

            in_edges_to_rewrite: List[Edge] = []
            in_edges_to_drop: List[Edge] = []
            for e in in_edges:
                rel = (e.relationship_type or "").strip().lower()
                if e.source_id == keep_id or e.source_id == fold_id:
                    # fold↔keep or fold↔fold becomes a self-loop after rewrite.
                    in_edges_to_drop.append(e)
                elif (e.source_id, rel) in keep_in_keys:
                    in_edges_to_drop.append(e)
                else:
                    in_edges_to_rewrite.append(e)

            out_edges_to_rewrite: List[Edge] = []
            out_edges_to_drop: List[Edge] = []
            for e in out_edges:
                rel = (e.relationship_type or "").strip().lower()
                if e.target_id == keep_id or e.target_id == fold_id:
                    out_edges_to_drop.append(e)
                elif (e.target_id, rel) in keep_out_keys:
                    out_edges_to_drop.append(e)
                else:
                    out_edges_to_rewrite.append(e)

            before = {
                "keep": _node_snapshot(keep),
                "fold": _node_snapshot(fold),
                "in_edges": [_edge_snapshot(e) for e in in_edges],
                "out_edges": [_edge_snapshot(e) for e in out_edges],
                "dropped_redundant_edges": [
                    _edge_snapshot(e) for e in (in_edges_to_drop + out_edges_to_drop)
                ],
            }

            # Build the after preview: aliases unioned, edges rewritten.
            new_aliases = list(dict.fromkeys(
                list(keep.aliases or []) + [fold.label] + list(fold.aliases or [])
            ))
            # Drop the keep node's own label out of its aliases if it slipped in.
            new_aliases = [a for a in new_aliases if a and a != keep.label]

            after_preview = {
                "keep": {**_node_snapshot(keep), "aliases": new_aliases},
                "fold": "DELETED",
                "rewritten_in_edges": [
                    {**_edge_snapshot(e), "target_id": keep_id} for e in in_edges_to_rewrite
                ],
                "rewritten_out_edges": [
                    {**_edge_snapshot(e), "source_id": keep_id} for e in out_edges_to_rewrite
                ],
                "dropped_redundant_edges": [
                    _edge_snapshot(e) for e in (in_edges_to_drop + out_edges_to_drop)
                ],
            }

            if dry_run:
                return self.publish_result(ToolResult(
                    result_type="kg_merge_nodes",
                    content=(
                        f"DRY RUN: would merge {fold.label!r} ({fold_id[:8]}) into "
                        f"{keep.label!r} ({keep_id[:8]}); "
                        f"rewrite {len(in_edges_to_rewrite)} incoming + "
                        f"{len(out_edges_to_rewrite)} outgoing edges; "
                        f"drop {len(in_edges_to_drop) + len(out_edges_to_drop)} redundant edge(s); "
                        f"add {len(new_aliases) - len(keep.aliases or [])} alias(es)."
                    ),
                    data={"ok": True, "dry_run": True, "before": before, "after": after_preview},
                ))

            # Drop redundant edges first so the rewrite below can't collide
            # with a row that's about to be replaced anyway.
            for e in in_edges_to_drop + out_edges_to_drop:
                session.delete(e)
            session.flush()

            for e in in_edges_to_rewrite:
                e.target_id = keep_id
            for e in out_edges_to_rewrite:
                e.source_id = keep_id
            keep.aliases = new_aliases
            # Flush BEFORE delete so the FK CASCADE on Edge.{source,target}_id
            # sees edges already pointing at keep, not still-pointing-at-fold.
            # Without this, SQLite cascades fold's deletion and nulls out the
            # rewritten edges' source_id/target_id, hitting NOT NULL.
            session.flush()
            session.delete(fold)

            rid = _write_revision_log(
                session=session,
                op="merge_nodes",
                args={"keep_id": keep_id, "fold_id": fold_id, "finding_id": finding_id},
                before=before,
                after=after_preview,
                reason=reason,
                finding_id=finding_id,
                agent_id=self._agent_id(tool_message),
            )

            rewritten_count = len(in_edges_to_rewrite) + len(out_edges_to_rewrite)
            dropped_count = len(in_edges_to_drop) + len(out_edges_to_drop)
            return self.publish_result(ToolResult(
                result_type="kg_merge_nodes",
                content=(
                    f"Merged {fold.label!r} into {keep.label!r}. "
                    f"Rewrote {rewritten_count} edge(s), dropped {dropped_count} redundant; "
                    f"aliases now {new_aliases}. "
                    f"revision_log_id={rid}"
                ),
                data={
                    "ok": True,
                    "revision_log_id": rid,
                    "edges_rewritten": rewritten_count,
                    "edges_dropped_redundant": dropped_count,
                    "new_aliases": new_aliases,
                },
            ))

    def handle_kg_rename_label(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        node_id = str(arguments.get("node_id") or "").strip()
        new_label = str(arguments.get("new_label") or "").strip()
        if not node_id or not new_label:
            raise ValueError("`node_id` and `new_label` are required.")
        reason = self._require_reason(arguments)
        dry_run = bool(arguments.get("dry_run", False))
        finding_id = arguments.get("finding_id") or None

        with get_db_manager().transaction(op="kg_mutator.rename_label") as session:
            node = session.query(Node).filter(Node.id == node_id).first()
            if node is None:
                raise ValueError(f"node {node_id!r} not found")
            old_label = node.label
            if old_label == new_label:
                return self.publish_result(ToolResult(
                    result_type="kg_rename_label",
                    content=f"No change: node already labeled {new_label!r}.",
                    data={"ok": True, "no_op": True},
                ))

            # Collision check: a different active node with the new label
            # means this is a merge, not a rename.
            collision = (
                session.query(Node)
                .filter(Node.label == new_label, Node.node_type == node.node_type, Node.id != node_id)
                .first()
            )
            if collision is not None:
                raise ValueError(
                    f"Refusing to rename: another {node.node_type} node with label {new_label!r} "
                    f"already exists (id={collision.id}). Use kg_merge_nodes instead."
                )

            before = _node_snapshot(node)
            new_aliases = list(dict.fromkeys([old_label] + list(node.aliases or [])))

            if dry_run:
                return self.publish_result(ToolResult(
                    result_type="kg_rename_label",
                    content=f"DRY RUN: would rename {old_label!r} → {new_label!r} on node {node_id[:8]}; "
                            f"old label preserved as alias.",
                    data={"ok": True, "dry_run": True, "before": before,
                          "after": {**before, "label": new_label, "aliases": new_aliases}},
                ))

            node.label = new_label
            node.aliases = new_aliases
            after = _node_snapshot(node)

            rid = _write_revision_log(
                session=session,
                op="rename_label",
                args={"node_id": node_id, "new_label": new_label, "finding_id": finding_id},
                before=before,
                after=after,
                reason=reason,
                finding_id=finding_id,
                agent_id=self._agent_id(tool_message),
            )

            return self.publish_result(ToolResult(
                result_type="kg_rename_label",
                content=f"Renamed {old_label!r} → {new_label!r} on node {node_id[:8]}. "
                        f"Old label kept as alias. revision_log_id={rid}",
                data={"ok": True, "revision_log_id": rid, "old_label": old_label, "new_label": new_label},
            ))

    def handle_kg_update_node_field(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        node_id = str(arguments.get("node_id") or "").strip()
        field = str(arguments.get("field") or "").strip()
        if not node_id or not field:
            raise ValueError("`node_id` and `field` are required.")
        if field not in _ALLOWED_UPDATE_FIELDS and field not in _LIST_FIELDS:
            raise ValueError(
                f"field {field!r} is not in the allowlist. "
                f"Allowed: {sorted(_ALLOWED_UPDATE_FIELDS | _LIST_FIELDS)}"
            )
        reason = self._require_reason(arguments)
        dry_run = bool(arguments.get("dry_run", False))
        finding_id = arguments.get("finding_id") or None
        new_value = arguments.get("value")
        list_op = str(arguments.get("list_op") or "set").lower()
        if field in _LIST_FIELDS and list_op not in ("set", "add", "remove"):
            raise ValueError("list_op must be one of: set, add, remove")

        with get_db_manager().transaction(op="kg_mutator.update_node_field") as session:
            node = session.query(Node).filter(Node.id == node_id).first()
            if node is None:
                raise ValueError(f"node {node_id!r} not found")

            before = _node_snapshot(node)
            old_value = before.get(field)

            if field in _LIST_FIELDS:
                current = list(getattr(node, field) or [])
                incoming = new_value if isinstance(new_value, list) else ([new_value] if new_value else [])
                if list_op == "set":
                    new_list = [x for x in incoming if x]
                elif list_op == "add":
                    new_list = list(dict.fromkeys(current + [x for x in incoming if x]))
                else:  # remove
                    drop = set(incoming)
                    new_list = [x for x in current if x not in drop]
                computed_value = new_list
            elif field in _DATE_FIELDS:
                computed_value = _parse_date(new_value)
            else:
                computed_value = new_value if new_value != "" else None

            after_preview = {**before, field: (
                computed_value.isoformat() if isinstance(computed_value, datetime)
                else computed_value
            )}

            if dry_run:
                return self.publish_result(ToolResult(
                    result_type="kg_update_node_field",
                    content=(
                        f"DRY RUN: would update {field!r} on node {node_id[:8]} "
                        f"({before.get('label')!r}): {old_value!r} → {after_preview[field]!r}"
                    ),
                    data={"ok": True, "dry_run": True, "before": before, "after": after_preview},
                ))

            setattr(node, field, computed_value)
            after = _node_snapshot(node)

            rid = _write_revision_log(
                session=session,
                op="update_node_field",
                args={"node_id": node_id, "field": field, "value": after.get(field),
                      "list_op": list_op if field in _LIST_FIELDS else None,
                      "finding_id": finding_id},
                before=before,
                after=after,
                reason=reason,
                finding_id=finding_id,
                agent_id=self._agent_id(tool_message),
            )
            return self.publish_result(ToolResult(
                result_type="kg_update_node_field",
                content=(
                    f"Updated {field!r} on node {node_id[:8]} ({before.get('label')!r}): "
                    f"{old_value!r} → {after.get(field)!r}. revision_log_id={rid}"
                ),
                data={"ok": True, "revision_log_id": rid, "field": field,
                      "old_value": old_value, "new_value": after.get(field)},
            ))

    def handle_kg_delete_edge(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        edge_id = str(arguments.get("edge_id") or "").strip()
        if not edge_id:
            raise ValueError("`edge_id` is required.")
        reason = self._require_reason(arguments)
        dry_run = bool(arguments.get("dry_run", False))
        finding_id = arguments.get("finding_id") or None

        with get_db_manager().transaction(op="kg_mutator.delete_edge") as session:
            edge = session.query(Edge).filter(Edge.id == edge_id).first()
            if edge is None:
                raise ValueError(f"edge {edge_id!r} not found")
            before = _edge_snapshot(edge)

            if dry_run:
                return self.publish_result(ToolResult(
                    result_type="kg_delete_edge",
                    content=f"DRY RUN: would delete edge {edge_id[:8]} "
                            f"({before['source_id'][:8]} -[{before['relationship_type']}]-> {before['target_id'][:8]})",
                    data={"ok": True, "dry_run": True, "before": before, "after": "DELETED"},
                ))

            session.delete(edge)

            rid = _write_revision_log(
                session=session,
                op="delete_edge",
                args={"edge_id": edge_id, "finding_id": finding_id},
                before=before,
                after="DELETED",
                reason=reason,
                finding_id=finding_id,
                agent_id=self._agent_id(tool_message),
            )
            return self.publish_result(ToolResult(
                result_type="kg_delete_edge",
                content=f"Deleted edge {edge_id[:8]} ({before['relationship_type']}). revision_log_id={rid}",
                data={"ok": True, "revision_log_id": rid, "deleted": before},
            ))

    # ---- create-side mutations (option-A leeway, 2026-04-30) -------------------

    _ALLOWED_NEW_NODE_TYPES = frozenset({"State", "Event"})

    def handle_kg_create_state_node(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        """Create a new State (or Event) node + an edge from owner → state in
        one transaction. Both rows are referenced in the kg_revision_log entry
        so reverse is a clean two-row delete.

        Required: owner_node_id, predicate, label, reason.
        Optional: node_type ('State' default; 'Event' allowed), category,
        description, original_sentence, start_date, end_date,
        start_date_confidence, end_date_confidence, sentence (for the edge),
        attributes, finding_id, dry_run.
        """
        owner_id = str(arguments.get("owner_node_id") or "").strip()
        predicate = str(arguments.get("predicate") or "").strip()
        label = str(arguments.get("label") or "").strip()
        if not owner_id or not predicate or not label:
            raise ValueError("`owner_node_id`, `predicate`, `label` are all required.")
        node_type = str(arguments.get("node_type") or "State").strip()
        if node_type not in self._ALLOWED_NEW_NODE_TYPES:
            raise ValueError(
                f"node_type {node_type!r} not allowed for kg_create_state_node; "
                f"use one of {sorted(self._ALLOWED_NEW_NODE_TYPES)}."
            )
        reason = self._require_reason(arguments)
        dry_run = bool(arguments.get("dry_run", False))
        finding_id = arguments.get("finding_id") or None

        category = arguments.get("category")
        description = arguments.get("description")
        original_sentence = arguments.get("original_sentence")
        start_date = _parse_date(arguments.get("start_date"))
        end_date = _parse_date(arguments.get("end_date"))
        start_date_confidence = arguments.get("start_date_confidence")
        end_date_confidence = arguments.get("end_date_confidence")
        edge_sentence = arguments.get("sentence")
        attributes = arguments.get("attributes") or {}
        if not isinstance(attributes, dict):
            raise ValueError("`attributes` must be a dict.")

        with get_db_manager().transaction(op="kg_mutator.create_state_node") as session:
            owner = session.query(Node).filter(Node.id == owner_id).first()
            if owner is None:
                raise ValueError(f"owner_node_id {owner_id!r} not found")

            new_node_id = str(uuid.uuid4())
            new_edge_id = str(uuid.uuid4())

            # Compose the after-preview before any session.add so dry_run
            # gets the same shape as the real result.
            preview_node = {
                "id": new_node_id,
                "label": label,
                "node_type": node_type,
                "category": category,
                "description": description,
                "original_sentence": original_sentence,
                "start_date": start_date.isoformat() if start_date else None,
                "end_date": end_date.isoformat() if end_date else None,
                "start_date_confidence": start_date_confidence,
                "end_date_confidence": end_date_confidence,
                "attributes": attributes,
            }
            preview_edge = {
                "id": new_edge_id,
                "source_id": owner_id,
                "target_id": new_node_id,
                "relationship_type": predicate,
                "sentence": edge_sentence,
            }
            after = {"new_node": preview_node, "new_edge": preview_edge}
            before = "ABSENT"  # nothing existed before this op

            if dry_run:
                return self.publish_result(ToolResult(
                    result_type="kg_create_state_node",
                    content=(
                        f"DRY RUN: would create {node_type} {label!r} (id={new_node_id[:8]}) "
                        f"and edge {owner.label!r} -[{predicate}]-> {label!r} "
                        f"(edge_id={new_edge_id[:8]}). "
                        f"Dates: start={preview_node['start_date']}, end={preview_node['end_date']}."
                    ),
                    data={"ok": True, "dry_run": True, "before": before, "after": after},
                ))

            new_node = Node(
                id=new_node_id,
                label=label,
                node_type=node_type,
                category=category,
                description=description,
                original_sentence=original_sentence,
                start_date=start_date,
                end_date=end_date,
                start_date_confidence=start_date_confidence,
                end_date_confidence=end_date_confidence,
                attributes=attributes,
            )
            session.add(new_node)
            session.flush()  # node_id must exist before the edge FK fires

            new_edge = Edge(
                id=new_edge_id,
                source_id=owner_id,
                target_id=new_node_id,
                relationship_type=predicate,
                sentence=edge_sentence,
            )
            session.add(new_edge)
            session.flush()

            after["new_node"] = _node_snapshot(new_node)
            after["new_edge"] = _edge_snapshot(new_edge)

            rid = _write_revision_log(
                session=session,
                op="create_state_node",
                args={
                    "owner_node_id": owner_id,
                    "predicate": predicate,
                    "label": label,
                    "node_type": node_type,
                    "new_node_id": new_node_id,
                    "new_edge_id": new_edge_id,
                    "finding_id": finding_id,
                },
                before=before,
                after=after,
                reason=reason,
                finding_id=finding_id,
                agent_id=self._agent_id(tool_message),
            )

            return self.publish_result(ToolResult(
                result_type="kg_create_state_node",
                content=(
                    f"Created {node_type} {label!r} (id={new_node_id[:8]}) "
                    f"and edge {owner.label!r} -[{predicate}]-> {label!r} "
                    f"(edge_id={new_edge_id[:8]}). revision_log_id={rid}"
                ),
                data={
                    "ok": True,
                    "revision_log_id": rid,
                    "new_node_id": new_node_id,
                    "new_edge_id": new_edge_id,
                },
            ))

    def handle_kg_create_edge(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        """Create one edge between two existing nodes. Refuses if both
        endpoints aren't already in the KG, or if a same-predicate edge
        already exists between them.

        Required: source_id, target_id, relationship_type, reason.
        Optional: sentence, relationship_descriptor, finding_id, dry_run.
        """
        source_id = str(arguments.get("source_id") or "").strip()
        target_id = str(arguments.get("target_id") or "").strip()
        relationship_type = str(arguments.get("relationship_type") or "").strip()
        if not source_id or not target_id or not relationship_type:
            raise ValueError("`source_id`, `target_id`, `relationship_type` are all required.")
        if source_id == target_id:
            raise ValueError("source_id and target_id must differ (no self-loops).")
        reason = self._require_reason(arguments)
        dry_run = bool(arguments.get("dry_run", False))
        finding_id = arguments.get("finding_id") or None
        sentence = arguments.get("sentence")
        descriptor = arguments.get("relationship_descriptor")

        with get_db_manager().transaction(op="kg_mutator.create_edge") as session:
            src = session.query(Node).filter(Node.id == source_id).first()
            tgt = session.query(Node).filter(Node.id == target_id).first()
            if src is None:
                raise ValueError(f"source_id {source_id!r} not found")
            if tgt is None:
                raise ValueError(f"target_id {target_id!r} not found")

            existing = (
                session.query(Edge)
                .filter(
                    Edge.source_id == source_id,
                    Edge.target_id == target_id,
                    Edge.relationship_type == relationship_type,
                )
                .first()
            )
            if existing is not None:
                raise ValueError(
                    f"Refusing to create edge: {src.label!r} -[{relationship_type}]-> {tgt.label!r} "
                    f"already exists (id={existing.id}). Use kg_update_node_field on attributes "
                    "or kg_delete_edge first if you mean to replace."
                )

            new_edge_id = str(uuid.uuid4())
            preview = {
                "id": new_edge_id,
                "source_id": source_id,
                "target_id": target_id,
                "relationship_type": relationship_type,
                "sentence": sentence,
                "relationship_descriptor": descriptor,
            }
            before = "ABSENT"

            if dry_run:
                return self.publish_result(ToolResult(
                    result_type="kg_create_edge",
                    content=(
                        f"DRY RUN: would create edge {src.label!r} -[{relationship_type}]-> {tgt.label!r} "
                        f"(id={new_edge_id[:8]})."
                    ),
                    data={"ok": True, "dry_run": True, "before": before, "after": preview},
                ))

            new_edge = Edge(
                id=new_edge_id,
                source_id=source_id,
                target_id=target_id,
                relationship_type=relationship_type,
                sentence=sentence,
                relationship_descriptor=descriptor,
            )
            session.add(new_edge)
            session.flush()
            after = _edge_snapshot(new_edge)

            rid = _write_revision_log(
                session=session,
                op="create_edge",
                args={
                    "source_id": source_id,
                    "target_id": target_id,
                    "relationship_type": relationship_type,
                    "new_edge_id": new_edge_id,
                    "finding_id": finding_id,
                },
                before=before,
                after=after,
                reason=reason,
                finding_id=finding_id,
                agent_id=self._agent_id(tool_message),
            )
            return self.publish_result(ToolResult(
                result_type="kg_create_edge",
                content=(
                    f"Created edge {src.label!r} -[{relationship_type}]-> {tgt.label!r} "
                    f"(id={new_edge_id[:8]}). revision_log_id={rid}"
                ),
                data={"ok": True, "revision_log_id": rid, "new_edge_id": new_edge_id},
            ))

    def handle_kg_close_state(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        """Set ``end_date`` (and optional ``end_date_confidence`` /
        ``end_date_prose``) on an existing State or Event node. Use this
        when a residence / job / membership era ends.

        Refuses if the node already has an end_date unless ``force=True``.

        Required: node_id, end_date, reason.
        Optional: end_date_confidence (default 'operator_close'),
        end_date_prose, force, finding_id, dry_run.
        """
        node_id = str(arguments.get("node_id") or "").strip()
        if not node_id:
            raise ValueError("`node_id` is required.")
        end_date = _parse_date(arguments.get("end_date"))
        if end_date is None:
            raise ValueError("`end_date` is required and must be a parseable ISO date.")
        reason = self._require_reason(arguments)
        dry_run = bool(arguments.get("dry_run", False))
        force = bool(arguments.get("force", False))
        finding_id = arguments.get("finding_id") or None
        end_date_confidence = arguments.get("end_date_confidence") or "operator_close"
        end_date_prose = arguments.get("end_date_prose")

        with get_db_manager().transaction(op="kg_mutator.close_state") as session:
            node = session.query(Node).filter(Node.id == node_id).first()
            if node is None:
                raise ValueError(f"node {node_id!r} not found")
            if (node.node_type or "") not in self._ALLOWED_NEW_NODE_TYPES:
                raise ValueError(
                    f"node_type={node.node_type!r} is not closeable; "
                    f"close_state expects one of {sorted(self._ALLOWED_NEW_NODE_TYPES)}."
                )
            if node.end_date is not None and not force:
                raise ValueError(
                    f"node {node_id!r} already has end_date={node.end_date.isoformat()}. "
                    "Pass force=true to override (kg_revision_log will record the prior value)."
                )

            before = _node_snapshot(node)

            if dry_run:
                preview = {**before,
                           "end_date": end_date.isoformat(),
                           "end_date_confidence": end_date_confidence,
                           "end_date_prose": end_date_prose if end_date_prose is not None else before.get("end_date_prose")}
                return self.publish_result(ToolResult(
                    result_type="kg_close_state",
                    content=(
                        f"DRY RUN: would set end_date={preview['end_date']} "
                        f"on {node.node_type} {node.label!r} ({node_id[:8]})."
                    ),
                    data={"ok": True, "dry_run": True, "before": before, "after": preview},
                ))

            node.end_date = end_date
            node.end_date_confidence = end_date_confidence
            if end_date_prose is not None:
                node.end_date_prose = end_date_prose
            after = _node_snapshot(node)

            rid = _write_revision_log(
                session=session,
                op="close_state",
                args={
                    "node_id": node_id,
                    "end_date": end_date.isoformat(),
                    "end_date_confidence": end_date_confidence,
                    "force": force,
                    "finding_id": finding_id,
                },
                before=before,
                after=after,
                reason=reason,
                finding_id=finding_id,
                agent_id=self._agent_id(tool_message),
            )
            return self.publish_result(ToolResult(
                result_type="kg_close_state",
                content=(
                    f"Closed {node.node_type} {node.label!r} ({node_id[:8]}) at "
                    f"end_date={end_date.isoformat()}. revision_log_id={rid}"
                ),
                data={"ok": True, "revision_log_id": rid, "node_id": node_id},
            ))

    def handle_kg_finding_resolve(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        return self._finding_status_change(
            arguments, tool_message,
            new_status="executed",
            op="finding_resolve",
            note_field="execution_notes",
            extra_fields=("action",),
        )

    def handle_kg_finding_escalate(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        return self._finding_status_change(
            arguments, tool_message,
            new_status="approved",  # 'approved' = queued for human review per existing vocab
            op="finding_escalate",
            note_field="execution_notes",
            extra_fields=("summary", "suggested_action"),
        )

    def _finding_status_change(
        self,
        arguments: Dict[str, Any],
        tool_message: ToolMessage,
        *,
        new_status: str,
        op: str,
        note_field: str,
        extra_fields: Iterable[str],
    ) -> ToolResult:
        finding_id = str(arguments.get("finding_id") or "").strip()
        if not finding_id:
            raise ValueError("`finding_id` is required.")
        reason = self._require_reason(arguments)
        notes = str(arguments.get("notes") or "").strip()
        # Compose execution_notes from notes + any extra context fields.
        extras = []
        for f in extra_fields:
            v = arguments.get(f)
            if v not in (None, ""):
                extras.append(f"{f}={v}")
        composed_notes = " ".join([notes] + extras).strip() or reason

        with get_db_manager().transaction(op=f"kg_mutator.{op}") as session:
            f = session.query(KGMaintenanceFinding).filter(KGMaintenanceFinding.id == finding_id).first()
            if f is None:
                raise ValueError(f"finding {finding_id!r} not found")
            before = {"status": f.status, "execution_notes": f.execution_notes}
            f.status = new_status
            setattr(f, note_field, composed_notes[:2000])
            f.executed_by = self._agent_id(tool_message) or f.executed_by
            f.executed_at = datetime.now(timezone.utc)
            after = {"status": f.status, "execution_notes": getattr(f, note_field)}

            rid = _write_revision_log(
                session=session,
                op=op,
                args={"finding_id": finding_id, **{k: arguments.get(k) for k in extra_fields}},
                before=before,
                after=after,
                reason=reason,
                finding_id=finding_id,
                agent_id=self._agent_id(tool_message),
            )

        verb = "Resolved" if op == "finding_resolve" else "Escalated"
        return self.publish_result(ToolResult(
            result_type=op,
            content=f"{verb} finding {finding_id[:8]} → status={new_status}. revision_log_id={rid}",
            data={"ok": True, "revision_log_id": rid, "new_status": new_status},
        ))
