"""Identity sentences — graph-derived definite descriptions.

A node's identity sentence is the minimal description that uniquely picks
out its referent in THIS graph: "Tom and Sue's house in Springfield",
"The school the user's daughter attends (since fall 2024)". It is a
PROJECTION:
deterministic code gathers the discriminating inputs (label + anchor
edges + era dates), an LLM phrases them, and a stable hash of the inputs
detects staleness so the sentence regenerates only when the structure it
derives from changes. It is NOT original_sentence — that is the first
observation, an accident of arrival order ("the house only has one Nest
thermostat" was a real one).

Spec: scratch/IDENTITY_SENTENCE_SPEC.md. Covers Entity/Event/State/Goal/
Concept (Property resolves subject-scoped and is excluded).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

MAX_EDGES = 12
MAX_SENTENCE_CHARS = 220
AGENT_NAME = "kg_maintenance::identity_sentence_writer"


def gather_identity_inputs(session, node) -> Dict[str, Any]:
    """Deterministic input assembly: the facts the sentence derives from.

    Edges ordered by importance (the discriminating anchors — possessor,
    location, membership — tend to be the important ones), capped, both
    directions, pod endpoints skipped.
    """
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
    from app.assistant.kg_core.kg_utils.date_display import format_node_date

    edge_lines: List[str] = []
    edge_keys: List[tuple] = []

    out_edges = (
        session.query(Edge, Node)
        .outerjoin(Node, Edge.target_id == Node.id)
        .filter(Edge.source_id == node.id)
        .order_by(Edge.importance.desc().nulls_last())
        .limit(MAX_EDGES).all()
    )
    in_edges = (
        session.query(Edge, Node)
        .outerjoin(Node, Edge.source_id == Node.id)
        .filter(Edge.target_id == node.id)
        .order_by(Edge.importance.desc().nulls_last())
        .limit(MAX_EDGES).all()
    )
    for e, other in out_edges:
        if other is None:
            continue
        other_dates = format_node_date(other.start_date, other.start_date_confidence)
        edge_lines.append(
            f"{node.label} --{e.relationship_type}--> {other.label} [{other.node_type}]"
            + (f" (since {other_dates})" if other_dates else "")
        )
        edge_keys.append(("out", e.relationship_type, (other.label or "").lower()))
    for e, other in in_edges:
        if other is None:
            continue
        edge_lines.append(
            f"{other.label} [{other.node_type}] --{e.relationship_type}--> {node.label}"
        )
        edge_keys.append(("in", e.relationship_type, (other.label or "").lower()))

    start_s = format_node_date(node.start_date, node.start_date_confidence)
    end_s = format_node_date(node.end_date, node.end_date_confidence)

    return {
        "label": node.label,
        "node_type": node.node_type,
        "category": node.category,
        "start_date": start_s,
        "end_date": end_s,
        "edges": edge_lines[: 2 * MAX_EDGES],
        "original_sentence": (node.original_sentence or "")[:250],
        "description": (node.description or "")[:300],
        # hash inputs ride along so the hash and the prompt can't diverge
        "_edge_keys": sorted(edge_keys),
    }


def identity_inputs_hash(inputs: Dict[str, Any]) -> str:
    """Stable hash of the DISCRIMINATING inputs only: label, era dates,
    edge structure. Deliberately excludes description/original_sentence —
    prose churn must not regenerate identity; structure changes must."""
    basis = json.dumps(
        {
            "label": (inputs.get("label") or "").lower(),
            "start": inputs.get("start_date"),
            "end": inputs.get("end_date"),
            "edges": inputs.get("_edge_keys") or [],
        },
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def generate_identity_sentence(inputs: Dict[str, Any]) -> Optional[str]:
    """LLM phrases the gathered facts into one definite description.

    Deterministic proposes (the facts), LLM decides (selection + phrasing).
    Returns None when the writer produces nothing usable.
    """
    from app.assistant.routine_handlers.subconscious import _run_subconscious_agent

    if not (inputs.get("label") or "").strip():
        raise ValueError("identity inputs missing label — refusing to invoke writer")

    agent_input = {k: v for k, v in inputs.items() if not k.startswith("_")}
    result = _run_subconscious_agent(
        handler_label="identity_sentence_writer",
        agent_name=AGENT_NAME,
        context=agent_input,
        scope_id=AGENT_NAME,
        actor_id="identity_sentence_refresh",
    )
    if not isinstance(result, dict):
        return None
    sentence = str(result.get("identity_sentence") or "").strip()
    if not sentence:
        return None
    return sentence[:MAX_SENTENCE_CHARS]


COVERED_NODE_TYPES = ("Entity", "Event", "State", "Goal", "Concept")
# Property excluded: resolves subject-scoped, never by label/similarity.
# Disambiguation excluded: meta-graph markers, no referent of their own.


def run_identity_sentence_refresh(
    *, max_per_run: int = 50, node_types: tuple = COVERED_NODE_TYPES,
) -> Dict[str, Any]:
    """Nightly projection pass: (re)generate identity sentences for nodes
    that have none or whose discriminating inputs drifted.

    NULL-sentence nodes first (backfill), then hash-stale, both ordered by
    pagerank so important nodes get sentences earliest. Writes preserve
    updated_at verbatim (a projection is not a content change — bumping
    would cascade card/wiki refreshes). Embeds into the
    node_identity_embeddings chroma collection after the DB write.

    node_types narrows a run to specific types (one-time per-type
    backfills); the nightly default covers everything but Property and
    Disambiguation.
    """
    from sqlalchemy import update as sql_update

    from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
    from app.assistant.embeddings.embedder import embed_text
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
    from app.assistant.kg.disambiguation import DISAMBIGUATION_NODE_TYPE
    from app.models.db_manager import get_db_manager

    examined = 0
    generated = 0
    failed = 0
    skipped_fresh = 0

    with get_db_manager().read_session() as session:
        nodes = (
            session.query(Node)
            .filter(Node.node_type.in_(list(node_types)))
            .filter(Node.node_type != DISAMBIGUATION_NODE_TYPE)
            .order_by(
                Node.identity_sentence.isnot(None),  # NULLs (False) first
                Node.pagerank_score.desc().nulls_last(),
            )
            .all()
        )
        # Snapshot work items inside the session; LLM calls happen after.
        work: List[Dict[str, Any]] = []
        for n in nodes:
            if len(work) >= max_per_run:
                break
            examined += 1
            inputs = gather_identity_inputs(session, n)
            h = identity_inputs_hash(inputs)
            if n.identity_sentence and n.identity_inputs_hash == h:
                skipped_fresh += 1
                continue
            work.append({"node_id": n.id, "inputs": inputs, "hash": h})

    # The user-self node is the graph's center — its identity is trivially
    # true and needs no derivation (the LLM otherwise writes an awkward
    # relational description of the one person everything anchors TO).
    try:
        from app.assistant.utils.identity_names import get_required_primary_user_name
        primary_user = get_required_primary_user_name().strip().lower()
    except Exception:
        primary_user = None

    cm = None
    for item in work:
        sentence = None
        label = str(item["inputs"].get("label") or "")
        if primary_user and label.strip().lower() == primary_user:
            sentence = f"{label}, the primary user of this system."
        else:
            try:
                sentence = generate_identity_sentence(item["inputs"])
            except Exception as e:
                logger.error("[identity_refresh] writer failed for %r: %s",
                             label, e)
        if not sentence:
            failed += 1
            continue

        with get_db_manager().transaction(op="identity_sentence_refresh") as s:
            from app.assistant.kg.db.knowledge_graph_db_sqlite import Node as N
            s.execute(
                sql_update(N)
                .where(N.id == item["node_id"])
                .values(
                    identity_sentence=sentence,
                    identity_inputs_hash=item["hash"],
                    updated_at=N.updated_at,  # projection: preserve verbatim
                )
            )
        try:
            if cm is None:
                cm = get_chroma_manager()
            cm.store_node_identity_embedding(
                item["node_id"], sentence, embed_text(sentence),
            )
        except Exception as e:
            logger.error("[identity_refresh] chroma embed failed for %s: %s "
                         "(nightly rebuild heals)", item["node_id"][:8], e)
        generated += 1
        logger.info("[identity_refresh] %r -> %s",
                    item["inputs"].get("label"), sentence[:90])

    return {
        "examined": examined,
        "generated": generated,
        "failed": failed,
        "skipped_fresh": skipped_fresh,
        "queued": len(work),
    }
