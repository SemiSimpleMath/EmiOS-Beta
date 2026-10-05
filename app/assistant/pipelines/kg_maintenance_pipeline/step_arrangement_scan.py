"""
Step: arrangement_scan (supersession of ongoing arrangements, 2026-10-05)

An open State ("a call every Thursday at 8pm") stays open, and every reader keeps treating it as current, after the
owner says the arrangement changed ("we meet on Mondays now"): the promoter's merger only decides merge-or-create,
and conflict detection covers five single-target predicates. A friend's card said Thursday for ten months.

Deterministic proposes (ids and joins only): an open State (no end_date, not valid_currently=False) with a dated
claim, and the State/Event nodes that share at least one participant Entity with it (the primary user does not count:
he shares everything) and were first said AFTER the open State was last said. Said dates come from the source
messages (kg_node_evidence.message_timestamp), never Node.first_observed (the import date for legacy claims). The
later statements are ranked by sentence similarity to the open State and capped; pairs already ruled distinct are
left out. An open State is judged once, and again only when a later statement it was not judged against appears;
the open States with the most recent later statements go first.

The LLM decides: the arrangement_judge reads the open State and the later statements in full and says supersedes /
both_true / same_statement / unrelated / unsure. 'supersedes' (high or medium confidence, a named later statement and
a change date) becomes an arrangement_supersession finding, born investigated with disposition='needs_user_review':
on Accept the executor closes the State (kg_close_state, end_date = the change date) and links it to its successor
(kg_create_edge succeeded_by). Anything else writes a rejected finding recording the later statements it was
judged against. Nothing here mutates the graph.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from app.assistant.kg_maintenance.store import upsert_finding
from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import utc_now
from app.models.base import get_session

logger = get_logger(__name__)

FINDING_TYPE = "arrangement_supersession"
MAX_JUDGES_PER_RUN = 100
MAX_LATER_PER_STATE = 8
MAX_EVIDENCE_PER_NODE = 30
CONFIDENCE_MAP = {"high": 0.9, "medium": 0.7, "low": 0.4}
VERDICTS = {"supersedes", "both_true", "same_statement", "unrelated", "unsure"}


def _run_judge(agent_input: Dict[str, Any]) -> Dict[str, Any]:
    from app.assistant.routine_handlers.subconscious import _run_subconscious_agent

    return _run_subconscious_agent(
        handler_label="arrangement_judge",
        agent_name="kg_maintenance::arrangement_judge",
        context=agent_input,
        scope_id="kg_maintenance::arrangement_judge",
        actor_id="arrangement_scan",
    )


def _embed(texts: List[str]) -> Dict[str, Any]:
    """text -> unit vector, one batch call to the local sentence embedder."""
    import numpy as np
    from app.assistant.embeddings.embedder import embed_texts

    unique = list(dict.fromkeys(texts))
    out = {}
    for t, v in zip(unique, embed_texts(unique)):
        a = np.array(v, dtype=float)
        n = float(np.linalg.norm(a))
        out[t] = a / n if n else a
    return out


def _similarity(text: str, others: List[str], vectors: Dict[str, Any]) -> List[float]:
    """Cosine similarity of `text` to each of `others`, from pre-computed unit vectors."""
    import numpy as np
    return [float(np.dot(vectors[text], vectors[o])) for o in others]


def _said(session, node_ids: Set[str]) -> Dict[str, tuple]:
    """node_id -> (first, last) datetime its claim was said, from its source messages."""
    from sqlalchemy import func

    from app.assistant.database.kg_chat_projection import KGNodeEvidence
    if not node_ids:
        return {}
    rows = (
        session.query(KGNodeEvidence.node_id, func.min(KGNodeEvidence.message_timestamp),
                      func.max(KGNodeEvidence.message_timestamp))
        .filter(KGNodeEvidence.node_id.in_(list(node_ids)))
        .filter(KGNodeEvidence.message_timestamp.isnot(None))
        .group_by(KGNodeEvidence.node_id)
        .all()
    )
    return {r[0]: (r[1], r[2]) for r in rows}


def _said_text(said: tuple) -> str:
    first, last = (str(d)[:10] for d in said)
    return first if first == last else f"first {first}, last {last}"


def gather(session, *, now) -> List[Dict[str, Any]]:
    """The judge inputs for this run, most important open State first (deterministic)."""
    from app.assistant.database.kg_chat_projection import KGNodeEvidence
    from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
    from app.assistant.kg_core.kg_utils.evidence_sampler import sample_evidence
    from app.assistant.kg_maintenance.verdict_store import load_distinct_verdicts_among
    from app.assistant.utils.identity_names import get_required_primary_user_name

    owner = get_required_primary_user_name().strip().lower()
    entities = {n.id: n for n in session.query(Node).filter(Node.node_type == "Entity").all()}
    owner_ids = {i for i, n in entities.items() if (n.label or "").strip().lower() == owner}
    claims = {n.id: n for n in session.query(Node).filter(Node.node_type.in_(("State", "Event"))).all()}

    # Participant index: claim -> its Entity neighbours (the owner left out), and back.
    parts: Dict[str, Set[str]] = {}
    for src, tgt in session.query(Edge.source_id, Edge.target_id).all():
        for claim, other in ((src, tgt), (tgt, src)):
            if claim in claims and other in entities and other not in owner_ids:
                parts.setdefault(claim, set()).add(other)
    by_participant: Dict[str, Set[str]] = {}
    for claim, ps in parts.items():
        for p in ps:
            by_participant.setdefault(p, set()).add(claim)

    said = _said(session, set(parts))
    # Judged before: an open finding keeps the State out; any finding records the later statements it was judged
    # against, and the State is judged again only when a later statement outside them appears. The open State is
    # read from evidence.older_id: upsert_finding stores a node pair sorted by id, so primary_node_id can be
    # either node.
    open_finding: Set[str] = set()
    judged_against: Dict[str, Set[str]] = {}
    for status, evidence in (
            session.query(KGMaintenanceFinding.status, KGMaintenanceFinding.evidence_json)
            .filter(KGMaintenanceFinding.finding_type == FINDING_TYPE).all()):
        older_id = (evidence or {})["older_id"]
        if status in ("pending", "investigated", "approved", "executing"):
            open_finding.add(older_id)
        judged_against.setdefault(older_id, set()).update(evidence.get("judged_against") or [])
    open_states = [
        n for n in claims.values()
        if n.node_type == "State" and n.end_date is None and getattr(n, "valid_currently", None) is not False
        and n.id in said and n.id in parts and n.id not in open_finding
    ]

    def node_view(n, *, with_dates=True) -> Dict[str, Any]:
        ev_rows = (session.query(KGNodeEvidence).filter(KGNodeEvidence.node_id == n.id)
                   .filter(KGNodeEvidence.digested_at.is_(None)).all())
        sampled = sample_evidence(ev_rows, cap=MAX_EVIDENCE_PER_NODE,
                                  date_key=lambda ev: ev.message_timestamp or ev.created_at)
        return {
            "node_id": n.id, "label": n.label, "node_type": n.node_type, "category": n.category,
            "sentence": n.original_sentence, "said": _said_text(said[n.id]),
            "end_date": str(n.end_date)[:10] if n.end_date else None,
            "participants": sorted(entities[p].label for p in parts.get(n.id, ()) if p in entities),
            "evidence": [{"date": str(ev.message_timestamp)[:10] if ev.message_timestamp else None,
                          "text": ev.source_text or ev.derived_sentence or ""} for ev in sampled],
        }

    def text_of(n) -> str:
        return n.original_sentence or n.label or ""

    groups = []
    for older in open_states:
        last_said = said[older.id][1]
        later_ids = set()
        for p in parts[older.id]:
            later_ids |= by_participant.get(p, set())
        later = [claims[i] for i in later_ids
                 if i != older.id and i in said and said[i][0] > last_said]
        if later and not {n.id for n in later} <= judged_against.get(older.id, set()):
            groups.append((older, later))
    # The open States with the most recent later statements first: a fresh change matters most.
    groups.sort(key=lambda g: max(said[n.id][0] for n in g[1]), reverse=True)
    picked = []
    for older, later in groups:
        if len(picked) >= MAX_JUDGES_PER_RUN:
            break
        distinct = {frozenset((a, b)) for a, b, _, _ in load_distinct_verdicts_among([older.id] + [n.id for n in later])}
        later = [n for n in later if frozenset((older.id, n.id)) not in distinct]
        if later:
            picked.append((older, later))
    groups = picked
    vectors = _embed([text_of(n) for older, later in groups for n in [older, *later]])
    out: List[Dict[str, Any]] = []
    for older, later in groups:
        scores = _similarity(text_of(older), [text_of(n) for n in later], vectors)
        ranked = [n for _, n in sorted(zip(scores, later), key=lambda t: -t[0])][:MAX_LATER_PER_STATE]
        out.append({"older": node_view(older), "newer": [node_view(n) for n in ranked],
                    "considered": sorted(n.id for n in later), "today": now.date().isoformat()})
    return out


def _check(verdict: Dict[str, Any], newer_ids: Set[str]) -> str:
    """The verdict, validated: a malformed answer raises (fail loud), never reads as 'unsure'."""
    v = str(verdict.get("verdict") or "").strip().lower()
    if v not in VERDICTS:
        raise ValueError(f"arrangement_judge: verdict {v!r} is not one of {sorted(VERDICTS)}")
    if v == "supersedes":
        if str(verdict.get("superseded_by") or "").strip() not in newer_ids:
            raise ValueError(f"arrangement_judge: superseded_by {verdict.get('superseded_by')!r} is not one of the "
                             f"later statements")
        from datetime import date
        date.fromisoformat(str(verdict.get("change_date") or "")[:10])
    return v


def run(ctx: PipelineContext) -> dict:
    """Returns {"candidates": int, "judged": int, "supersession_findings": int, "cooled": int}."""
    now = utc_now()
    session = get_session()
    try:
        inputs = gather(session, now=now)
    finally:
        session.close()

    findings = cooled = 0
    for ji in inputs:
        older = ji["older"]
        judged = ji.pop("considered")  # every later statement weighed, shown or ranked below the cap
        verdict = _run_judge(ji)
        v = _check(verdict, {n["node_id"] for n in ji["newer"]})
        conf = str(verdict.get("confidence") or "low").strip().lower()
        if v == "supersedes" and conf in ("high", "medium"):
            newer = next(n for n in ji["newer"] if n["node_id"] == verdict["superseded_by"].strip())
            fid, created = upsert_finding(
                finding_type=FINDING_TYPE,
                primary_node_id=older["node_id"],
                secondary_node_id=newer["node_id"],
                suggested_action="close_state",
                reason=f"{older['label']!r} was replaced by {newer['label']!r}: {verdict.get('reasoning') or ''}",
                confidence=CONFIDENCE_MAP.get(conf, 0.5),
                priority="high" if conf == "high" else "medium",
                agent_name="arrangement_scan",
                evidence={"older_id": older["node_id"], "older": older, "newer": newer,
                          "change_date": verdict["change_date"][:10],
                          "change_date_basis": verdict.get("change_date_basis"), "judge_confidence": conf,
                          "judged_against": judged},
                pipeline_run_id=ctx.run_id,
            )
            if created:
                _promote_to_review(fid, older=older, newer=newer, verdict=verdict)
                findings += 1
        else:
            upsert_finding(
                finding_type=FINDING_TYPE,
                primary_node_id=older["node_id"],
                suggested_action="none",
                reason=f"arrangement_judge: {v} ({conf}): {verdict.get('reasoning') or ''}",
                confidence=CONFIDENCE_MAP.get(conf, 0.5),
                priority="low",
                agent_name="arrangement_scan",
                evidence={"older_id": older["node_id"], "judged_against": judged, "verdict": v},
                pipeline_run_id=ctx.run_id,
                initial_status="rejected",
            )
            cooled += 1
    summary = {"candidates": len(inputs), "judged": len(inputs), "supersession_findings": findings, "cooled": cooled}
    logger.info("[arrangement_scan] %s", summary)
    return summary


def _promote_to_review(finding_id: str, *, older: Dict[str, Any], newer: Dict[str, Any],
                       verdict: Dict[str, Any]) -> None:
    """Born investigated with needs_user_review: the judge did the investigation, and closing a fact is gated on the
    owner's Accept (the role-succession precedent). The recommendation names the exact tool calls."""
    from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
    from app.models.db_manager import get_db_manager

    change_date = str(verdict["change_date"])[:10]
    prose = (f"superseded by {newer['label']} ({newer['node_id']}), said {newer['said']}; the old arrangement "
             f"ended no later than {change_date}")
    recommendation = (
        f"ARRANGEMENT SUPERSEDED: the open State `{older['node_id']}` ({older['label']!r}: {older['sentence']!r}, "
        f"said {older['said']}) was replaced by `{newer['node_id']}` ({newer['label']!r}: {newer['sentence']!r}, "
        f"said {newer['said']}). Apply, with finding_id={finding_id!r}:\n"
        f"1. kg_close_state(node_id={older['node_id']!r}, end_date={change_date!r}, end_date_confidence='inferred', "
        f"end_date_prose={prose!r})\n"
        f"2. kg_create_edge(source_id={older['node_id']!r}, target_id={newer['node_id']!r}, "
        f"relationship_type='succeeded_by', sentence={('The arrangement ' + repr(older['label']) + ' was succeeded by ' + repr(newer['label']) + '.')!r})\n"
        f"If the owner's review notes say both are true, or correct the date, the notes are ground truth."
    )
    with get_db_manager().transaction(op="arrangement_scan.promote") as s:
        f = s.query(KGMaintenanceFinding).filter_by(id=finding_id).first()
        if f is None:
            return
        f.status = "investigated"
        f.investigated_at = utc_now()
        f.investigation_report_json = {
            "recommendation": recommendation,
            "diagnosis": str(verdict.get("reasoning") or ""),
            "evidence": [{"kind": "change_date_basis", "text": str(verdict.get("change_date_basis") or "")}],
            "confidence": str(verdict.get("confidence") or "medium"),
            "disposition": "needs_user_review",
        }
