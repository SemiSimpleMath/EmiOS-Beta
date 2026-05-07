"""
Step: wiki_inference

Runs `wiki_connection_investigator` over recently-updated wiki pages.
For each candidate subject:
  1. Read the rendered prose page.
  2. Pull the subject's KG neighborhood + a name-only list of known
     entities (so the agent can resolve targets to existing ids without
     needing to embed full descriptions for every node).
  3. Call the agent — it returns a list of InferredConnection proposals.
  4. Pipe each accepted connection through the standard claim_proposal
     pipeline via proposal_writer.write_one_proposal_group. The promoter
     applies the same gates as for chat-extracted facts (dedup,
     time-frame, hub-overlap, LLM merger).

Conservative defaults:
  - LLM is mini, not nano (the inference chain demands real reasoning).
  - Only proposals with `confidence >= MIN_CONFIDENCE` get written.
  - Only proposals with `not_already_in_kg=True` get written (the agent's
    own self-check; we trust it but ALSO verify by SQL before writing).
  - Per-subject cap on connections to keep one bad page from flooding
    the proposal queue.
  - Per-run cap on subjects examined (LLM cost bound).

Idempotency: a `wiki_inference_run` row records which subjects were
examined when, so repeated runs skip subjects whose page hasn't changed
since their last examination.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.models.base import get_session

logger = get_logger(__name__)


MIN_CONFIDENCE = 0.6
PER_SUBJECT_CONNECTION_CAP = 5
DEFAULT_SUBJECT_LIMIT = 10


def run(ctx: PipelineContext, *, subject_limit: Optional[int] = None) -> dict:
    """Returns counts: {"subjects_examined": int, "proposals_written": int,
                         "proposals_rejected_low_confidence": int,
                         "proposals_rejected_already_in_kg": int}."""
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node

    subject_limit = int(subject_limit or DEFAULT_SUBJECT_LIMIT)

    vault_path = _resolve_vault_path()
    if vault_path is None:
        logger.info("[wiki_inference] no vault path; nothing to do")
        return _empty_result()

    prose_dir = vault_path / "prose"
    if not prose_dir.exists():
        logger.info("[wiki_inference] no prose dir at %s; nothing to do", prose_dir)
        return _empty_result()

    subjects = _pick_subjects_to_examine(prose_dir, limit=subject_limit)
    if not subjects:
        logger.info("[wiki_inference] no subjects to examine")
        return _empty_result()

    agent = DI.agent_factory.create_agent("wiki_connection_investigator")
    if agent is None:
        raise RuntimeError("Failed to create wiki_connection_investigator agent")

    # Pre-fetch known entities once (~1k labels) so each LLM call gets a
    # fixed-size names list rather than streaming the entire KG. This is
    # the agent's resolution lookup — labels + ids only.
    known_entities = _fetch_known_entities()

    subjects_examined = 0
    written = 0
    skipped_low_conf = 0
    skipped_already_in_kg = 0

    for subject in subjects:
        subjects_examined += 1
        page_path = prose_dir / f"{_safe_filename(subject['label'])}.md"
        try:
            page_text = _read_prose_page(prose_dir, subject["label"])
            if not page_text or len(page_text) < 200:
                # Even when we skip, mark the page examined so we don't
                # keep re-touching tiny pages every night.
                _write_examined_sidecar(page_path)
                continue
            neighborhood = _format_subject_neighborhood(subject["id"])
            agent_input = {
                "subject_label": subject["label"],
                "subject_neighborhood": neighborhood,
                "wiki_page_text": page_text,
                "known_entities": _format_known_entities(
                    known_entities, exclude_id=subject["id"]
                ),
            }
            result = agent.action_handler(Message(agent_input=agent_input))
            data = getattr(result, "data", None)
            if not isinstance(data, dict):
                logger.warning(
                    "[wiki_inference] non-dict from agent for %s: %s",
                    subject["label"], type(data).__name__,
                )
                continue
            connections = list(data.get("connections") or [])

            for c in connections[:PER_SUBJECT_CONNECTION_CAP]:
                if not isinstance(c, dict):
                    continue
                if float(c.get("confidence") or 0.0) < MIN_CONFIDENCE:
                    skipped_low_conf += 1
                    continue
                if not c.get("not_already_in_kg"):
                    skipped_already_in_kg += 1
                    continue

                # Verify-not-already-in-KG: the agent's flag is trust-but-verify.
                # If a same-predicate edge already exists, skip even if the
                # agent thought it didn't.
                if _edge_exists(
                    subject_id=subject["id"],
                    target_id=c.get("target_node_id"),
                    target_label=c.get("target_label"),
                    predicate=c.get("predicate"),
                ):
                    skipped_already_in_kg += 1
                    continue

                ok = _write_proposal_for_inferred_connection(
                    subject=subject,
                    connection=c,
                    pipeline_run_id=ctx.run_id,
                )
                if ok:
                    written += 1

            # Successful examination — record sidecar so we don't re-examine
            # this page until it gets rewritten by the wiki refresh.
            _write_examined_sidecar(page_path)

        except Exception:
            logger.error(
                "[wiki_inference] subject %s crashed (continuing)",
                subject.get("label"), exc_info=True,
            )

    logger.info(
        "[wiki_inference] subjects=%d proposals_written=%d skipped_low_conf=%d skipped_already=%d",
        subjects_examined, written, skipped_low_conf, skipped_already_in_kg,
    )
    return {
        "subjects_examined": subjects_examined,
        "proposals_written": written,
        "proposals_rejected_low_confidence": skipped_low_conf,
        "proposals_rejected_already_in_kg": skipped_already_in_kg,
    }


def _empty_result() -> dict:
    return {
        "subjects_examined": 0,
        "proposals_written": 0,
        "proposals_rejected_low_confidence": 0,
        "proposals_rejected_already_in_kg": 0,
    }


# ── Vault / page resolution ────────────────────────────────────────────


def _resolve_vault_path() -> Optional[Path]:
    """Mirror of routes.wiki_viewer._wiki_vault_root() but importable from
    pipeline code without bringing in the Flask blueprint."""
    import os

    override = os.environ.get("EMI_WIKI_DIR")
    if override:
        return Path(override)
    # Fall back to the assistant_name-derived default.
    try:
        from app.assistant.utils.config_utils import get_assistant_name
        name = get_assistant_name() or "Emi"
    except Exception:
        name = "Emi"
    return Path.home() / f"{name}Wiki"


def _safe_filename(label: str) -> str:
    """Mirror the wiki page writer's filesystem-safe label transform."""
    return label.replace("/", "_").replace("\\", "_")


