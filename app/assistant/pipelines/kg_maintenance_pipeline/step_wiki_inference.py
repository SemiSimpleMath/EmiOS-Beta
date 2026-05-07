"""
Step: wiki_inference

Runs `wiki_connection_investigator` over recently-updated wiki pages.
For each candidate subject:
  1. Read the rendered prose page.
  2. Pull the subject's KG neighborhood + a name-only list of known
     entities (so the agent can use canonical name spellings without
     us streaming the entire KG for every call).
  3. Call the agent — it returns a list of InferredSentence: synthetic
     factual sentences the page implies but the KG doesn't yet have.
  4. Write each accepted sentence as a kg_maintenance_finding with
     finding_type='wiki_inferred_fact'. NO direct ingestion. The
     maintenance UI surfaces these for user review; the user can fill
     in dates and approve, at which point a wiki-aware ingestion path
     (TBD — separate from the chat fact_extractor path) constructs the
     proper claim_proposal.

The agent is NOT trusted to write edges directly — only fact_extractor
or a similar wiki-aware extractor can construct entity → state → entity
correctly. The finding queue is the human-in-the-loop checkpoint
between "agent inferred" and "graph mutated."

Conservative defaults:
  - Only sentences with `confidence >= MIN_CONFIDENCE` produce findings.
  - Only sentences with `not_already_in_kg=True` produce findings.
  - Per-subject cap on findings.
  - Per-run cap on subjects examined.

Idempotency: a `<entity>.wiki_inference.json` sidecar next to each
prose page records `examined_at_epoch`. Re-runs skip the page until
its mtime advances (i.e., wiki_nightly_refresh rewrote it).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.kg_maintenance.store import upsert_finding
from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.models.base import get_session

logger = get_logger(__name__)


MIN_CONFIDENCE = 0.6
PER_SUBJECT_SENTENCE_CAP = 5
DEFAULT_SUBJECT_LIMIT = 10
FINDING_TYPE = "synthetic_fact_proposal"  # Generic — not wiki-specific
PRODUCER = "wiki_connection_investigator"


def run(ctx: PipelineContext, *, subject_limit: Optional[int] = None) -> dict:
    """Returns counts: {"subjects_examined": int, "sentences_emitted": int,
                         "sentences_rejected_low_confidence": int,
                         "sentences_rejected_already_in_kg": int}."""
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

    known_entities = _fetch_known_entities()

    subjects_examined = 0
    emitted = 0
    skipped_low_conf = 0
    skipped_already_in_kg = 0
    skipped_duplicate_finding = 0

    for subject in subjects:
        subjects_examined += 1
        page_path = prose_dir / f"{_safe_filename(subject['label'])}.md"
        try:
            page_text = _read_prose_page(prose_dir, subject["label"])
            if not page_text or len(page_text) < 200:
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
            sentences = list(data.get("sentences") or [])

            for s in sentences[:PER_SUBJECT_SENTENCE_CAP]:
                if not isinstance(s, dict):
                    continue
                if float(s.get("confidence") or 0.0) < MIN_CONFIDENCE:
                    skipped_low_conf += 1
                    continue
                if not s.get("not_already_in_kg"):
                    skipped_already_in_kg += 1
                    continue
                sentence_text = (s.get("sentence") or "").strip()
                if not sentence_text:
                    continue

                # Dedup against any existing wiki_inferred_fact finding for
                # this same subject + same canonical sentence. The standard
                # upsert_finding dedup keys on (type, primary, secondary),
                # which doesn't catch text-level dupes — we add an extra
                # check here.
                if _wiki_finding_already_exists(
                    primary_node_id=subject["id"],
                    sentence_text=sentence_text,
                ):
                    skipped_duplicate_finding += 1
                    continue

                if _write_wiki_inferred_finding(
                    sentence_text=sentence_text,
                    subject=subject,
                    inference=s,
                    pipeline_run_id=ctx.run_id,
                ):
                    emitted += 1

            _write_examined_sidecar(page_path)

        except Exception:
            logger.error(
                "[wiki_inference] subject %s crashed (continuing)",
                subject.get("label"), exc_info=True,
            )

    logger.info(
        "[wiki_inference] subjects=%d findings_written=%d skipped_low_conf=%d skipped_already=%d skipped_dup_finding=%d",
        subjects_examined, emitted, skipped_low_conf, skipped_already_in_kg, skipped_duplicate_finding,
    )
    return {
        "subjects_examined": subjects_examined,
        "findings_written": emitted,
        "skipped_low_confidence": skipped_low_conf,
        "skipped_already_in_kg": skipped_already_in_kg,
        "skipped_duplicate_finding": skipped_duplicate_finding,
    }


def _empty_result() -> dict:
    return {
        "subjects_examined": 0,
        "findings_written": 0,
        "skipped_low_confidence": 0,
        "skipped_already_in_kg": 0,
        "skipped_duplicate_finding": 0,
    }


# ── Vault / page resolution ────────────────────────────────────────────


def _resolve_vault_path() -> Optional[Path]:
    """Mirror of routes.wiki_viewer._wiki_vault_root() but importable from
    pipeline code without bringing in the Flask blueprint."""
    import os

    override = os.environ.get("EMI_WIKI_DIR")
    if override:
        return Path(override)
    try:
        from app.assistant.utils.config_utils import get_assistant_name
        name = get_assistant_name() or "Emi"
    except Exception:
        name = "Emi"
    return Path.home() / f"{name}Wiki"


def _safe_filename(label: str) -> str:
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
    p = prose_dir / f"{_safe_filename(label)}.md"
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning("[wiki_inference] read failed for %s: %s", p, e)
        return None


def _pick_subjects_to_examine(prose_dir: Path, *, limit: int) -> List[Dict[str, Any]]:
    """Pick the N subjects whose prose page is freshest AND who haven't
    been examined since their last page write (sidecar gate)."""
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node

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
                        continue
                except Exception:
                    pass
            candidates.append((label, page_mtime))
    except Exception as e:
        logger.warning("[wiki_inference] prose dir scan failed: %s", e)
        return []

    candidates.sort(key=lambda t: t[1], reverse=True)
    candidates = candidates[: limit * 3]

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
    """One-row-per-node summary so the agent can use canonical name
    spellings. Sorted by importance desc."""
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
    """Tabular block for the prompt. type | label.

    Note: id column dropped — the agent doesn't need ids in the new
    sentence-emitting design. fact_extractor resolves names to ids on
    its own pass. Removing ids saves prompt budget and removes a
    hallucination surface."""
    lines = ["type | label"]
    for r in rows:
        if r["id"] == exclude_id:
            continue
        lines.append(f"{r['node_type']:14} | {r['label']}")
    return "\n".join(lines[:1500])


def _format_subject_neighborhood(node_id: str) -> str:
    """Render the subject's existing edges so the agent can de-dup
    against them."""
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
            parts.append(f"  -[{rel}]-> {tlabel} ({ttype})")
    if in_rows:
        parts.append("Incoming:")
        for rel, sentence, sid, slabel, stype in in_rows:
            parts.append(f"  <-[{rel}]- {slabel} ({stype})")
    return "\n".join(parts)


# ── finding write + dedup ──────────────────────────────────────────────


_WS_RE = re.compile(r"\s+")


def _canonical_sentence_hash(sentence_text: str) -> str:
    """Hash for dedup: lowercased, whitespace-collapsed sentence. Two
    inferences with identical canonical form produce the same hash
    even if punctuation/casing differs."""
    s = _WS_RE.sub(" ", (sentence_text or "")).strip().lower().rstrip(".")
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def _wiki_finding_already_exists(*, primary_node_id: str, sentence_text: str) -> bool:
    """True if a wiki_inferred_fact finding with the same canonical
    sentence hash already exists for this primary node, in any status.
    The hash is stored in evidence_json['sentence_hash'] when the
    finding is written.

    Why all statuses (not just pending/approved): if the user already
    rejected this fact ("not actually true / not interesting"), don't
    keep re-surfacing it on every nightly run. Same goes for executed —
    that fact is in the graph; don't propose it again."""
    h = _canonical_sentence_hash(sentence_text)
    session = get_session()
    try:
        # SQLite JSON_EXTRACT works on the JSON column; portable enough
        # within this codebase. Pythonic fallback if not.
        from sqlalchemy import text as sql_text
        try:
            row = session.execute(
                sql_text(
                    "SELECT 1 FROM kg_maintenance_finding "
                    "WHERE finding_type = :ft "
                    "  AND primary_node_id = :pid "
                    "  AND json_extract(evidence_json, '$.sentence_hash') = :h "
                    "LIMIT 1"
                ),
                {"ft": FINDING_TYPE, "pid": primary_node_id, "h": h},
            ).first()
            return row is not None
        except Exception:
            # JSON-extract may not be available on every SQLite build;
            # fall back to scanning python-side.
            rows = (
                session.query(KGMaintenanceFinding)
                .filter(KGMaintenanceFinding.finding_type == FINDING_TYPE)
                .filter(KGMaintenanceFinding.primary_node_id == primary_node_id)
                .all()
            )
            for r in rows:
                ev = r.evidence_json or {}
                if isinstance(ev, dict) and ev.get("sentence_hash") == h:
                    return True
            return False
    finally:
        session.close()


