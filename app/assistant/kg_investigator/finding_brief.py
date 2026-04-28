"""Convert a kg_maintenance_finding row into an investigator brief.

Produces the (task, information) pair that gets handed to
``kg_investigation_manager``. Pre-fetches the obvious context so the
investigator can spend its query budget on real digging instead of
re-discovering what tables already say.

Branches per finding_type:
- wiki_contradiction: quoted prose + paragraph + ground truth + entity neighborhood
- duplicate_node: both nodes side-by-side
- orphan_node / duplicate_edge / missing_description / type_error: minimal brief
- unknown / legacy types (e.g., the historical suspect_node): falls through to
  a generic "investigate and recommend a fix" brief.

The brief stays as text, not structured fields, so the investigator's
planner can read it as a normal task description.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text as sql_text

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.utils.logging_config import get_logger
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)


# ----- helpers --------------------------------------------------------------


def _fetch_node(session, node_id: str) -> Optional[Dict[str, Any]]:
    if not node_id:
        return None
    row = session.execute(
        sql_text(
            "SELECT id, label, node_type, category, aliases, description, "
            "       original_sentence, start_date, end_date, importance, "
            "       pagerank_score, source, created_at "
            "FROM kg_node_metadata WHERE id = :nid"
        ),
        {"nid": node_id},
    ).fetchone()
    if row is None:
        return None
    return dict(row._mapping) if hasattr(row, "_mapping") else dict(zip(
        ["id", "label", "node_type", "category", "aliases", "description",
         "original_sentence", "start_date", "end_date", "importance",
         "pagerank_score", "source", "created_at"], row,
    ))


def _fetch_neighborhood(session, node_id: str, *, limit: int = 30) -> Dict[str, List[Dict[str, Any]]]:
    """Return (out_edges, in_edges). Each edge dict: relationship_type, sentence, other_id, other_label, other_type."""
    if not node_id:
        return {"out": [], "in": []}
    out_rows = session.execute(
        sql_text(
            "SELECT e.relationship_type, e.sentence, "
            "       t.id AS other_id, t.label AS other_label, t.node_type AS other_type "
            "FROM kg_edge_metadata e "
            "JOIN kg_node_metadata t ON t.id = e.target_id "
            "WHERE e.source_id = :nid "
            "ORDER BY e.created_at DESC LIMIT :lim"
        ),
        {"nid": node_id, "lim": limit},
    ).fetchall()
    in_rows = session.execute(
        sql_text(
            "SELECT e.relationship_type, e.sentence, "
            "       s.id AS other_id, s.label AS other_label, s.node_type AS other_type "
            "FROM kg_edge_metadata e "
            "JOIN kg_node_metadata s ON s.id = e.source_id "
            "WHERE e.target_id = :nid "
            "ORDER BY e.created_at DESC LIMIT :lim"
        ),
        {"nid": node_id, "lim": limit},
    ).fetchall()

    def _to_dict(r):
        return {
            "relationship_type": r[0],
            "sentence": r[1],
            "other_id": r[2],
            "other_label": r[3],
            "other_type": r[4],
        }
    return {"out": [_to_dict(r) for r in out_rows], "in": [_to_dict(r) for r in in_rows]}


def _format_node_block(label: str, node: Dict[str, Any]) -> str:
    if not node:
        return f"### {label}\n(node not found)\n"
    lines = [f"### {label}"]
    lines.append(f"- id: `{node.get('id')}`")
    lines.append(f"- label: {node.get('label')!r}")
    lines.append(f"- node_type: {node.get('node_type')}")
    lines.append(f"- category: {node.get('category')}")
    aliases = node.get("aliases")
    if aliases:
        lines.append(f"- aliases: {aliases}")
    if node.get("start_date") or node.get("end_date"):
        lines.append(
            f"- validity window: start={node.get('start_date')} end={node.get('end_date')}"
        )
    if node.get("description"):
        desc = str(node["description"])
        lines.append(f"- description: {desc[:300]}{'…' if len(desc) > 300 else ''}")
    if node.get("original_sentence"):
        sent = str(node["original_sentence"])
        lines.append(f"- original_sentence (present-tense canonical): {sent[:300]}{'…' if len(sent) > 300 else ''}")
    return "\n".join(lines) + "\n"


def _format_neighborhood_block(neigh: Dict[str, List[Dict[str, Any]]], *, max_lines: int = 20) -> str:
    out_edges = neigh.get("out", [])[:max_lines]
    in_edges = neigh.get("in", [])[:max_lines]
    lines = []
    if out_edges:
        lines.append("Outgoing edges:")
        for e in out_edges:
            sent = (e.get("sentence") or "")[:140]
            lines.append(
                f"  -[{e.get('relationship_type')}]-> `{e.get('other_id')}` "
                f"({e.get('other_type')}) {e.get('other_label')!r}  | {sent}"
            )
    if in_edges:
        lines.append("Incoming edges:")
        for e in in_edges:
            sent = (e.get("sentence") or "")[:140]
            lines.append(
                f"  `{e.get('other_id')}` ({e.get('other_type')}) {e.get('other_label')!r} "
                f"-[{e.get('relationship_type')}]->  | {sent}"
            )
    if not lines:
        lines.append("(no edges)")
    return "\n".join(lines) + "\n"


# ----- per-finding-type brief builders --------------------------------------


def _brief_wiki_contradiction(session, finding: KGMaintenanceFinding) -> Tuple[str, str]:
    ev = finding.evidence_json or {}
    entity_label = ev.get("entity_label") or "?"
    issue_type = ev.get("issue_type") or "?"
    quoted = (ev.get("quoted_text") or "").strip()
    paragraph = (ev.get("wiki_paragraph") or "").strip()
    section = (ev.get("wiki_section_heading") or "").strip()
    line_no = ev.get("wiki_line_number")
    prose_path = (ev.get("prose_path") or "").strip()
    source_kind = (ev.get("source_kind") or "").strip()
    source_statement = (ev.get("source_statement") or "").strip()

    primary = _fetch_node(session, finding.primary_node_id)
    neigh = _fetch_neighborhood(session, finding.primary_node_id)

    parts: List[str] = []
    parts.append(f"## Finding")
    parts.append(f"- finding_id: `{finding.id}`")
    parts.append(f"- finding_type: wiki_contradiction")
    parts.append(f"- issue_type (per critic): {issue_type}")
    parts.append(f"- priority: {finding.priority}")
    parts.append(f"- critic confidence: {finding.confidence}")
    parts.append(f"- summary: {finding.reason}")
    parts.append("")

    parts.append(f"## Wiki location")
    parts.append(f"- subject: {entity_label}")
    parts.append(f"- prose path: {prose_path}")
    if section:
        parts.append(f"- section: `{section}`")
    if line_no:
        parts.append(f"- line: {line_no}")
    parts.append("")

    parts.append(f"## Quoted contradicting text")
    parts.append(f"```\n{quoted}\n```")
    if paragraph and paragraph != quoted:
        parts.append("")
        parts.append("Paragraph context:")
        parts.append(f"```\n{paragraph}\n```")
    parts.append("")

    if source_kind and source_kind != "internal" and source_statement:
        parts.append(f"## Authoritative source the prose disputes")
        parts.append(f"- source_kind: {source_kind}")
        parts.append(f"- source_statement: {source_statement!r}")
        parts.append("")
    elif source_kind == "internal":
        parts.append(f"## Source")
        parts.append(f"Internal contradiction (the prose contradicts itself).")
        parts.append("")

    parts.append(f"## Subject node (primary)")
    parts.append(_format_node_block(entity_label, primary))
    parts.append("Neighborhood:")
    parts.append(_format_neighborhood_block(neigh))

    task = (
        f"Investigate wiki contradiction on the page for {entity_label!r}. "
        f"Decide whether the root cause is: a duplicate node, a typo / spelling variant, "
        f"a tense or canonical-sentence error on a State/Event/Goal node, "
        f"a wrong start_date/end_date validity window, or a one-off bad fact. "
        f"Recommend a concrete fix via proposed_action."
    )
    return task, "\n".join(parts)


def _brief_node_pair(
    session,
    finding: KGMaintenanceFinding,
    *,
    finding_label: str,
    task_phrase: str,
) -> Tuple[str, str]:
    primary = _fetch_node(session, finding.primary_node_id)
    secondary = _fetch_node(session, finding.secondary_node_id) if finding.secondary_node_id else None
    primary_neigh = _fetch_neighborhood(session, finding.primary_node_id)
    secondary_neigh = (
        _fetch_neighborhood(session, finding.secondary_node_id)
        if finding.secondary_node_id
        else {"out": [], "in": []}
    )

    parts: List[str] = [
        f"## Finding",
        f"- finding_id: `{finding.id}`",
        f"- finding_type: {finding_label}",
        f"- priority: {finding.priority}",
        f"- agent confidence: {finding.confidence}",
        f"- agent reason: {finding.reason}",
        "",
        f"## Primary node",
        _format_node_block("primary", primary),
        "Neighborhood:",
        _format_neighborhood_block(primary_neigh),
    ]
    if secondary is not None:
        parts.extend([
            "",
            f"## Secondary node",
            _format_node_block("secondary", secondary),
            "Neighborhood:",
            _format_neighborhood_block(secondary_neigh),
        ])
    return task_phrase, "\n".join(parts)


def _brief_single_node(
    session,
    finding: KGMaintenanceFinding,
    *,
    finding_label: str,
    task_phrase: str,
) -> Tuple[str, str]:
    primary = _fetch_node(session, finding.primary_node_id)
    neigh = _fetch_neighborhood(session, finding.primary_node_id)
    ev = finding.evidence_json or {}

    parts: List[str] = [
        f"## Finding",
        f"- finding_id: `{finding.id}`",
        f"- finding_type: {finding_label}",
        f"- priority: {finding.priority}",
        f"- agent confidence: {finding.confidence}",
        f"- agent reason: {finding.reason}",
        "",
    ]
    if ev:
        parts.append("## Evidence from the finder")
        for k, v in ev.items():
            if v in (None, "", []):
                continue
            sval = str(v)
            parts.append(f"- {k}: {sval[:300]}{'…' if len(sval) > 300 else ''}")
        parts.append("")
    parts.append("## Subject node")
    parts.append(_format_node_block(ev.get("entity_label") or finding.primary_node_id[:8], primary))
    parts.append("Neighborhood:")
    parts.append(_format_neighborhood_block(neigh))
    return task_phrase, "\n".join(parts)


# ----- public entry point ---------------------------------------------------


def build_finding_brief(finding_id: str) -> Optional[Tuple[str, str]]:
    """Return ``(task, information)`` for the investigator, or ``None`` if the
    finding doesn't exist."""
    with get_db_manager().read_session() as session:
        finding = (
            session.query(KGMaintenanceFinding)
            .filter(KGMaintenanceFinding.id == finding_id)
            .first()
        )
        if finding is None:
            logger.warning("build_finding_brief: finding %s not found", finding_id)
            return None

        ftype = (finding.finding_type or "").strip().lower()

        if ftype == "wiki_contradiction":
            return _brief_wiki_contradiction(session, finding)
        if ftype == "duplicate_node":
            return _brief_node_pair(
                session, finding,
                finding_label="duplicate_node",
                task_phrase=(
                    "Investigate whether these two nodes refer to the same real-world thing. "
                    "Look for: alias overlap, neighborhood overlap (shared participants), "
                    "compatible or contradictory dates, label spelling variants. "
                    "Recommend merge_nodes (with canonical winner) or no_action via proposed_action."
                ),
            )
        if ftype == "duplicate_edge":
            return _brief_node_pair(
                session, finding,
                finding_label="duplicate_edge",
                task_phrase=(
                    "Investigate whether multiple edges between these nodes are duplicates of one "
                    "underlying claim, or genuinely distinct events / observations. "
                    "Recommend delete_edge (specifying which) or no_action."
                ),
            )
        if ftype == "orphan_node":
            return _brief_single_node(
                session, finding,
                finding_label="orphan_node",
                task_phrase=(
                    "Investigate this orphan (zero edges) node. Decide whether it is a stranded "
                    "remnant safe to delete, or whether it should be linked to existing nodes "
                    "(recommend escalate_user with a clear suggestion in args)."
                ),
            )
        if ftype == "missing_description":
            return _brief_single_node(
                session, finding,
                finding_label="missing_description",
                task_phrase=(
                    "Investigate why this node has no description and propose update_node_field "
                    "with a one-paragraph description grounded in the node's edges + evidence. "
                    "If the node is itself dubious, recommend escalate_user instead."
                ),
            )
        if ftype == "type_error":
            return _brief_single_node(
                session, finding,
                finding_label="type_error",
                task_phrase=(
                    "Investigate this type-error finding. Compare node_type vs the actual content "
                    "(label, original_sentence, neighborhood) and recommend update_node_field "
                    "with the correct node_type, or no_action if the finder was wrong."
                ),
            )

        # Unknown type: minimal brief.
        return _brief_single_node(
            session, finding,
            finding_label=ftype or "unknown",
            task_phrase=(
                "Investigate this finding. Determine what (if anything) is wrong and recommend a "
                "concrete fix via proposed_action."
            ),
        )