def _write_examined_sidecar(page_path: Path) -> None:
    """Mark a page as examined so the next run skips it until the page
    is rewritten (page mtime > sidecar.examined_at_epoch)."""
    sidecar = page_path.with_suffix(".wiki_inference.json")
    try:
        sidecar.write_text(
            json.dumps({
                "examined_at_epoch": page_path.stat().st_mtime,
                "examined_at_iso": datetime.now(timezone.utc).isoformat(),
            }),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("[wiki_inference] sidecar write failed for %s: %s", page_path, e)


def _read_prose_page(prose_dir: Path, label: str) -> Optional[str]:
    """Read the markdown for an entity, or None if the file doesn't exist."""
    p = prose_dir / f"{_safe_filename(label)}.md"
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning("[wiki_inference] read failed for %s: %s", p, e)
        return None


def _pick_subjects_to_examine(prose_dir: Path, *, limit: int) -> List[Dict[str, Any]]:
    """Pick the N subjects whose prose page is freshest (newest mtime)
    AND who haven't been examined since their last page write. The "haven't
    been examined" check is encoded as a JSON sidecar at
    `<entity>.wiki_inference.json` so we don't need a new DB table.
    """
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node

    # Gather candidate (label, mtime) from prose files.
    candidates: List[tuple[str, float]] = []
    try:
        for p in prose_dir.iterdir():
            if not p.is_file() or p.suffix != ".md":
                continue
            label = p.stem
            sidecar = p.with_suffix(".wiki_inference.json")
            page_mtime = p.stat().st_mtime
            if sidecar.exists():
                try:
                    last = json.loads(sidecar.read_text(encoding="utf-8")).get("examined_at_epoch", 0)
                    if last >= page_mtime:
                        # Already examined since last page write
                        continue
                except Exception:
                    pass
            candidates.append((label, page_mtime))
    except Exception as e:
        logger.warning("[wiki_inference] prose dir scan failed: %s", e)
        return []

    candidates.sort(key=lambda t: t[1], reverse=True)
    candidates = candidates[: limit * 3]  # over-fetch, then filter to known KG entities

    # Resolve to KG node ids by label.
    if not candidates:
        return []
    labels = [c[0] for c in candidates]
    session = get_session()
    try:
        rows = (
            session.query(Node.id, Node.label, Node.node_type)
            .filter(Node.label.in_(labels))
            .all()
        )
        by_label = {r.label: {"id": str(r.id), "label": r.label, "node_type": r.node_type or ""} for r in rows}
    finally:
        session.close()

    out: List[Dict[str, Any]] = []
    for label, mtime in candidates:
        if label in by_label:
            entry = dict(by_label[label])
            entry["page_mtime"] = mtime
            out.append(entry)
            if len(out) >= limit:
                break
    return out


# ── KG context helpers ────────────────────────────────────────────────


def _fetch_known_entities() -> List[Dict[str, str]]:
    """One-row-per-node summary so the agent can resolve target labels to
    ids without us streaming the whole graph. Sorted by importance desc
    (so when the agent looks at the list, the most-relevant matches are
    near the top)."""
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node

    session = get_session()
    try:
        rows = (
            session.query(Node.id, Node.label, Node.node_type, Node.importance)
            .filter(Node.label.isnot(None))
            .order_by(Node.importance.desc().nulls_last(), Node.label.asc())
            .limit(2000)
            .all()
        )
        return [
            {"id": str(r.id), "label": r.label, "node_type": r.node_type or ""}
            for r in rows
        ]
    finally:
        session.close()


def _format_known_entities(rows: List[Dict[str, str]], *, exclude_id: str) -> str:
    """Tabular block for the prompt. id | type | label."""
    lines = ["id | type | label"]
    for r in rows:
        if r["id"] == exclude_id:
            continue
        lines.append(f"{r['id'][:8]} | {r['node_type']:14} | {r['label']}")
    return "\n".join(lines[:1500])


def _format_subject_neighborhood(node_id: str) -> str:
    """Render the subject's existing edges so the agent can de-dup against them."""
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
    from sqlalchemy.orm import aliased

    session = get_session()
    try:
        T = aliased(Node)
        S = aliased(Node)
        out_rows = (
            session.query(Edge.relationship_type, Edge.sentence, T.id, T.label, T.node_type)
            .join(T, T.id == Edge.target_id)
            .filter(Edge.source_id == node_id)
            .order_by(Edge.created_at.desc())
            .limit(60)
            .all()
        )
        in_rows = (
            session.query(Edge.relationship_type, Edge.sentence, S.id, S.label, S.node_type)
            .join(S, S.id == Edge.source_id)
            .filter(Edge.target_id == node_id)
            .order_by(Edge.created_at.desc())
            .limit(60)
            .all()
        )
    finally:
        session.close()

    if not out_rows and not in_rows:
        return "(no existing edges)"

    parts = []
    if out_rows:
        parts.append("Outgoing:")
        for rel, sentence, tid, tlabel, ttype in out_rows:
            parts.append(f"  -[{rel}]-> {tlabel} ({ttype}) [id={str(tid)[:8]}]")
    if in_rows:
        parts.append("Incoming:")
        for rel, sentence, sid, slabel, stype in in_rows:
            parts.append(f"  <-[{rel}]- {slabel} ({stype}) [id={str(sid)[:8]}]")
    return "\n".join(parts)


def _edge_exists(
    *,
    subject_id: str,
    target_id: Optional[str],
    target_label: Optional[str],
    predicate: Optional[str],
) -> bool:
    """Verify-not-already check. Returns True if there's already a same-
    predicate edge between subject and the target (resolved by id or label)."""
    if not predicate:
        return False
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node

    session = get_session()
    try:
        if target_id:
            n = session.query(Edge).filter(
                Edge.relationship_type == predicate,
                ((Edge.source_id == subject_id) & (Edge.target_id == target_id))
                | ((Edge.source_id == target_id) & (Edge.target_id == subject_id))
            ).first()
            return n is not None
        if target_label:
            target_rows = session.query(Node.id).filter(Node.label == target_label).all()
            if not target_rows:
                return False
            target_ids = [str(r.id) for r in target_rows]
            n = session.query(Edge).filter(
                Edge.relationship_type == predicate,
                ((Edge.source_id == subject_id) & (Edge.target_id.in_(target_ids)))
                | ((Edge.source_id.in_(target_ids)) & (Edge.target_id == subject_id))
            ).first()
            return n is not None
        return False
    finally:
        session.close()


# ── Proposal write ─────────────────────────────────────────────────────


def _write_proposal_for_inferred_connection(
    *,
    subject: Dict[str, Any],
    connection: Dict[str, Any],
    pipeline_run_id: Optional[str],
) -> bool:
    """Push one InferredConnection through the standard proposal_writer.

    Builds a minimal nodes/edges/anchor tuple matching the contract used
    by the chat-side fact_extractor, so the same promoter logic applies.
    Returns True on success.
    """
    from app.assistant.kg.proposal_writer import write_one_proposal_group

    subject_temp = "wi_s"
    target_temp = "wi_t"

    nodes: List[Dict[str, Any]] = [
        {
            "temp_id": subject_temp,
            "label": subject["label"],
            "node_type": subject.get("node_type") or "Entity",
            "kg_node_id": subject["id"],  # already-resolved
        },
    ]
    if connection.get("target_node_id"):
        nodes.append({
            "temp_id": target_temp,
            "label": "",  # filled by promoter from kg_node_id
            "node_type": "Entity",
            "kg_node_id": connection["target_node_id"],
        })
    else:
        nodes.append({
            "temp_id": target_temp,
            "label": connection.get("target_label") or "(unknown)",
            "node_type": connection.get("target_node_type") or "Entity",
        })

    edges: List[Dict[str, Any]] = [{
        "source_temp_id": subject_temp,
        "target_temp_id": target_temp,
        # proposal_writer reads `relationship_type` (or `label`) — not `predicate` —
        # then runs it through normalize_predicate. Aligning with the established
        # extractor-side contract instead of inventing a parallel name.
        "relationship_type": connection.get("predicate") or "related_to",
        "sentence": connection.get("sentence") or "",
    }]

    # Anchor doesn't have a real conversation behind it, so we synthesize
    # one rooted in "wiki_inference" so the promoter's evidence row carries
    # provenance back to this run.
    anchor: Dict[str, Any] = {
        "room_id": "wiki_inference",
        "speaker_name": "wiki_connection_investigator",
        "speaker_role": "system",
        "unified_log_id": None,
        "observed_at": datetime.now(timezone.utc),
        "raw_text": (
            f"Inferred from wiki page on {subject['label']}: "
            f"{connection.get('inference_path') or ''} "
            f"Quote: {connection.get('evidence_quote') or ''}"
        )[:1000],
    }

    session = get_session()
    try:
        proposal_id = write_one_proposal_group(
            session,
            nodes=nodes,
            edges=edges,
            anchor=anchor,
            window_id=None,  # no chat window backs this proposal
            extractor_agent_name="wiki_connection_investigator",
            extraction_run_id=pipeline_run_id,
        )
        if proposal_id is None:
            session.rollback()
            return False
        session.commit()
        return True
    except Exception:
        session.rollback()
        logger.error(
            "[wiki_inference] write_one_proposal_group failed for subject=%s",
            subject["label"], exc_info=True,
        )
        return False
    finally:
        session.close()