def _write_wiki_inferred_finding(
    *,
    sentence_text: str,
    subject: Dict[str, Any],
    inference: Dict[str, Any],
    pipeline_run_id: Optional[str],
) -> bool:
    """Write one wiki_inferred_fact finding. The maintenance UI surfaces
    these to the user; the user reviews + fills any missing dates +
    explicitly approves before any KG mutation. NEVER auto-applied.

    Returns True on success.
    """
    confidence_val = inference.get("confidence")
    try:
        confidence = float(confidence_val) if confidence_val is not None else None
    except (TypeError, ValueError):
        confidence = None

    evidence: Dict[str, Any] = {
        # Producer identity — multiple agents can emit synthetic_fact_proposal
        # findings (wiki investigator today; "find missing connections" or
        # other graph-walkers tomorrow). The review UI groups by producer.
        "producer": PRODUCER,
        "sentence": sentence_text,
        "sentence_hash": _canonical_sentence_hash(sentence_text),
        "evidence_quote": (inference.get("evidence_quote") or "")[:1000],
        "inference_path": (inference.get("inference_path") or "")[:1000],
        "agent_confidence": confidence,
        "subject_label": subject.get("label"),
        "subject_node_type": subject.get("node_type"),
        # Suggested dates — user reviews + adjusts at approval time.
        "suggested_start_date": inference.get("suggested_start_date") or None,
        "suggested_end_date": inference.get("suggested_end_date") or None,
        "suggested_start_date_prose": inference.get("suggested_start_date_prose") or None,
        "suggested_end_date_prose": inference.get("suggested_end_date_prose") or None,
        # Review state machine (defaults — flipped by the review module
        # when the user edits/approves/rejects):
        #   review_status: 'pending_review' | 'edited' | 'approved' | 'rejected'
        #   user_edited_sentence: filled in if user rewrote the sentence
        #   user_dates: {start_date, end_date, start_date_prose, end_date_prose}
        #   user_notes: free-text comments
        #   extraction_result: structural decomposition once the wiki-aware
        #     extractor runs (post-approve); shape TBD.
        "review_status": "pending_review",
    }
    reason = (
        f"Inferred from wiki page on {subject.get('label')!r}: {sentence_text} "
        f"(conf={confidence})"
    )[:1000]

    try:
        upsert_finding(
            finding_type=FINDING_TYPE,
            primary_node_id=subject["id"],
            suggested_action="review",
            reason=reason,
            confidence=confidence,
            priority="low",
            agent_name="wiki_connection_investigator",
            evidence=evidence,
            pipeline_run_id=pipeline_run_id,
        )
        return True
    except Exception:
        logger.error(
            "[wiki_inference] finding write failed for subject=%s",
            subject.get("label"), exc_info=True,
        )
        return False
